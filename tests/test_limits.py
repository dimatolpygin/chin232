"""Дневные лимиты: счётчики, пробный период, стена и напоминание."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.bot.keyboards.limits import (
    LIMIT_REMIND,
    LIMIT_SUBSCRIBE,
    limit_keyboard,
    parse_limit_action,
)
from app.bot.render import render_left
from app.bot.texts import ru
from app.core.services import limits as lim
from app.db.models import Setting, User

МОСКВА = ZoneInfo("Europe/Moscow")


class FakeSession:
    """Подмена сессии: отдаёт строку настроек и копит выполненный SQL."""

    def __init__(self, настройки: dict | None = None) -> None:
        self.row = Setting(key=lim.SETTINGS_KEY, value=настройки) if настройки is not None else None
        self.added: list[object] = []
        self.executed: list[tuple[str, dict]] = []

    async def get(self, model, key):
        if model is Setting and key == lim.SETTINGS_KEY:
            return self.row
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params or {}))
        return None

    @property
    def events(self) -> list[str]:
        return [getattr(o, "type", "") for o in self.added]


class FakeRedis:
    """Подмена redis со счётчиками, TTL и одним хешем заявок."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def decr(self, key):
        self.store[key] = self.store.get(key, 0) - 1
        return self.store[key]

    async def expire(self, key, seconds):
        self.ttl[key] = seconds

    async def get(self, key):
        value = self.store.get(key)
        return None if value is None else str(value)

    async def hset(self, name, field, value):
        self.hashes.setdefault(name, {})[field] = value

    async def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    async def hdel(self, name, field):
        self.hashes.get(name, {}).pop(field, None)


def _user(дней_назад: int = 30) -> User:
    user = User()
    user.id = uuid.uuid4()
    user.tz = "Europe/Moscow"
    user.created_at = datetime.now(UTC) - timedelta(days=дней_назад)
    return user


# --- числа лимитов из базы ---------------------------------------------------


@pytest.mark.asyncio
async def test_числа_берутся_из_базы_а_не_из_кода():
    """Критерий этапа: правка значения в базе меняет поведение без перезапуска.

    Кэша нет намеренно — иначе «без перезапуска» превращается в «через N минут
    после перезапуска кэша».
    """
    session = FakeSession({"messages": 42, "checks": 7, "trial_days": 1})
    limits = await lim.get_limits(session)
    assert limits.messages == 42
    assert limits.checks == 7
    # Незаполненные ключи берутся из значений по умолчанию, а не обнуляются.
    assert limits.trial_messages == lim.DEFAULTS["trial_messages"]


@pytest.mark.asyncio
async def test_без_строки_настроек_работают_значения_из_тз():
    limits = await lim.get_limits(FakeSession(None))
    assert (limits.messages, limits.checks, limits.trial_messages) == (10, 3, 30)


@pytest.mark.asyncio
async def test_мусор_в_настройках_не_роняет_бота():
    limits = await lim.get_limits(FakeSession({"messages": "десять"}))
    assert limits.messages == lim.DEFAULTS["messages"]


# --- пробный период ----------------------------------------------------------


def test_первые_три_дня_лимит_повышенный():
    limits = lim.Limits(**lim.DEFAULTS)
    новичок = _user(дней_назад=0)
    норма, пробный = lim.limit_for(новичок, limits, lim.KIND_MESSAGE)
    assert (норма, пробный) == (30, True)


def test_на_четвёртый_день_лимит_обычный():
    limits = lim.Limits(**lim.DEFAULTS)
    ветеран = _user(дней_назад=3)
    норма, пробный = lim.limit_for(ветеран, limits, lim.KIND_MESSAGE)
    assert (норма, пробный) == (10, False)


def test_пробный_период_считается_календарными_днями():
    """Регистрация вечером не должна съедать треть первого дня."""
    limits = lim.Limits(**lim.DEFAULTS)
    user = _user()
    user.created_at = datetime(2026, 8, 15, 23, 40, tzinfo=МОСКВА)
    вечер_регистрации = datetime(2026, 8, 15, 23, 50, tzinfo=МОСКВА)
    следующее_утро = datetime(2026, 8, 16, 9, 0, tzinfo=МОСКВА)
    третий_день = datetime(2026, 8, 17, 9, 0, tzinfo=МОСКВА)
    четвёртый_день = datetime(2026, 8, 18, 9, 0, tzinfo=МОСКВА)
    assert lim.is_trial(user, limits, вечер_регистрации)
    assert lim.is_trial(user, limits, следующее_утро)
    assert lim.is_trial(user, limits, третий_день)
    assert not lim.is_trial(user, limits, четвёртый_день)


# --- сброс в полночь ---------------------------------------------------------


def test_счётчик_живёт_ровно_до_московской_полуночи():
    """Критерий этапа: в полночь по Москве счётчик обнуляется.

    Обнуляет его сам redis по TTL, поэтому проверяем именно TTL.
    """
    user = _user()
    вечер = datetime(2026, 8, 15, 23, 0, tzinfo=МОСКВА)
    assert lim.seconds_to_midnight(user, вечер) == 3600
    утро = datetime(2026, 8, 15, 6, 0, tzinfo=МОСКВА)
    assert lim.seconds_to_midnight(user, утро) == 18 * 3600


def test_ключ_счётчика_меняется_вместе_с_местной_датой():
    user = _user()
    до = lim.usage_key(user, lim.KIND_MESSAGE, datetime(2026, 8, 15, 23, 59, tzinfo=МОСКВА))
    после = lim.usage_key(user, lim.KIND_MESSAGE, datetime(2026, 8, 16, 0, 1, tzinfo=МОСКВА))
    assert до != после
    # Redis общий с чужими проектами — префикс обязателен.
    assert до.startswith("china:")


def test_московская_полночь_наступает_по_поясу_пользователя():
    """Юзер из другого пояса не должен ждать сброса по чужим часам."""
    utc = _user()
    utc.tz = "UTC"
    момент = datetime(2026, 8, 15, 22, 30, tzinfo=МОСКВА)
    assert lim.seconds_to_midnight(utc, момент) != lim.seconds_to_midnight(_user(), момент)


def test_неизвестный_пояс_откатывается_на_москву():
    user = _user()
    user.tz = "Марс/Олимп"
    assert lim.user_zone(user) == ZoneInfo("Europe/Moscow")


# --- списание ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_каждое_действие_уменьшает_счётчик():
    session, redis, user = FakeSession(None), FakeRedis(), _user()
    первое = await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    второе = await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    assert (первое.left, второе.left) == (9, 8)
    assert первое.allowed and второе.allowed


@pytest.mark.asyncio
async def test_ttl_ставится_один_раз_при_первом_списании():
    """Иначе счётчик каждый раз начинал бы жить сутки заново и не сбрасывался."""
    session, redis, user = FakeSession(None), FakeRedis(), _user()
    await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    ttl_после_первого = dict(redis.ttl)
    redis.ttl.clear()
    await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    assert ttl_после_первого and not redis.ttl


@pytest.mark.asyncio
async def test_после_исчерпания_действие_запрещено():
    """Критерий этапа: за стеной сообщения не обрабатываются."""
    session, redis, user = FakeSession({"messages": 2}), FakeRedis(), _user()
    await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    отказ = await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    assert not отказ.allowed
    assert отказ.left == 0
    assert "limit_reached" in session.events


@pytest.mark.asyncio
async def test_счётчик_не_убегает_за_лимит_на_отказах():
    """Иначе после повышения лимита из админки юзер остался бы заперт."""
    session, redis, user = FakeSession({"messages": 1}), FakeRedis(), _user()
    await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    for _ in range(5):
        await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    assert redis.store[lim.usage_key(user, lim.KIND_MESSAGE)] == 1


@pytest.mark.asyncio
async def test_разборы_считаются_отдельным_счётчиком():
    """Критерий этапа: разбор произношения не тратит норму разговора."""
    session, redis, user = FakeSession(None), FakeRedis(), _user()
    for _ in range(3):
        await lim.consume(redis, session, user, lim.KIND_CHECK)
    разбор = await lim.consume(redis, session, user, lim.KIND_CHECK)
    сообщение = await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    assert not разбор.allowed
    assert сообщение.allowed, "разговор обязан работать после исчерпания разборов"


@pytest.mark.asyncio
async def test_расход_дублируется_в_daily_usage():
    """Критерий этапа: история расхода нужна прогрессу и статистике."""
    session, redis, user = FakeSession(None), FakeRedis(), _user()
    await lim.consume(redis, session, user, lim.KIND_CHECK)
    sql, params = session.executed[-1]
    assert "daily_usage" in sql
    assert (params["messages"], params["checks"]) == (0, 1)
    assert params["day"] == lim.local_today(user)


@pytest.mark.asyncio
async def test_отказ_не_пишется_в_историю_расхода():
    """Несостоявшееся действие не должно выглядеть в статистике как сделанное."""
    session, redis, user = FakeSession({"messages": 1}), FakeRedis(), _user()
    await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    было = len(session.executed)
    await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    assert len(session.executed) == было


@pytest.mark.asyncio
async def test_проверка_остатка_ничего_не_списывает():
    session, redis, user = FakeSession(None), FakeRedis(), _user()
    await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    первая = await lim.peek(redis, session, user, lim.KIND_MESSAGE)
    вторая = await lim.peek(redis, session, user, lim.KIND_MESSAGE)
    assert первая.used == вторая.used == 1
    assert вторая.allowed


@pytest.mark.asyncio
async def test_проверка_остатка_на_исчерпанном_счётчике_запрещает():
    session, redis, user = FakeSession({"checks": 1}), FakeRedis(), _user()
    await lim.consume(redis, session, user, lim.KIND_CHECK)
    assert not (await lim.peek(redis, session, user, lim.KIND_CHECK)).allowed


# --- отрисовка ---------------------------------------------------------------


def test_строка_остатка_показывает_и_остаток_и_норму():
    quota = lim.Quota(kind=lim.KIND_MESSAGE, used=3, limit=10, trial=False, allowed=True)
    assert render_left(quota) == "🔋 Осталось 7 из 10 на сегодня"


def test_строка_остатка_разборов_отличается_от_сообщений():
    разборы = lim.Quota(kind=lim.KIND_CHECK, used=1, limit=3, trial=False, allowed=True)
    assert "Разборов" in render_left(разборы)


def test_в_пробном_периоде_остаток_подписан():
    quota = lim.Quota(kind=lim.KIND_MESSAGE, used=0, limit=30, trial=True, allowed=True)
    assert render_left(quota).endswith(ru.LIMIT_TRIAL)


def test_стена_лимита_предлагает_подписку_и_напоминание():
    """Критерий этапа: экран с кнопками «Оформить подписку» и «Напомнить завтра»."""
    кнопки = [b for row in limit_keyboard().inline_keyboard for b in row]
    assert [b.text for b in кнопки] == [ru.BTN_SUBSCRIBE, ru.BTN_REMIND]
    assert [parse_limit_action(b.callback_data) for b in кнопки] == [
        LIMIT_SUBSCRIBE,
        LIMIT_REMIND,
    ]


def test_чужая_кнопка_не_разбирается():
    assert parse_limit_action("ans:txt:42") is None


# --- напоминание -------------------------------------------------------------


@pytest.mark.asyncio
async def test_напоминание_ждёт_смены_местного_дня():
    """Кнопка обещает «завтра» — значит сегодня звать нельзя."""
    redis, user = FakeRedis(), _user()
    session = FakeSession(None)
    await lim.ask_remind(redis, user, chat_id=555)
    assert await lim.take_due_reminders(redis, session) == []


@pytest.mark.asyncio
async def test_напоминание_уходит_на_следующий_день():
    redis, user = FakeRedis(), _user()

    class Сессия(FakeSession):
        async def get(self, model, key):
            if model is User:
                return user
            return await super().get(model, key)

    session = Сессия(None)
    await lim.ask_remind(redis, user, chat_id=555)
    # Заявка вчерашняя: местная дата уже сменилась.
    заявка = json.loads(redis.hashes[lim.remind_key()][str(user.id)])
    заявка["day"] = (lim.local_today(user) - timedelta(days=1)).isoformat()
    redis.hashes[lim.remind_key()][str(user.id)] = json.dumps(заявка)

    готовые = await lim.take_due_reminders(redis, session)
    assert готовые == [(user, 555)]
    # Заявка снимается сразу: позвать дважды хуже, чем не позвать.
    assert not redis.hashes[lim.remind_key()]
