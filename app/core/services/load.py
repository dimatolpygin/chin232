"""Нагрузка на сервер: когда пора добавлять ядра.

Круг разговора почти весь состоит из ожидания чужих серверов, и только один
его кусок работает на нашем процессоре — ffmpeg. На машине с одним ядром
именно он и упирается в потолок первым: пока кругов мало, конвертация занимает
доли секунды, а когда их десяток разом, они выстраиваются в очередь к
единственному ядру, и ждёт уже пользователь.

Смотреть на это глазами по логам бесполезно — цифра нужна тогда, когда никто
не смотрит. Поэтому здесь считается сводка, а по ней выносится приговор в трёх
словах: запас есть, плотно, потолок. Дальше её показывает админка, а при
«потолке» бот сам пишет админу.

Что именно считается и почему:

* **Ожидание в очереди** — сколько реплика пролежала от нажатия «отправить»
  до начала работы. Пока сервер справляется, это доли секунды. Оно растёт
  первым, раньше длительности самого круга, и потому годится в ранний признак.
* **Всего** — ожидание плюс круг, то есть ровно то время, которое видит
  пользователь. По нему проверяется обещанный бюджет в двенадцать секунд.
* **Доля ffmpeg** — подтверждение, что упираемся именно в процессор, а не в
  медленный ответ внешнего сервиса. Если тормозит внешний сервис, ядра не
  помогут, и апгрейд будет выброшенными деньгами.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.services.limits import DEFAULT_TZ
from app.logging import get_logger

log = get_logger("load")

# Обещанный клиенту бюджет круга: дольше этого человек ждёт ответа слишком долго.
BUDGET_SEC = 12.0

# Пороги приговора. Числа не выдуманы: на живой нагрузочной проверке двадцать
# одновременных кругов на одном ядре дали медиану 9.7 секунды при одиночном
# круге в 2.5 — то есть машина уже тогда работала на пределе, а ожидание в
# очереди было первым, что выросло.
WAIT_TIGHT_SEC = 1.5
WAIT_CEILING_SEC = 5.0
# Доля кругов, вылезших за бюджет.
SLOW_TIGHT = 0.05
SLOW_CEILING = 0.20
# Средняя очередь к процессору на одно ядро. Единица — ядро занято целиком.
LOAD_TIGHT = 1.0
LOAD_CEILING = 2.0

# Меньше этого числа кругов — считать не о чем: одна медленная ночная задача
# сделает картину какой угодно.
ENOUGH_ROUNDS = 20

VERDICT_UNKNOWN = "мало данных"
VERDICT_FREE = "запас есть"
VERDICT_TIGHT = "плотно"
VERDICT_CEILING = "потолок"


@dataclass(slots=True, frozen=True)
class Load:
    """Сводка нагрузки за период."""

    hours: int
    rounds: int
    wait_median: float
    wait_p95: float
    total_median: float
    total_p95: float
    ffmpeg_ms: float
    slow: int
    busiest_minute: int
    refused: int
    queue_depth: int
    load_avg: float | None
    cores: int

    @property
    def slow_share(self) -> float:
        """Доля кругов, не уложившихся в бюджет."""
        return self.slow / self.rounds if self.rounds else 0.0

    @property
    def load_per_core(self) -> float | None:
        if self.load_avg is None or not self.cores:
            return None
        return round(self.load_avg / self.cores, 2)

    @property
    def ffmpeg_share(self) -> float:
        """Какую часть круга занимает своя же конвертация звука."""
        if not self.total_median:
            return 0.0
        return min(self.ffmpeg_ms / 1000 / self.total_median, 1.0)

    @property
    def verdict(self) -> str:
        if self.rounds < ENOUGH_ROUNDS:
            return VERDICT_UNKNOWN
        нагрузка = self.load_per_core
        if (
            self.wait_p95 >= WAIT_CEILING_SEC
            or self.slow_share >= SLOW_CEILING
            or (нагрузка is not None and нагрузка >= LOAD_CEILING)
        ):
            return VERDICT_CEILING
        if (
            self.wait_p95 >= WAIT_TIGHT_SEC
            or self.slow_share >= SLOW_TIGHT
            or (нагрузка is not None and нагрузка >= LOAD_TIGHT)
        ):
            return VERDICT_TIGHT
        return VERDICT_FREE

    @property
    def alarming(self) -> bool:
        """Пора звать человека."""
        return self.verdict == VERDICT_CEILING


def cpu_load() -> tuple[float | None, int]:
    """Средняя очередь к процессору за минуту и число ядер.

    В контейнере `/proc/loadavg` показывает хозяйскую машину целиком, и это
    как раз то, что нужно: соседи по серверу отнимают то же самое ядро. На
    Windows такого счётчика нет вовсе — на машине разработки метрика просто
    не показывается, вместо того чтобы ронять экран.
    """
    try:
        минута = os.getloadavg()[0]
    except (AttributeError, OSError):
        return None, os.cpu_count() or 1
    return round(минута, 2), os.cpu_count() or 1


async def queue_depth(queue: Any) -> int:
    """Сколько задач сейчас ждёт своей очереди. Ноль — воркер успевает."""
    if queue is None:
        return 0
    try:
        return int(await queue.zcard(get_settings().redis_prefix + "arq"))
    except Exception as exc:  # noqa: BLE001  экран нагрузки не должен падать из-за redis
        log.warning("длину очереди узнать не вышло", ошибка=repr(exc))
        return 0


# Перцентили считает сам постгрес: тащить в питон тысячи строк ради двух чисел
# незачем, а percentile_cont умеет это одним проходом.
_STATS_SQL = """
SELECT
    count(*) AS rounds,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY (payload->>'ожидание_сек')::float) AS wait_median,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY (payload->>'ожидание_сек')::float) AS wait_p95,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY (payload->>'всего_сек')::float) AS total_median,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY (payload->>'всего_сек')::float) AS total_p95,
    avg((payload->>'ffmpeg_мс')::float) AS ffmpeg_ms,
    count(*) FILTER (WHERE (payload->>'всего_сек')::float > :budget) AS slow
FROM events
WHERE type = 'round_timing' AND created_at >= :since
"""

# Самая занятая минута за период: сколько кругов машина разгребала разом. Это
# ответ на «сколько человек одновременно она выдерживает».
_BUSIEST_SQL = """
SELECT coalesce(max(cnt), 0) FROM (
    SELECT count(*) AS cnt FROM events
    WHERE type = 'round_timing' AND created_at >= :since
    GROUP BY date_trunc('minute', created_at)
) AS by_minute
"""

_REFUSED_SQL = """
SELECT count(*) FROM events WHERE type = 'round_busy' AND created_at >= :since
"""


async def load_report(session: AsyncSession, hours: int = 24, queue: Any = None) -> Load:
    """Собрать сводку нагрузки за последние `hours` часов."""
    since = datetime.now(ZoneInfo(DEFAULT_TZ)) - timedelta(hours=hours)
    params = {"since": since, "budget": BUDGET_SEC}

    row = (await session.execute(text(_STATS_SQL), params)).one()
    busiest = int(await session.scalar(text(_BUSIEST_SQL), {"since": since}) or 0)
    refused = int(await session.scalar(text(_REFUSED_SQL), {"since": since}) or 0)
    средняя, ядер = cpu_load()

    report = Load(
        hours=hours,
        rounds=int(row.rounds or 0),
        wait_median=round(float(row.wait_median or 0), 2),
        wait_p95=round(float(row.wait_p95 or 0), 2),
        total_median=round(float(row.total_median or 0), 2),
        total_p95=round(float(row.total_p95 or 0), 2),
        ffmpeg_ms=round(float(row.ffmpeg_ms or 0), 1),
        slow=int(row.slow or 0),
        busiest_minute=busiest,
        refused=refused,
        queue_depth=await queue_depth(queue),
        load_avg=средняя,
        cores=ядер,
    )
    log.info(
        "нагрузка посчитана",
        часов=hours,
        кругов=report.rounds,
        ожидание_p95=report.wait_p95,
        всего_p95=report.total_p95,
        медленных=report.slow,
        очередь=report.queue_depth,
        ядер=report.cores,
        приговор=report.verdict,
    )
    return report
