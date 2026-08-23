"""Админка: доступ, правка чисел, статистика, расход и рассылка.

Здесь проверяется то, чем клиент будет пользоваться без разработчика, поэтому
цена ошибки высокая в обе стороны: пустивший чужого `/admin` открывает правку
цен, а лимит, не доехавший до базы, тихо оставляет бота работать по-старому.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from app.bot.keyboards.admin import (
    SECTION_KNOB,
    SECTION_SPEND,
    admin_menu,
    knob_keyboard,
    limits_keyboard,
    parse_admin_action,
    plan_keyboard,
    segments_keyboard,
    spend_keyboard,
)
from app.bot.render import money, render_limits, render_price, render_spending, render_stats
from app.bot.texts import ru
from app.core import usage
from app.core.services import admin as adm
from app.core.services import limits as lim
from app.core.services.admin import (
    KNOBS_BY_KEY,
    SEGMENT_ACTIVE,
    SEGMENT_ALL,
    SEGMENT_FREE,
    SEGMENT_PAYING,
    SEGMENT_TITLES,
    add_admin,
    change_limit,
    change_price,
    is_admin,
    remove_admin,
)
from app.core.services.limits import KIND_MESSAGE, Limits, get_limits, limit_for
from app.core.services.stats import Spend, Summary
from app.db.models import Plan, Setting, User
from app.worker.tasks.broadcast import _elapsed, _send_one

ИСХОДНЫЕ = {
    "trial_days": 3,
    "trial_messages": 30,
    "trial_checks": 3,
    "messages": 10,
    "checks": 3,
}


class FakeSession:
    """Настройки лимитов и тарифы в памяти: ровно то, что трогает админка."""

    def __init__(self, значения: dict | None = None, тарифы: dict | None = None) -> None:
        self.row = Setting(key=lim.SETTINGS_KEY, value=dict(значения)) if значения else None
        self.тарифы = тарифы or {}
        self.added: list[object] = []

    async def get(self, model, key):
        if model is Setting:
            return self.row if self.row is not None and self.row.key == key else None
        if model is Plan:
            return self.тарифы.get(key)
        return None

    def add(self, obj) -> None:
        self.added.append(obj)
        if isinstance(obj, Setting):
            self.row = obj

    async def flush(self) -> None:
        return None

    @property
    def events(self) -> list[str]:
        return [getattr(o, "type", "") for o in self.added]


def тариф(price: str = "990", **kwargs) -> Plan:
    поля = {
        "code": "monthly",
        "title": "Подписка на месяц",
        "price": Decimal(price),
        "currency": "RUB",
        "duration_days": 30,
        "offer_id": "offer-1",
        "active": True,
        "autorenew": True,
    }
    поля.update(kwargs)
    return Plan(**поля)


@pytest.fixture(autouse=True)
def чистый_журнал():
    """Журнал расхода общий на процесс: чужие записи ломали бы соседний тест."""
    usage.take()
    yield
    usage.take()


# --- доступ --------------------------------------------------------------------


@pytest.fixture
def админы(monkeypatch):
    """Подменяет ADMIN_IDS и чинит кэш настроек за собой."""
    from app.config import get_settings

    def задать(значение: str | None):
        get_settings.cache_clear()
        monkeypatch.setenv("BOT_TOKEN", "123:AAtest")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/5")
        if значение is None:
            monkeypatch.delenv("ADMIN_IDS", raising=False)
        else:
            monkeypatch.setenv("ADMIN_IDS", значение)

    yield задать
    get_settings.cache_clear()


async def test_в_админку_пускает_только_свои_id(админы):
    админы("111, 222")
    session = FakeSession(ИСХОДНЫЕ)
    assert await is_admin(session, 111)
    assert not await is_admin(session, 333)
    # Ни пустой, ни отсутствующий id админом не считается: иначе апдейт без
    # from_user (а такие бывают) открывал бы админку.
    assert not await is_admin(session, None)
    assert not await is_admin(session, 0)


async def test_без_списка_админов_админки_нет(админы):
    админы(None)
    session = FakeSession(ИСХОДНЫЕ)
    assert not await is_admin(session, 111)


async def test_админа_можно_выдать_и_отобрать_из_бота(админы):
    админы("111")
    session = FakeSession(ИСХОДНЫЕ)

    assert await add_admin(session, 555, by=111)
    assert await is_admin(session, 555)
    assert "admin_added" in session.events
    # Повторная выдача ничего не меняет и не плодит записей.
    assert not await add_admin(session, 555, by=111)

    assert await remove_admin(session, 555, by=111)
    assert not await is_admin(session, 555)


async def test_админа_из_конфига_из_бота_не_убрать(админы):
    """Иначе один неверный /admin_del оставил бы бота без единого админа."""
    админы("111")
    session = FakeSession(ИСХОДНЫЕ)
    assert not await remove_admin(session, 111, by=111)
    assert await is_admin(session, 111)


async def test_мусор_в_списке_админов_не_роняет_проверку(админы):
    админы("111")
    session = FakeSession(ИСХОДНЫЕ)
    session.add(Setting(key=adm.ADMINS_KEY, value=["777", None, "не число"]))
    # Строку с числом принимаем, остальное пропускаем: список правится руками,
    # и одна опечатка не должна закрывать админку всем.
    assert await is_admin(session, 777)
    assert await is_admin(session, 111)


# --- разбор нажатий ------------------------------------------------------------


def test_нажатие_разбирается_вместе_со_знаком_шага():
    action = parse_admin_action("adm:lim:messages:-5")
    assert action is not None
    assert (action.section, action.value, action.delta) == (SECTION_KNOB, "messages", -5)

    открытие = parse_admin_action("adm:limits")
    assert открытие is not None
    assert открытие.delta == 0


def test_чужое_и_битое_нажатие_не_разбирается():
    assert parse_admin_action("set:voice:play") is None
    assert parse_admin_action("") is None
    # Шаг не число — вся команда отбрасывается, а не выполняется с нулём.
    assert parse_admin_action("adm:lim:messages:много") is None


# --- лимиты --------------------------------------------------------------------


async def test_лимит_меняется_и_сразу_действует_на_юзера():
    session = FakeSession(ИСХОДНЫЕ)
    user = User(hsk_level="hsk12")

    было = await get_limits(session)
    assert limit_for(user, было, KIND_MESSAGE)[0] == 10

    стало = await change_limit(session, "messages", 5)
    assert стало.messages == 15

    # Главное этого этапа: следующая же проверка лимита читает базу заново и
    # видит новое число — без перезапуска контейнера.
    перечитано = await get_limits(session)
    assert перечитано.messages == 15
    assert limit_for(user, перечитано, KIND_MESSAGE)[0] == 15
    assert "admin_limit_changed" in session.events


async def test_лимит_не_уходит_за_границы():
    session = FakeSession(ИСХОДНЫЕ)
    # Ниже нуля лимит не имеет смысла: отрицательный потолок закрыл бы бота
    # навсегда и снаружи выглядел бы поломкой.
    стало = await change_limit(session, "messages", -100)
    assert стало.messages == 0
    стало = await change_limit(session, "messages", 100000)
    assert стало.messages == KNOBS_BY_KEY["messages"].maximum


async def test_остальные_лимиты_не_сбиваются_при_правке_одного():
    session = FakeSession(ИСХОДНЫЕ)
    стало = await change_limit(session, "trial_days", 3)
    assert стало.trial_days == 6
    assert стало.messages == 10
    assert стало.checks == 3
    assert стало.trial_messages == 30


async def test_правка_неизвестного_лимита_ничего_не_ломает():
    session = FakeSession(ИСХОДНЫЕ)
    стало = await change_limit(session, "цена_вопроса", 5)
    assert стало.messages == 10
    assert session.events == []


async def test_лимиты_заводятся_если_строки_настроек_ещё_нет():
    """Пустая база — не повод отказать в правке: строка создаётся на месте."""
    session = FakeSession(None)
    стало = await change_limit(session, "checks", 2)
    assert стало.checks == 5
    assert session.row is not None
    assert session.row.value["checks"] == 5


async def test_шаг_на_месте_не_пишет_в_базу():
    session = FakeSession({**ИСХОДНЫЕ, "messages": 0})
    await change_limit(session, "messages", -5)
    # Уже ноль: событие не пишем, иначе журнал забивается пустыми правками.
    assert session.events == []


# --- цена ----------------------------------------------------------------------


async def test_цена_тарифа_меняется_из_админки():
    plan = тариф("990")
    session = FakeSession(ИСХОДНЫЕ, {"monthly": plan})
    стало = await change_price(session, "monthly", 100)
    assert стало is not None
    assert стало.price == Decimal("1090.00")
    assert "admin_price_changed" in session.events


async def test_цена_не_уходит_в_минус():
    plan = тариф("50")
    session = FakeSession(ИСХОДНЫЕ, {"monthly": plan})
    стало = await change_price(session, "monthly", -100)
    assert стало is not None
    assert стало.price == Decimal("0")


async def test_несуществующий_тариф_не_правится():
    session = FakeSession(ИСХОДНЫЕ, {})
    assert await change_price(session, "yearly", 100) is None


def test_экран_цены_предупреждает_про_оффер_платёжки():
    """Без предупреждения админ поднимет цену в боте, а спишется старая.

    Это не косметика: расхождение видно только в логах при выставлении счёта,
    а человек в это время уже платит не ту сумму, которую ему показали.
    """
    текст = render_price([тариф("990")])
    assert "lava.top" in текст
    assert "990" in текст


def test_тариф_без_оффера_помечен():
    текст = render_price([тариф("990", offer_id=None)])
    assert ru.ADMIN_PRICE_NO_OFFER.strip() in текст


# --- экраны --------------------------------------------------------------------


def test_на_кнопках_лимитов_стоят_текущие_числа():
    клавиатура = limits_keyboard(Limits(**ИСХОДНЫЕ))
    подписи = [b.text for row in клавиатура.inline_keyboard for b in row]
    assert any("Сообщений в день: 10" in подпись for подпись in подписи)
    assert any("Пробный период: 3" in подпись for подпись in подписи)


def test_ряд_правки_числа_даёт_оба_шага_в_обе_стороны():
    knob = KNOBS_BY_KEY["messages"]
    ряд = knob_keyboard(knob, 10).inline_keyboard[0]
    шаги = [b.callback_data for b in ряд]
    assert f"adm:lim:messages:-{knob.big}" in шаги
    assert f"adm:lim:messages:-{knob.step}" in шаги
    assert f"adm:lim:messages:{knob.step}" in шаги
    assert f"adm:lim:messages:{knob.big}" in шаги
    # Средняя кнопка показывает значение и намеренно ничего не делает.
    assert ряд[2].text.startswith("10")
    assert ряд[2].callback_data == "adm:noop"


def test_нулевой_лимит_объясняется_прямо_на_экране():
    текст = render_limits(Limits(**{**ИСХОДНЫЕ, "messages": 0}))
    assert ru.ADMIN_LIMITS_ZERO in текст
    assert ru.ADMIN_LIMITS_ZERO not in render_limits(Limits(**ИСХОДНЫЕ))


def test_статистика_считает_конверсию_и_склоняет_счётчики():
    summary = Summary(
        users_total=200,
        users_today=3,
        users_week=11,
        active_today=12,
        active_week=40,
        paying=7,
        messages_today=21,
        checks_today=1,
        revenue={"RUB": Decimal("6930")},
    )
    assert summary.conversion == pytest.approx(3.5)
    текст = render_stats(summary)
    assert "3.5%" in текст
    assert "21 сообщение" in текст
    assert "1 разбор" in текст
    assert "6 930 RUB" in текст


def test_статистика_на_пустой_базе_не_делит_на_ноль():
    summary = Summary(0, 0, 0, 0, 0, 0, 0, 0, {})
    assert summary.conversion == 0.0
    assert ru.ADMIN_STATS_NO_REVENUE in render_stats(summary)


def test_расход_показан_по_каждому_сервису_с_итогом():
    текст = render_spending(
        [
            Spend("speechsuper", 12, 1, 12, "запросов", 0.072),
            Spend("openrouter", 40, 0, 51000, "токенов", 0.0138),
        ],
        7,
    )
    assert "Оценка (SpeechSuper)" in текст
    assert "Диалог (OpenRouter)" in текст
    assert "ошибок 1" in текст
    # Итог — сумма показанных строк, а не отдельный запрос: расхождение между
    # строками и итогом читается как ошибка счёта.
    assert "0.0858" in текст
    assert "за 7 дней" in текст


def test_пустой_расход_не_рисует_таблицу_из_нулей():
    assert ru.ADMIN_SPEND_EMPTY in render_spending([], 1)


def test_центы_не_округляются_в_ноль():
    """$0.00 вместо $0.0184 выглядит как сломанный счётчик, а не как «дёшево»."""
    assert money(0.0184) == "0.0184"
    assert money(12.5) == "12.50"
    assert money(0) == "0.00"


def test_периоды_расхода_переключаются_и_отмечены():
    ряд = spend_keyboard(7).inline_keyboard[0]
    assert [b.callback_data for b in ряд] == [
        f"adm:{SECTION_SPEND}:1",
        f"adm:{SECTION_SPEND}:7",
        f"adm:{SECTION_SPEND}:30",
    ]
    assert ряд[1].text.startswith("✅")


def test_в_главном_меню_есть_все_разделы():
    подписи = [b.text for row in admin_menu().inline_keyboard for b in row]
    assert set(подписи) == {
        ru.BTN_ADMIN_STATS,
        ru.BTN_ADMIN_SPEND,
        ru.BTN_ADMIN_LIMITS,
        ru.BTN_ADMIN_PRICE,
        ru.BTN_ADMIN_BROADCAST,
        ru.BTN_ADMIN_ADMINS,
    }


def test_ряд_правки_цены_помнит_код_тарифа():
    ряд = plan_keyboard(тариф("990")).inline_keyboard[0]
    assert all("monthly" in b.callback_data for b in ряд if b.callback_data != "adm:noop")


# --- рассылка ------------------------------------------------------------------


def test_на_кнопках_сегментов_видно_число_адресатов():
    """Число обязано быть до выбора: «всем» и «платящим» — разные решения."""
    клавиатура = segments_keyboard({SEGMENT_ALL: 128, SEGMENT_PAYING: 7})
    подписи = [b.text for row in клавиатура.inline_keyboard for b in row]
    assert any(подпись.endswith("· 128") for подпись in подписи)
    assert any(подпись.endswith("· 7") for подпись in подписи)
    # Сегмент без счёта показывает ноль, а не пустоту: пустое место читается
    # как «считаем», и админ ждёт.
    assert any(подпись.endswith("· 0") for подпись in подписи)


def test_у_каждого_сегмента_свой_отбор():
    все = str(adm._audience_stmt(SEGMENT_ALL))
    платящие = str(adm._audience_stmt(SEGMENT_PAYING))
    бесплатные = str(adm._audience_stmt(SEGMENT_FREE))
    активные = str(adm._audience_stmt(SEGMENT_ACTIVE))

    assert "subscriptions" not in все
    assert "subscriptions" in платящие and "NOT IN" not in платящие.upper()
    assert "subscriptions" in бесплатные and "NOT IN" in бесплатные.upper()
    assert "daily_usage" in активные
    # Адрес всегда берётся из identities: без него слать некуда.
    assert all("identities" in sql for sql in (все, платящие, бесплатные, активные))


def test_у_всех_сегментов_есть_человеческое_название():
    assert set(SEGMENT_TITLES) == {SEGMENT_ALL, SEGMENT_PAYING, SEGMENT_FREE, SEGMENT_ACTIVE}
    assert all(SEGMENT_TITLES.values())


class FakeBot:
    """Бот, который отвечает на отправку тем, что ему велели."""

    def __init__(self, поведение: list) -> None:
        self.поведение = list(поведение)
        self.отправлено: list[int] = []

    async def send_message(self, chat_id, text, **kwargs):
        исход = self.поведение.pop(0) if self.поведение else None
        if isinstance(исход, Exception):
            raise исход
        self.отправлено.append(chat_id)
        return True


async def test_заблокировавший_бота_не_считается_ошибкой(monkeypatch):
    method = SendMessage(chat_id=1, text="x")
    bot = FakeBot([TelegramForbiddenError(method=method, message="bot was blocked")])
    assert await _send_one(bot, 1, "привет") == "blocked"


async def test_просьбу_подождать_рассылка_уважает(monkeypatch):
    """Игнорировать retry_after нельзя: телеграм закроет бота целиком, вместе
    с ответами живым людям."""
    паузы: list[float] = []

    async def подождать(seconds):
        паузы.append(seconds)

    monkeypatch.setattr("app.worker.tasks.broadcast.asyncio.sleep", подождать)
    method = SendMessage(chat_id=1, text="x")
    bot = FakeBot([TelegramRetryAfter(method=method, message="flood", retry_after=7)])

    assert await _send_one(bot, 1, "привет") == "sent"
    assert паузы and паузы[0] >= 7
    assert bot.отправлено == [1]


async def test_ошибка_одного_адресата_не_роняет_рассылку():
    bot = FakeBot([RuntimeError("сеть отвалилась")])
    assert await _send_one(bot, 1, "привет") == "failed"


def test_длительность_рассылки_читается_человеком():
    assert _elapsed(41) == "41 с"
    assert _elapsed(133) == "2 мин 13 с"


# --- расход на сервисы ---------------------------------------------------------


def test_стоимость_считается_по_единицам_каждого_сервиса():
    # Диалог платит за токены, распознавание — за секунды звука, озвучка — за
    # знаки, оценка — за сам факт запроса. Ни один не считается «за вызов».
    диалог = usage.estimate("openrouter", {"токенов_вход": 1000, "токенов_выход": 500})
    assert диалог.unit == usage.UNIT_TOKENS
    assert диалог.units == 1500
    assert диалог.cost == pytest.approx(1000 * usage.PRICE_LLM_INPUT + 500 * usage.PRICE_LLM_OUTPUT)

    слух = usage.estimate("openai_whisper", {"секунд": 30})
    assert слух.unit == usage.UNIT_SECONDS
    assert слух.cost == pytest.approx(30 * usage.PRICE_WHISPER_SEC)

    голос = usage.estimate("fish", {"знаков": 20})
    assert голос.unit == usage.UNIT_CHARS
    # Иероглиф — три байта UTF-8, а платим мы за байты.
    assert голос.cost == pytest.approx(20 * usage.BYTES_PER_HANZI * usage.PRICE_FISH_BYTE)

    оценка = usage.estimate("speechsuper", {})
    assert оценка.cost == pytest.approx(usage.PRICE_SPEECHSUPER_CALL)


def test_сорвавшийся_вызов_считается_бесплатным_но_считается():
    """У неудачного вызова нет токенов, но сам вызов должен быть виден."""
    цена = usage.estimate("openrouter", {"http_код": 500})
    assert цена.cost == 0
    assert цена.units == 0


def test_незнакомый_сервис_всё_равно_попадает_в_журнал():
    цена = usage.estimate("новый_сервис", {})
    assert цена.unit == usage.UNIT_CALLS
    assert цена.cost == 0


def test_журнал_копится_и_забирается_целиком():
    usage.note("speechsuper", "pronunciation", True, 640, {})
    usage.note("fish", "tts", True, 900, {"знаков": 10})
    assert usage.pending_count() == 2

    забрано = usage.take()
    assert [c.provider for c in забрано] == ["speechsuper", "fish"]
    assert usage.pending_count() == 0

    # Сессия откатилась — записи возвращаются, а не пропадают: деньги за эти
    # вызовы уже потрачены.
    usage.give_back(забрано)
    assert usage.pending_count() == 2


def test_журнал_не_растёт_бесконечно(monkeypatch):
    """База может лежать часами, а память процесса — нет."""
    monkeypatch.setattr(usage, "MAX_PENDING", 3)
    for _ in range(5):
        usage.note("fish", "tts", True, 10, {"знаков": 1})
    assert usage.pending_count() == 3
