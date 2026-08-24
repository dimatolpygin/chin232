"""Копия базы на стороннем хранилище.

Зачем это вообще. Дампы, которые лежат на том же диске, что и база, спасают от
неудачной миграции и не спасают ни от чего больше: сервер меняют, диск теряют,
хостер отключает машину за неоплату. Поэтому копия уезжает в S3, а хранится
столько, чтобы можно было откатиться не только на вчера.

Что НЕ попадает в копию и должно быть под рукой отдельно:

* `.env` с боевого сервера — там пароль базы, токен бота, ключи сервисов и
  секрет вебхука. В хранилище он не кладётся намеренно: бакет общий с другими
  проектами, и один утёкший ключ доступа к нему открыл бы весь проект разом.
  Все эти значения лежат в `dostupi.txt`, восстанавливаются оттуда руками.
* Redis — это очередь и кеш. В нём нет ничего, чего нельзя пересоздать: в
  худшем случае теряются круги, которые в момент падения были в работе.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.engine import make_url

from app.config import Settings, get_settings
from app.core.providers.storage import S3Storage, StoredObject
from app.core.services.limits import DEFAULT_TZ
from app.logging import get_logger

log = get_logger("backup")

# Имя копии. По нему же работает уборка: под удаление попадает только то, что
# сошлось с этим шаблоном — чужие файлы в общем бакете не наши, и трогать их
# нельзя ни при какой настройке.
NAME_TEMPLATE = "china_bot-{day}.sql.gz"
NAME_PATTERN = re.compile(r"^china_bot-(\d{4})-(\d{2})-(\d{2})\.sql\.gz$")

# Дамп собирается в память целиком: у этого проекта он — единицы мегабайт.
# Если база когда-нибудь дорастёт до сотен, здесь понадобится поток на диск, и
# предупреждение в логе придёт раньше, чем кончится память.
BIG_DUMP_BYTES = 200 * 1024 * 1024

DUMP_TIMEOUT_SEC = 300


class BackupError(RuntimeError):
    """Копия не собралась. Отдельный тип: об этом обязан узнать человек."""


@dataclass(slots=True)
class BackupResult:
    """Чем кончилась ночная копия."""

    name: str
    size: int
    seconds: float
    deleted: list[str] = field(default_factory=list)
    kept: int = 0


def today_msk() -> date:
    """День копии по Москве: по нему живёт заказчик, по нему и имена файлов."""
    return datetime.now(ZoneInfo(DEFAULT_TZ)).date()


def backup_name(day: date) -> str:
    return NAME_TEMPLATE.format(day=day.isoformat())


def parse_day(name: str) -> date | None:
    """Дата из имени копии. None — файл не наш, и уборка его не касается."""
    match = NAME_PATTERN.match(name)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


async def dump_database(settings: Settings | None = None) -> bytes:
    """Снять дамп базы через `pg_dump` и сжать.

    Ходим по сети к контейнеру базы, а не внутрь него: доступа к чужому
    контейнеру у воркера нет и быть не должно — это означало бы отдать ему
    сокет docker, то есть root на хосте.
    """
    settings = settings or get_settings()
    url = make_url(settings.database_url)

    args = [
        "pg_dump",
        "-h",
        url.host or "postgres",
        "-p",
        str(url.port or 5432),
        "-U",
        url.username or "china_bot",
        "-d",
        url.database or "china_bot",
        # Права и владельцы принадлежат конкретной машине; на новой их не
        # будет, и без этих двух флагов восстановление сыпет ошибками там, где
        # ошибок нет.
        "--no-owner",
        "--no-privileges",
        # Чтобы дамп накатывался и поверх живой базы, а не только в пустую.
        "--clean",
        "--if-exists",
    ]

    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Пароль только через окружение: в аргументах его видно всякому,
            # кто запустит `ps` на хосте.
            env={**os.environ, "PGPASSWORD": url.password or ""},
        )
    except FileNotFoundError as exc:
        raise BackupError(
            "в контейнере нет pg_dump — нужен postgresql-client той же версии, что и сервер"
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DUMP_TIMEOUT_SEC)
    except TimeoutError as exc:
        process.kill()
        raise BackupError(f"pg_dump не уложился в {DUMP_TIMEOUT_SEC} секунд") from exc

    if process.returncode != 0 or not stdout:
        tail = stderr.decode("utf-8", "replace")[-800:]
        raise BackupError(f"pg_dump вернул код {process.returncode}: {tail}")

    if len(stdout) > BIG_DUMP_BYTES:
        log.warning(
            "дамп базы стал большим, пора снимать его потоком, а не в память",
            байт=len(stdout),
            предел=BIG_DUMP_BYTES,
        )

    сжато = gzip.compress(stdout, compresslevel=6)
    log.info(
        "дамп базы снят",
        база=url.database,
        байт_дампа=len(stdout),
        байт_архива=len(сжато),
        длительность_сек=round(time.monotonic() - started, 2),
    )
    return сжато


def keep_plan(
    days: list[date], today: date, keep_daily: int, keep_weekly: int
) -> tuple[set[date], set[date]]:
    """Что оставить и что удалить. Чистая функция: уборка обязана быть проверяемой.

    Правило простое. Последние `keep_daily` дней — целиком, это ответ на
    «вчера всё работало». Дальше в глубину остаются только воскресенья, по
    одному на неделю, — это ответ на «когда именно данные испортились», и
    стоит он в одиннадцать файлов вместо тридцати.

    Копия за сегодня не удаляется никогда, даже если настройки выкрутили в
    ноль: иначе задача сама снесла бы то, что только что положила.
    """
    keep: set[date] = {today}
    порог = today - timedelta(days=max(keep_daily, 0))

    for day in sorted(days, reverse=True):
        if day > today:
            # Файл из будущего — след ручного эксперимента или сбитых часов.
            # Не наше дело его удалять.
            keep.add(day)
        elif day > порог:
            keep.add(day)

    # Воскресенья считаем от самых свежих: weekday() == 6.
    воскресенья = [d for d in sorted(days, reverse=True) if d.weekday() == 6 and d <= today]
    keep.update(воскресенья[: max(keep_weekly, 0)])

    return keep, {d for d in days if d not in keep}


async def run_backup(settings: Settings | None = None) -> BackupResult:
    """Снять копию, положить в хранилище, убрать старьё.

    Порядок именно такой: сначала кладём новое, потом удаляем старое. Наоборот
    нельзя — упавшая загрузка оставила бы проект вообще без копий.
    """
    settings = settings or get_settings()
    storage = S3Storage(settings)
    if not storage.configured:
        raise BackupError("хранилище копий не настроено: заданы не все переменные S3_*")

    started = time.monotonic()
    day = today_msk()
    имя = backup_name(day)
    ключ = settings.s3_prefix + имя

    архив = await dump_database(settings)
    await storage.put(ключ, архив)

    удалено = await prune(storage, settings, day)
    итог = BackupResult(
        name=имя,
        size=len(архив),
        seconds=round(time.monotonic() - started, 2),
        deleted=удалено,
        kept=len(await list_backups(storage, settings)),
    )
    log.info(
        "копия базы уехала в хранилище",
        файл=итог.name,
        мегабайт=round(итог.size / 1024 / 1024, 2),
        длительность_сек=итог.seconds,
        удалено_старых=len(итог.deleted),
        всего_копий=итог.kept,
    )
    return итог


async def list_backups(
    storage: S3Storage | None = None, settings: Settings | None = None
) -> list[StoredObject]:
    """Наши копии в хранилище, от старой к новой. Чужие файлы отсеиваются."""
    settings = settings or get_settings()
    storage = storage or S3Storage(settings)
    объекты = await storage.list(settings.s3_prefix)
    наши = [o for o in объекты if parse_day(o.name) is not None]
    наши.sort(key=lambda o: o.name)
    return наши


async def prune(storage: S3Storage, settings: Settings, today: date) -> list[str]:
    """Удалить копии, вышедшие из срока хранения. Возвращает имена удалённых."""
    копии = await list_backups(storage, settings)
    по_дням = {parse_day(o.name): o for o in копии if parse_day(o.name) is not None}

    _, лишние = keep_plan(
        list(по_дням.keys()), today, settings.backup_keep_daily, settings.backup_keep_weekly
    )

    удалено: list[str] = []
    for day in sorted(лишние):
        объект = по_дням[day]
        try:
            await storage.delete(объект.key)
        except Exception as exc:  # noqa: BLE001  неудачная уборка не отменяет копию
            log.warning("старую копию удалить не вышло", файл=объект.key, ошибка=repr(exc))
            continue
        удалено.append(объект.name)
    if удалено:
        log.info("старые копии убраны", файлы=", ".join(удалено))
    return удалено


@dataclass(slots=True, frozen=True)
class BackupStatus:
    """Что показать админу про копии: есть ли они и насколько свежие."""

    configured: bool
    last: StoredObject | None = None
    count: int = 0
    error: str | None = None

    @property
    def stale(self) -> bool:
        """Свежей копии нет. Значит, ночная задача молчит — а должна была."""
        if self.last is None:
            return True
        day = parse_day(self.last.name)
        return day is None or (today_msk() - day).days > 1


async def backup_status(settings: Settings | None = None) -> BackupStatus:
    """Состояние копий. Ходит в хранилище: показывать надо правду, а не отметку в базе."""
    settings = settings or get_settings()
    storage = S3Storage(settings)
    if not storage.configured:
        return BackupStatus(configured=False)
    try:
        копии = await list_backups(storage, settings)
    except Exception as exc:  # noqa: BLE001  недоступное хранилище не должно ронять админку
        log.warning("состояние копий узнать не вышло", ошибка=repr(exc))
        return BackupStatus(configured=True, error=str(exc)[:200])
    return BackupStatus(
        configured=True,
        last=копии[-1] if копии else None,
        count=len(копии),
    )


async def fetch_backup(name: str, settings: Settings | None = None) -> bytes:
    """Скачать копию по имени. Для восстановления руками."""
    settings = settings or get_settings()
    storage = S3Storage(settings)
    return await storage.get(settings.s3_prefix + name)
