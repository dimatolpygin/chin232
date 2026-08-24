"""Приговор нагрузке: когда пора добавлять ядра, а когда рано.

Смысл этих проверок — не в арифметике, а в границах. Порог, сдвинутый в одну
сторону, заставит клиента платить за сервер, который ему не нужен; сдвинутый в
другую — оставит людей ждать ответа по двенадцать секунд и молчать об этом.
"""

from __future__ import annotations

import pytest

from app.core.services.load import (
    VERDICT_CEILING,
    VERDICT_FREE,
    VERDICT_TIGHT,
    VERDICT_UNKNOWN,
    Load,
)


def _load(**over) -> Load:
    """Спокойный день: круги быстрые, очереди нет."""
    поля = {
        "hours": 24,
        "rounds": 200,
        "wait_median": 0.05,
        "wait_p95": 0.2,
        "total_median": 2.5,
        "total_p95": 4.0,
        "ffmpeg_ms": 300.0,
        "slow": 0,
        "busiest_minute": 3,
        "refused": 0,
        "queue_depth": 0,
        "load_avg": 0.3,
        "cores": 1,
    }
    поля.update(over)
    return Load(**поля)  # type: ignore[arg-type]


def test_на_пустой_статистике_приговора_нет():
    # Пять кругов за сутки — это не нагрузка, а случайность.
    assert _load(rounds=5).verdict == VERDICT_UNKNOWN


def test_спокойный_день_это_запас():
    assert _load().verdict == VERDICT_FREE
    assert _load().alarming is False


def test_ожидание_очереди_поднимает_тревогу_раньше_остального():
    # Круги ещё укладываются в бюджет, но реплики уже лежат в очереди — это и
    # есть ранний признак, ради которого метрика заводилась.
    плотно = _load(wait_p95=2.0)
    assert плотно.verdict == VERDICT_TIGHT
    assert _load(wait_p95=6.0).verdict == VERDICT_CEILING


def test_медленные_круги_это_потолок():
    # Каждый четвёртый ответ дольше обещанного — люди это уже чувствуют.
    отчёт = _load(rounds=100, slow=25)
    assert отчёт.slow_share == 0.25
    assert отчёт.verdict == VERDICT_CEILING
    assert отчёт.alarming is True


def test_единичные_медленные_круги_это_ещё_не_потолок():
    отчёт = _load(rounds=100, slow=6)
    assert отчёт.verdict == VERDICT_TIGHT


def test_занятый_процессор_виден_даже_при_быстрых_кругах():
    # Соседи по железу отъедают то же самое ядро: круги пока быстрые, но
    # запаса уже нет.
    assert _load(load_avg=2.5, cores=1).verdict == VERDICT_CEILING
    # Те же две с половиной на четырёх ядрах — обычная рабочая нагрузка.
    assert _load(load_avg=2.5, cores=4).verdict == VERDICT_FREE


def test_без_счётчика_загрузки_приговор_всё_равно_выносится():
    # На Windows loadavg нет, и метрика не должна из-за этого молчать.
    отчёт = _load(load_avg=None)
    assert отчёт.load_per_core is None
    assert отчёт.verdict == VERDICT_FREE


def test_доля_ffmpeg_показывает_куда_уходит_время():
    # Полсекунды конвертации в круге на две с половиной — пятая часть.
    отчёт = _load(ffmpeg_ms=500.0, total_median=2.5)
    assert отчёт.ffmpeg_share == pytest.approx(0.2)


def test_доля_ffmpeg_не_вылезает_за_единицу():
    # Медиана круга может оказаться меньше среднего времени ffmpeg на редких
    # данных; показывать «180%» на экране нельзя.
    assert _load(ffmpeg_ms=9000.0, total_median=1.0).ffmpeg_share == 1.0


async def test_длина_очереди_переживает_упавший_redis():
    from app.core.services.load import queue_depth

    class _Мёртвый:
        async def zcard(self, key):
            raise RuntimeError("connection refused")

    # Экран нагрузки не должен падать из-за того, что нечем посчитать очередь.
    assert await queue_depth(_Мёртвый()) == 0
    assert await queue_depth(None) == 0
