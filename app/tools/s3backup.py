"""Копии базы руками: посмотреть, скачать, снять внеплановую, восстановить.

Запускается внутри контейнера воркера — там есть и настройки, и `pg_dump` с
`psql`:

    docker compose -f docker-compose.yml -f docker-compose.prod.yml \\
        exec -e PYTHONPATH=/app worker python -m app.tools.s3backup список

Команды по-русски намеренно: этой утилитой пользуется владелец бота, а не
только разработчик, и в момент, когда база уже потеряна, разбирать английские
глаголы — последнее, чем хочется заниматься.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import os
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.core.services.backup import fetch_backup, list_backups, run_backup, verify_restore
from app.logging import configure_logging, get_logger

log = get_logger("backup")


async def показать() -> int:
    копии = await list_backups()
    if not копии:
        print("Копий в хранилище нет.")
        return 1
    for объект in копии:
        мб = объект.size / 1024 / 1024
        print(f"{объект.name}\t{мб:8.2f} МБ\t{объект.modified:%d.%m.%Y %H:%M}")
    print(f"\nВсего: {len(копии)}")
    return 0


async def снять() -> int:
    итог = await run_backup()
    print(f"Готово: {итог.name}, {итог.size / 1024 / 1024:.2f} МБ, за {итог.seconds} с")
    if итог.deleted:
        print("Убрано старых: " + ", ".join(итог.deleted))
    return 0


async def проверить() -> int:
    """Убедиться, что свежая копия действительно накатывается."""
    итог = await verify_restore()
    print(
        f"Копия {итог.name} восстанавливается: таблиц {итог.tables}, "
        f"юзеров {итог.rows}, за {итог.seconds} с"
    )
    return 0


async def скачать(имя: str, куда: str) -> int:
    архив = await fetch_backup(имя)
    путь = Path(куда) / имя
    путь.write_bytes(архив)
    print(f"Скачано: {путь} ({len(архив) / 1024 / 1024:.2f} МБ)")
    print("Распаковать и накатить: gunzip -c ФАЙЛ | psql -h postgres -U china_bot -d china_bot")
    return 0


async def восстановить(имя: str, подтверждено: bool) -> int:
    """Накатить копию поверх текущей базы.

    Требует явного согласия: команда затирает живые данные, и спросить об этом
    после — уже не у кого.
    """
    if not подтверждено:
        print(
            "Эта команда ЗАТРЁТ текущую базу содержимым копии.\n"
            "Если вы уверены, повторите с флагом --да."
        )
        return 2

    url = make_url(get_settings().database_url)
    архив = await fetch_backup(имя)
    дамп = gzip.decompress(архив)
    print(f"Копия {имя} скачана: {len(дамп) / 1024 / 1024:.2f} МБ. Накатываю…")

    process = await asyncio.create_subprocess_exec(
        "psql",
        "-h",
        url.host or "postgres",
        "-p",
        str(url.port or 5432),
        "-U",
        url.username or "china_bot",
        "-d",
        url.database or "china_bot",
        # Без этого psql молча проглотит половину ошибок и отрапортует успех.
        "-v",
        "ON_ERROR_STOP=1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PGPASSWORD": url.password or ""},
    )
    stdout, stderr = await process.communicate(input=дамп)
    if process.returncode != 0:
        print(stderr.decode("utf-8", "replace")[-2000:])
        print(f"psql вернул код {process.returncode}: база НЕ восстановлена целиком.")
        return 1
    print(stdout.decode("utf-8", "replace")[-500:])
    print("База восстановлена. Перезапустите контейнеры бота и воркера.")
    return 0


def main() -> int:
    configure_logging(get_settings().log_level, get_settings().log_format)

    parser = argparse.ArgumentParser(description="Копии базы в хранилище S3")
    sub = parser.add_subparsers(dest="команда", required=True)
    sub.add_parser("список", help="что лежит в хранилище")
    sub.add_parser("снять", help="сделать копию прямо сейчас")
    sub.add_parser("проверить", help="накатить свежую копию в отдельную базу и проверить")

    p_get = sub.add_parser("скачать", help="сохранить копию в файл")
    p_get.add_argument("имя")
    p_get.add_argument("--куда", default=".")

    p_restore = sub.add_parser("восстановить", help="накатить копию поверх базы")
    p_restore.add_argument("имя")
    p_restore.add_argument("--да", action="store_true", dest="да")

    args = parser.parse_args()
    if args.команда == "список":
        return asyncio.run(показать())
    if args.команда == "снять":
        return asyncio.run(снять())
    if args.команда == "проверить":
        return asyncio.run(проверить())
    if args.команда == "скачать":
        return asyncio.run(скачать(args.имя, args.куда))
    return asyncio.run(восстановить(args.имя, args.да))


if __name__ == "__main__":
    sys.exit(main())
