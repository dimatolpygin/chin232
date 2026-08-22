"""Раздел «Прогресс»: серия дней, календарь, счётчики и динамика произношения.

Проверяем не «функция вернула число», а то, что число означает: серия должна
рваться ровно при пропущенном дне и переживать сегодняшнее утро, календарь —
не закрашивать будущее, а пустая история — выглядеть приглашением, а не нулями.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.bot.keyboards.menu import main_menu
from app.bot.render import calendar_rows, plural, render_progress
from app.bot.texts import ru
from app.core.services.progress import (
    DELTA_MIN_CHECKS,
    Progress,
    _delta,
    _streaks,
    load_progress,
)
from app.db.models import User

СЕГОДНЯ = date(2026, 8, 22)


def _user(tz: str = "Europe/Moscow") -> User:
    user = User()
    user.id = uuid.uuid4()
    user.tz = tz
    user.created_at = datetime(2026, 8, 1, tzinfo=UTC)
    return user


def _прогресс(**поля) -> Progress:
    базовые = {
        "today": СЕГОДНЯ,
        "month": СЕГОДНЯ.replace(day=1),
        "month_days": frozenset(),
        "streak": 0,
        "best_streak": 0,
        "messages": 0,
        "checks": 0,
        "score_avg": None,
        "score_delta": None,
        "days_total": 0,
    }
    return Progress(**{**базовые, **поля})


def _дни(*числа: int) -> list[date]:
    return [date(2026, 8, n) for n in числа]


# --- серия дней ---------------------------------------------------------------


def test_серия_растёт_днями_подряд():
    текущая, лучшая = _streaks(_дни(20, 21, 22), СЕГОДНЯ)
    assert текущая == 3
    assert лучшая == 3


def test_сегодняшнее_утро_не_рвёт_серию():
    """Зашёл в раздел до занятия — серия должна быть цела, а не обнулена.

    Иначе бот сам сообщает «всё пропало» человеку, у которого впереди целый
    день, и отбивает желание возвращаться.
    """
    текущая, _ = _streaks(_дни(19, 20, 21), СЕГОДНЯ)
    assert текущая == 3


def test_пропущенный_день_обнуляет_серию():
    текущая, лучшая = _streaks(_дни(18, 19, 20), СЕГОДНЯ)
    assert текущая == 0
    # Рекорд пропуском не отменяется: он про то, что человек уже смог.
    assert лучшая == 3


def test_рекорд_считается_по_всей_истории():
    текущая, лучшая = _streaks(_дни(1, 2, 3, 4, 5, 10, 21, 22), СЕГОДНЯ)
    assert текущая == 2
    assert лучшая == 5


def test_пустая_история_даёт_нули():
    assert _streaks([], СЕГОДНЯ) == (0, 0)


def test_один_день_это_серия_из_одного():
    assert _streaks(_дни(22), СЕГОДНЯ) == (1, 1)


# --- динамика произношения ----------------------------------------------------


def test_динамика_молчит_пока_попыток_мало():
    """Две попытки против двух — это шум, а не прогресс."""
    assert _delta([80] * (DELTA_MIN_CHECKS - 1)) is None


def test_динамика_сравнивает_свежие_попытки_с_прошлыми():
    # Список от новых к старым: свежие 90, прошлые 70.
    assert _delta([90, 90, 70, 70]) == 20
    assert _delta([70, 70, 90, 90]) == -20


def test_ровные_баллы_дают_нулевую_динамику():
    assert _delta([75] * 8) == 0


def test_половины_динамики_одной_длины():
    """Иначе три свежие попытки сравнивались бы с сорока прошлыми."""
    # Пять значений: последнее лишнее и в сравнение не идёт.
    assert _delta([100, 100, 50, 50, 0]) == 50


# --- календарь ----------------------------------------------------------------


def test_календарь_рисует_недели_по_семь_дней():
    строки = calendar_rows(_прогресс(month_days=frozenset(_дни(3, 4))))
    assert строки[0].startswith("01–07")
    assert строки[1].startswith("08–14")
    assert len(строки) == 4  # 22-е число — четвёртая неполная неделя
    квадраты = строки[0].split()[-1]
    assert len(квадраты) == 7
    assert квадраты[2] == ru.PROGRESS_DAY_DONE
    assert квадраты[0] == ru.PROGRESS_DAY_MISS


def test_календарь_не_закрашивает_будущее():
    """Месяц рисуется до сегодняшнего дня: 25-е ещё не пропущено, его не было."""
    строки = calendar_rows(_прогресс(month_days=frozenset(_дни(22))))
    assert строки[-1].startswith("22–22")
    assert строки[-1].endswith(ru.PROGRESS_DAY_DONE)


def test_календарь_первого_числа_не_падает():
    прогресс = _прогресс(today=date(2026, 9, 1), month=date(2026, 9, 1))
    assert calendar_rows(прогресс) == [f"01–01  {ru.PROGRESS_DAY_MISS}"]


# --- экран --------------------------------------------------------------------


def test_пустой_раздел_объясняет_а_не_показывает_нули():
    экран = render_progress(_прогресс())
    assert экран == ru.PROGRESS_EMPTY
    assert "0" not in экран


def test_экран_показывает_серию_календарь_счётчики_и_балл():
    экран = render_progress(
        _прогресс(
            month_days=frozenset(_дни(20, 21, 22)),
            streak=3,
            best_streak=5,
            messages=128,
            checks=14,
            score_avg=78,
            score_delta=6,
            days_total=12,
        )
    )
    assert "3 дня подряд" in экран
    assert "Лучшая серия: 5 дней" in экран
    assert "Август 2026" in экран
    assert "128 сообщений" in экран
    assert "14 разборов" in экран
    assert "78" in экран and "+6" in экран
    assert ru.PROGRESS_TODAY_LEFT not in экран


def test_экран_зовёт_вернуться_если_сегодня_ещё_не_занимались():
    экран = render_progress(_прогресс(month_days=frozenset(_дни(20, 21)), streak=2, days_total=2))
    assert ru.PROGRESS_TODAY_LEFT in экран


def test_прерванная_серия_не_показывает_ноль_дней():
    экран = render_progress(_прогресс(streak=0, best_streak=4, days_total=4))
    assert "0 дней подряд" not in экран
    assert ru.PROGRESS_STREAK_NONE in экран
    assert "Лучшая серия: 4 дня" in экран


def test_рекорд_не_дублирует_текущую_серию():
    экран = render_progress(
        _прогресс(streak=5, best_streak=5, days_total=5, month_days=frozenset(_дни(22)))
    )
    assert "Лучшая серия" not in экран


def test_без_оценок_экран_зовёт_нажать_оценку():
    экран = render_progress(_прогресс(days_total=1, month_days=frozenset(_дни(22))))
    assert ru.PROGRESS_SCORE_NONE in экран


@pytest.mark.parametrize(
    ("delta", "признак"),
    [(6, "↗"), (-6, "↘"), (0, "→")],
)
def test_динамика_видна_на_экране(delta: int, признак: str):
    экран = render_progress(
        _прогресс(days_total=3, score_avg=80, score_delta=delta, month_days=frozenset(_дни(22)))
    )
    assert признак in экран
    # Минус рисуется знаком, а не как «↘ -6» с дефисом.
    assert "-6" not in экран


def test_склонение_дней():
    assert plural(1, "день", "дня", "дней") == "1 день"
    assert plural(3, "день", "дня", "дней") == "3 дня"
    assert plural(5, "день", "дня", "дней") == "5 дней"
    # 11–14 — исключение, иначе «11 день».
    assert plural(11, "день", "дня", "дней") == "11 дней"
    assert plural(21, "день", "дня", "дней") == "21 день"
    assert plural(112, "день", "дня", "дней") == "112 дней"


def test_кнопка_прогресса_есть_в_нижнем_меню():
    ряды = [[b.text for b in row] for row in main_menu().keyboard]
    assert ru.MENU_PROGRESS in ряды[1]


# --- сборка из базы -----------------------------------------------------------


class FakeResult:
    def __init__(self, значения) -> None:
        self._значения = значения

    def scalars(self):
        return self._значения

    def one(self):
        return self._значения

    def scalar(self):
        return self._значения


class FakeSession:
    """Отвечает на четыре запроса раздела, различая их по тексту SQL."""

    def __init__(self, дни, сообщения=0, разборы=0, баллы=(), среднее=None) -> None:
        self.дни = дни
        self.итоги = (сообщения, разборы)
        self.баллы = list(баллы)
        self.среднее = среднее

    async def execute(self, statement):
        sql = str(statement)
        if "avg(" in sql:
            return FakeResult(self.среднее)
        if "sum(" in sql:
            return FakeResult(self.итоги)
        if "pronunciation_checks" in sql:
            return FakeResult(self.баллы)
        return FakeResult(self.дни)


async def test_прогресс_собирается_из_базы(monkeypatch):
    monkeypatch.setattr("app.core.services.progress.local_today", lambda user, now=None: СЕГОДНЯ)
    session = FakeSession(
        дни=[date(2026, 7, 30)] + _дни(1, 20, 21, 22),
        сообщения=128,
        разборы=14,
        баллы=[90, 90, 90, 70, 70, 70],
        среднее=80.4,
    )
    прогресс = await load_progress(session, _user())

    assert прогресс.streak == 3
    assert прогресс.best_streak == 3
    assert прогресс.messages == 128
    assert прогресс.checks == 14
    assert прогресс.score_avg == 80
    assert прогресс.score_delta == 20
    assert прогресс.days_total == 5
    # В календарь текущего месяца июльский день попасть не должен.
    assert прогресс.month_days == frozenset(_дни(1, 20, 21, 22))
    assert прогресс.today_done is True
    assert прогресс.empty is False


async def test_новичок_без_истории_даёт_пустой_прогресс(monkeypatch):
    monkeypatch.setattr("app.core.services.progress.local_today", lambda user, now=None: СЕГОДНЯ)
    прогресс = await load_progress(FakeSession(дни=[]), _user())
    assert прогресс.empty is True
    assert прогресс.score_avg is None
    assert render_progress(прогресс) == ru.PROGRESS_EMPTY


async def test_день_считается_по_часовому_поясу_пользователя():
    """Владивосток в 01:00 — уже завтра, и серия не должна считаться рваной."""
    user = _user("Asia/Vladivostok")
    ночь = datetime(2026, 8, 22, 16, 0, tzinfo=UTC)  # 02:00 23-го во Владивостоке
    session = FakeSession(дни=_дни(21, 22))
    прогресс = await load_progress(session, user, now=ночь)
    assert прогресс.today == date(2026, 8, 23)
    # Вчера занимался — серия цела, хотя сегодня ещё нет.
    assert прогресс.streak == 2
    assert прогресс.today_done is False


def test_серия_через_месяц_не_рвётся():
    дни = [date(2026, 7, 31), date(2026, 8, 1)]
    assert _streaks(дни, date(2026, 8, 1)) == (2, 2)
    assert _streaks(дни, date(2026, 8, 2))[0] == 2
    assert _streaks(дни, date(2026, 8, 3))[0] == 0


def test_первый_день_серии_читается_по_русски():
    """«1 день подряд» — не по-русски, у первого дня своя строка."""
    экран = render_progress(
        _прогресс(streak=1, best_streak=1, days_total=1, month_days=frozenset(_дни(22)))
    )
    assert "1 день подряд" not in экран
    assert ru.PROGRESS_STREAK_ONE in экран
