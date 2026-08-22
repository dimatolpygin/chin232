"""Прогресс: стрик, календарь месяца, счётчики и динамика произношения.

Новых таблиц этап не заводит — всё уже пишется по ходу разговора: дни и
счётчики в `daily_usage` (см. `limits._bump_daily`), баллы в
`pronunciation_checks`. Здесь только чтение и арифметика.

Дни считаются в **местной дате пользователя**, той же, по которой сбрасывается
дневной лимит. Иначе стрик рвался бы у всех, кто занимается поздно вечером:
по UTC это уже завтра.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.services.limits import local_today
from app.db.models import DailyUsage, PronunciationCheck, User
from app.logging import get_logger

log = get_logger("progress")

# Сколько последних попыток берём в каждую половину сравнения. Динамика по
# всей истории бесполезна: она меняется на единицы и выглядит застывшей.
DELTA_WINDOW = 10

# Меньше четырёх попыток — сравнивать нечего: одна неудачная фраза даст «−15»
# и человек решит, что стал хуже говорить.
DELTA_MIN_CHECKS = 4


@dataclass(slots=True, frozen=True)
class Progress:
    """Всё, что показывает раздел. Считается одним заходом в базу."""

    today: date_type
    # Первое число месяца, который рисуется календарём.
    month: date_type
    # Дни этого месяца, в которые была практика.
    month_days: frozenset[date_type]
    streak: int
    best_streak: int
    messages: int
    checks: int
    # Средний балл по всем оценённым попыткам и сдвиг за последние попытки.
    score_avg: int | None
    score_delta: int | None
    days_total: int

    @property
    def empty(self) -> bool:
        """Практики не было вовсе: раздел должен объяснить, а не показать нули."""
        return self.days_total == 0

    @property
    def today_done(self) -> bool:
        return self.today in self.month_days


def _streaks(days: list[date_type], today: date_type) -> tuple[int, int]:
    """Текущий и лучший стрик по списку дней практики (по возрастанию).

    Сегодняшний день не считается пропуском, пока он не кончился: человек,
    который вчера занимался, а сегодня зашёл в раздел утром, должен увидеть
    свой стрик целым, а не обнулённым.
    """
    if not days:
        return 0, 0

    best = current = 1
    for prev, day in zip(days, days[1:], strict=False):
        current = current + 1 if (day - prev).days == 1 else 1
        best = max(best, current)

    последний = days[-1]
    if последний < today - timedelta(days=1):
        # Пропущен целый день — цепочка оборвана, но рекорд остаётся.
        return 0, best
    return current, best


def _delta(scores: list[int]) -> int | None:
    """Сдвиг среднего балла: последние попытки против предыдущих.

    `scores` — от новых к старым. Половины равной длины, иначе «динамика»
    сравнивала бы две последние попытки с сорока прошлыми.
    """
    if len(scores) < DELTA_MIN_CHECKS:
        return None
    half = min(DELTA_WINDOW, len(scores) // 2)
    свежие = scores[:half]
    прошлые = scores[half : half * 2]
    return round(sum(свежие) / half - sum(прошлые) / half)


async def load_progress(session: AsyncSession, user: User, now: datetime | None = None) -> Progress:
    """Собрать раздел целиком."""
    today = local_today(user, now)
    month = today.replace(day=1)

    rows = (
        await session.execute(
            select(DailyUsage.date)
            .where(DailyUsage.user_id == user.id)
            .where(DailyUsage.messages + DailyUsage.checks > 0)
            .order_by(DailyUsage.date)
        )
    ).scalars()
    days = list(rows)

    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(DailyUsage.messages), 0),
                func.coalesce(func.sum(DailyUsage.checks), 0),
            ).where(DailyUsage.user_id == user.id)
        )
    ).one()

    # Берём ровно две половины окна: остальное всё равно не участвует ни в
    # динамике, ни в среднем (среднее считает база по всем строкам).
    scores = list(
        (
            await session.execute(
                select(PronunciationCheck.overall)
                .where(PronunciationCheck.user_id == user.id)
                .where(PronunciationCheck.overall.is_not(None))
                .order_by(PronunciationCheck.created_at.desc())
                .limit(DELTA_WINDOW * 2)
            )
        ).scalars()
    )
    average = (
        await session.execute(
            select(func.avg(PronunciationCheck.overall))
            .where(PronunciationCheck.user_id == user.id)
            .where(PronunciationCheck.overall.is_not(None))
        )
    ).scalar()

    streak, best = _streaks(days, today)
    progress = Progress(
        today=today,
        month=month,
        month_days=frozenset(d for d in days if d.year == month.year and d.month == month.month),
        streak=streak,
        best_streak=best,
        messages=int(totals[0]),
        checks=int(totals[1]),
        score_avg=round(float(average)) if average is not None else None,
        score_delta=_delta(scores),
        days_total=len(days),
    )
    log.info(
        "прогресс собран",
        user_id=str(user.id),
        стрик=progress.streak,
        рекорд=progress.best_streak,
        дней=progress.days_total,
        сообщений=progress.messages,
        разборов=progress.checks,
        средний_балл=progress.score_avg,
    )
    return progress
