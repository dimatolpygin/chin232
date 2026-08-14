"""Точка входа воркера.

Свой запуск вместо CLI `arq`: тот зовёт asyncio.get_event_loop() до создания
цикла, а uvloop (приезжает вместе с uvicorn[standard]) на это бросает
RuntimeError. Внутри asyncio.run цикл уже есть, и arq получает именно его.
"""

from __future__ import annotations

import asyncio

from arq.worker import create_worker

from app.worker.main import WorkerSettings


async def _run() -> None:
    worker = create_worker(WorkerSettings)  # type: ignore[arg-type]
    await worker.async_run()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
