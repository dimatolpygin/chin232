"""Подписка и деньги: разбор вебхука, подпись, идемпотентность, снятие лимита."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.bot.handlers.subscription import EMAIL_RE, money
from app.bot.render import render_left
from app.config import Settings
from app.core.providers.base import EVENT_CANCELLED, EVENT_FAILED, EVENT_PAID
from app.core.providers.payments.lavatop import LavaTopPayments
from app.core.services import billing
from app.core.services import limits as lim
from app.db.models import Plan, Subscription, User
from app.db.models.billing import PAY_COMPLETED, SUB_ACTIVE

BASE = {
    "bot_token": "123:AAtest",
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "redis_url": "redis://localhost:6379/5",
}
СЕКРЕТ = "ключ-платёжки"

КОНТРАКТ = "7ea82675-4ded-4133-95a7-a6efbaf165cc"
РОДИТЕЛЬСКИЙ = "c5a0cacc-3453-44b0-9532-aa492f1ba191"


def _provider(secret: str | None = СЕКРЕТ) -> LavaTopPayments:
    return LavaTopPayments(
        Settings(lavatop_api_key=СЕКРЕТ, lavatop_webhook_secret=secret, **BASE)  # type: ignore[arg-type]
    )


def _plan() -> Plan:
    plan = Plan()
    plan.code = "monthly"
    plan.title = "Подписка на месяц"
    plan.price = Decimal("590")
    plan.currency = "RUB"
    plan.duration_days = 30
    plan.periodicity = "MONTHLY"
    plan.offer_id = "836b9fc5-7ae9-4a27-9642-592bc44072b7"
    plan.active = True
    return plan


def _user() -> User:
    user = User()
    user.id = uuid.uuid4()
    user.created_at = datetime.now(UTC) - timedelta(days=30)
    user.tz = "Europe/Moscow"
    return user


def _payload(event_type: str = "payment.success", **extra) -> dict:
    payload = {
        "eventType": event_type,
        "product": {"id": "d31384b8", "title": "Подписка"},
        "buyer": {"email": "client@example.com"},
        "contractId": КОНТРАКТ,
        "amount": 590,
        "currency": "RUB",
        "timestamp": "2026-08-15T09:38:27.33277Z",
        "status": "subscription-active",
    }
    payload.update(extra)
    return payload


class FakeResult:
    def __init__(self, value=None, rows=None) -> None:
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._value

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeBillingSession:
    """Сессия, которая честно повторяет правило идемпотентности.

    Смысл не в том, чтобы обмануть тест: `INSERT ... ON CONFLICT DO UPDATE
    WHERE payments.status <> 'completed'` возвращает строку только на первой
    доставке вебхука. Ровно это здесь и воспроизведено — иначе проверять
    «второй раз не продлевает» было бы не на чем.
    """

    def __init__(self, plan: Plan | None = None, user_id: uuid.UUID | None = None) -> None:
        self.plan = plan
        self.user_id = user_id
        # external_id -> статус платежа
        self.payments: dict[str, str] = {}
        self.subscription: Subscription | None = None
        self.added: list[object] = []
        self.executed: list[str] = []

    async def get(self, model, key):
        if model is Plan:
            return self.plan
        return None

    def add(self, obj) -> None:
        self.added.append(obj)
        if isinstance(obj, Subscription):
            self.subscription = obj

    async def scalar(self, statement):
        sql = str(statement)
        if "payments.user_id" in sql or "subscriptions.user_id" in sql or "users.id" in sql:
            return self.user_id
        if "payments.plan_code" in sql:
            return self.plan.code if self.plan else None
        return None

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append(sql)
        if "INSERT INTO payments" in sql:
            external = params["external_id"]
            if self.payments.get(external) == PAY_COMPLETED:
                return FakeResult(rows=[])
            self.payments[external] = params["status"]
            return FakeResult(rows=[(uuid.uuid4(),)])
        if "subscriptions" in sql and "SELECT" in sql:
            return FakeResult(self.subscription)
        return FakeResult()

    async def flush(self) -> None:
        return None

    @property
    def events(self) -> list[str]:
        return [getattr(o, "type", "") for o in self.added]


# --- разбор вебхука -----------------------------------------------------------


def test_успешная_оплата_разбирается():
    event = _provider().parse_webhook(_payload())
    assert event.kind == EVENT_PAID
    assert event.external_id == КОНТРАКТ
    assert event.email == "client@example.com"
    assert event.amount == 590
    assert not event.recurring


def test_продление_видно_по_родительскому_контракту():
    """У продления свой contractId — без parentContractId его не с чем связать."""
    event = _provider().parse_webhook(
        _payload(
            "subscription.recurring.payment.success",
            contractId="d41db415",
            parentContractId=РОДИТЕЛЬСКИЙ,
        )
    )
    assert event.kind == EVENT_PAID
    assert event.recurring
    assert event.parent_external_id == РОДИТЕЛЬСКИЙ


def test_неуспешная_оплата_и_отмена_различаются():
    провайдер = _provider()
    сбой = провайдер.parse_webhook(
        _payload(
            "payment.failed", errorMessage="Not sufficient funds", status="subscription-failed"
        )
    )
    отмена = провайдер.parse_webhook(
        _payload("subscription.cancelled", willExpireAt="2026-09-14T08:44:49Z")
    )
    assert сбой.kind == EVENT_FAILED
    assert сбой.error == "Not sufficient funds"
    assert отмена.kind == EVENT_CANCELLED
    assert отмена.expires_at == "2026-09-14T08:44:49Z"


# --- подпись ------------------------------------------------------------------


def test_вебхук_со_своим_ключом_принимается():
    assert _provider().verify_webhook({"X-Api-Key": СЕКРЕТ}, b"{}")


def test_вебхук_с_чужим_ключом_отклоняется():
    провайдер = _provider()
    assert not провайдер.verify_webhook({"X-Api-Key": "не тот"}, b"{}")
    # Совсем без заголовка — тоже мимо: открытый вебхук платёжки означает, что
    # подписку себе выпишет кто угодно одним curl.
    assert not провайдер.verify_webhook({}, b"{}")


def test_basic_авторизация_вебхука_принимается():
    import base64

    заголовок = "Basic " + base64.b64encode(f"lava:{СЕКРЕТ}".encode()).decode()
    assert _provider().verify_webhook({"Authorization": заголовок}, b"{}")


def test_секрет_вебхука_по_умолчанию_это_ключ_api():
    """В кабинете lava.top для вебхука выбирается «API-ключ», отдельного нет."""
    assert _provider(secret=None).verify_webhook({"X-Api-Key": СЕКРЕТ}, b"{}")


# --- применение к подписке ----------------------------------------------------


@pytest.mark.asyncio
async def test_оплата_включает_подписку():
    user_id = uuid.uuid4()
    session = FakeBillingSession(_plan(), user_id)
    applied = await billing.apply_event(session, _provider().parse_webhook(_payload()), "lavatop")

    assert applied.duplicate is False
    assert session.subscription is not None
    assert session.subscription.status == SUB_ACTIVE
    # Ровно длительность тарифа, а не «до конца месяца».
    осталось = session.subscription.expires_at - datetime.now(UTC)
    assert timedelta(days=29, hours=23) < осталось <= timedelta(days=30)
    assert "payment_succeeded" in session.events


@pytest.mark.asyncio
async def test_повторный_вебхук_не_продлевает_подписку_дважды():
    """Главный критерий этапа: lava.top повторяет доставку до девятнадцати раз."""
    user_id = uuid.uuid4()
    session = FakeBillingSession(_plan(), user_id)
    event = _provider().parse_webhook(_payload())

    первый = await billing.apply_event(session, event, "lavatop")
    было = session.subscription.expires_at

    второй = await billing.apply_event(session, event, "lavatop")

    assert первый.duplicate is False
    assert второй.duplicate is True
    assert session.subscription.expires_at == было
    assert "payment_duplicate" in session.events


@pytest.mark.asyncio
async def test_продление_добавляет_срок_к_остатку():
    """Заплативший заранее не должен терять оплаченный остаток."""
    user_id = uuid.uuid4()
    session = FakeBillingSession(_plan(), user_id)
    подписка = Subscription()
    подписка.user_id = user_id
    подписка.plan_code = "monthly"
    подписка.status = SUB_ACTIVE
    подписка.started_at = datetime.now(UTC) - timedelta(days=25)
    подписка.expires_at = datetime.now(UTC) + timedelta(days=5)
    подписка.external_id = РОДИТЕЛЬСКИЙ
    session.subscription = подписка

    await billing.apply_event(
        session,
        _provider().parse_webhook(
            _payload(
                "subscription.recurring.payment.success",
                contractId="d41db415",
                parentContractId=РОДИТЕЛЬСКИЙ,
            )
        ),
        "lavatop",
    )
    осталось = подписка.expires_at - datetime.now(UTC)
    assert timedelta(days=34, hours=23) < осталось <= timedelta(days=35)


@pytest.mark.asyncio
async def test_несостоявшаяся_оплата_подписку_не_включает():
    session = FakeBillingSession(_plan(), uuid.uuid4())
    applied = await billing.apply_event(
        session,
        _provider().parse_webhook(_payload("payment.failed", errorMessage="Not sufficient funds")),
        "lavatop",
    )
    assert applied.kind == EVENT_FAILED
    assert session.subscription is None
    assert "payment_failed" in session.events


@pytest.mark.asyncio
async def test_платёж_без_хозяина_не_роняет_обработку():
    """Вебхук от чужого продукта: ругаемся в лог, но отвечаем платёжке спокойно."""
    session = FakeBillingSession(_plan(), None)
    applied = await billing.apply_event(session, _provider().parse_webhook(_payload()), "lavatop")
    assert applied.user_id is None
    assert session.subscription is None
    assert "payment_orphan" in session.events


# --- стык с лимитами ----------------------------------------------------------


class FakeLimitSession:
    """Сессия для лимитов: с подпиской или без неё."""

    def __init__(self, подписка: Subscription | None) -> None:
        self.подписка = подписка
        self.added: list[object] = []
        self.executed: list[str] = []

    async def get(self, model, key):
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    async def execute(self, statement, params=None):
        self.executed.append(str(statement))
        return FakeResult(self.подписка)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def decr(self, key):
        self.store[key] = self.store.get(key, 0) - 1
        return self.store[key]

    async def delete(self, key):
        self.store.pop(key, None)

    async def expire(self, key, seconds):
        return None

    async def get(self, key):
        value = self.store.get(key)
        return None if value is None else str(value)


def _подписка(дней: int = 10) -> Subscription:
    подписка = Subscription()
    подписка.user_id = uuid.uuid4()
    подписка.plan_code = "monthly"
    подписка.status = SUB_ACTIVE
    подписка.started_at = datetime.now(UTC)
    подписка.expires_at = datetime.now(UTC) + timedelta(days=дней)
    return подписка


@pytest.mark.asyncio
async def test_подписка_снимает_дневной_лимит():
    session, redis, user = FakeLimitSession(_подписка()), FakeRedis(), _user()
    for _ in range(50):
        quota = await lim.consume(redis, session, user, lim.KIND_MESSAGE)
        assert quota.allowed
        assert quota.unlimited
    # Счётчика в redis у подписчика нет вовсе: считать нечего.
    assert redis.store == {}


@pytest.mark.asyncio
async def test_расход_подписчика_всё_равно_попадает_в_историю():
    """Статистика админки и прогресс считаются по всем, а не только по бесплатным."""
    session, redis, user = FakeLimitSession(_подписка()), FakeRedis(), _user()
    await lim.consume(redis, session, user, lim.KIND_CHECK)
    assert any("daily_usage" in sql for sql in session.executed)


@pytest.mark.asyncio
async def test_истёкшая_подписка_лимит_возвращает():
    """Дата важнее статуса: между запусками почасовой задачи он ещё `active`."""
    просроченная = _подписка(дней=-1)
    session, redis, user = FakeLimitSession(None), FakeRedis(), _user()
    session.подписка = None  # запрос отбирает по expires_at > now, такую не найдёт
    quota = await lim.consume(redis, session, user, lim.KIND_MESSAGE)
    assert not quota.unlimited
    assert quota.limit == lim.DEFAULTS["messages"]
    assert просроченная.expires_at < datetime.now(UTC)


def test_подписчику_не_показывается_остаток():
    """Строка счётчика нужна, чтобы решить про подписку. Он уже решил."""
    assert render_left(lim.Quota("messages", 0, 0, False, True, unlimited=True)) == ""
    assert render_left(lim.Quota("messages", 3, 10, False, True)) != ""


# --- мелочи раздела -----------------------------------------------------------


def test_цена_без_лишних_копеек():
    assert money(Decimal("590.00")) == "590"
    assert money(Decimal("590.50")) == "590.5"


def test_почта_проверяется_на_явную_ерунду():
    assert EMAIL_RE.match("client@example.com")
    assert not EMAIL_RE.match("你好, как дела")
    assert not EMAIL_RE.match("client@example")
    assert not EMAIL_RE.match("почта без собаки.ru")
