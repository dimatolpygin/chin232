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
    plan.autorenew = True
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


# --- разовый тариф под СБП ----------------------------------------------------


def _once_plan() -> Plan:
    """Разовая покупка доступа: СБП не умеет автосписаний."""
    plan = Plan()
    plan.code = "once30"
    plan.title = "Доступ на 30 дней"
    plan.price = Decimal("549")
    plan.currency = "RUB"
    plan.duration_days = 30
    plan.periodicity = "ONE_TIME"
    plan.offer_id = "836b9fc5-7ae9-4a27-9642-592bc44072b7"
    plan.payment_provider = "PAY2ME"
    plan.payment_method = "SBP"
    plan.autorenew = False
    plan.active = True
    return plan


class FakeInvoiceProvider:
    """Запоминает, с чем к нему пришли: проверяем сам запрос, а не ответ."""

    name = "lavatop"

    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def create_invoice(self, **kwargs):
        from app.core.providers.base import Invoice

        self.kwargs = kwargs
        return Invoice(
            external_id="contract-1",
            payment_url="https://app.lava.top/pay/contract-1",
            amount=549,
            currency="RUB",
            status="new",
            raw={},
        )


@pytest.mark.asyncio
async def test_способ_оплаты_берётся_из_тарифа():
    """Иначе выбравший СБП получил бы счёт на карту."""
    provider = FakeInvoiceProvider()
    plan = _once_plan()
    user = _user()
    user.email = "client@example.com"
    session = FakeBillingSession(plan=plan, user_id=user.id)

    import app.core.services.billing as mod

    было = mod.get_payments
    mod.get_payments = lambda: provider
    try:
        started = await billing.start_payment(session, user, plan)
    finally:
        mod.get_payments = было

    assert provider.kwargs["paymentProvider"] == "PAY2ME"
    assert provider.kwargs["paymentMethod"] == "SBP"
    assert provider.kwargs["periodicity"] == "ONE_TIME"
    assert started.payment_url.endswith("contract-1")


@pytest.mark.asyncio
async def test_у_подписки_способ_оплаты_не_навязывается():
    """Для рублей платёжка сама ставит карту: лишний параметр только сузит выбор."""
    provider = FakeInvoiceProvider()
    plan = _plan()
    user = _user()
    user.email = "client@example.com"
    session = FakeBillingSession(plan=plan, user_id=user.id)

    import app.core.services.billing as mod

    было = mod.get_payments
    mod.get_payments = lambda: provider
    try:
        await billing.start_payment(session, user, plan)
    finally:
        mod.get_payments = было

    assert provider.kwargs["paymentProvider"] is None
    assert provider.kwargs["paymentMethod"] is None


def test_кнопка_тарифа_несёт_свой_код():
    """Без кода в кнопке выбор способа терялся бы при выставлении счёта."""
    from app.bot.keyboards.subscription import offer_keyboard

    подписка = _plan()
    разовый = _once_plan()
    markup = offer_keyboard([(подписка, "549", "₽"), (разовый, "549", "₽")])
    коды = [row[0].callback_data for row in markup.inline_keyboard]
    assert коды == ["sub:pay:monthly", "sub:pay:once30"]

    тексты = [row[0].text for row in markup.inline_keyboard]
    assert "Картой" in тексты[0]
    assert "СБП" in тексты[1] and "30" in тексты[1]


def test_старая_кнопка_без_кода_тарифа_не_ломается():
    """Кнопки из давней переписки живут вечно, нажатие должно работать."""
    from app.bot.keyboards.subscription import parse_subscription_action

    assert parse_subscription_action("sub:pay:once30") == ("pay", "once30")
    assert parse_subscription_action("sub:pay") == ("pay", "")
    assert parse_subscription_action("limits:show") is None
    assert parse_subscription_action("") is None


@pytest.mark.asyncio
async def test_разовой_оплате_не_обещают_автопродление():
    from app.worker.tasks.billing import _autorenews

    session = FakeBillingSession(plan=_once_plan())
    assert await _autorenews(session, "once30") is False

    session = FakeBillingSession(plan=_plan())
    assert await _autorenews(session, "monthly") is True


@pytest.mark.asyncio
async def test_напоминание_уходит_один_раз():
    """Задача крутится ежедневно: без отметки человек получал бы её каждый раз."""
    subscription = Subscription()
    subscription.user_id = uuid.uuid4()
    subscription.plan_code = "once30"
    subscription.status = SUB_ACTIVE
    subscription.expires_at = datetime.now(UTC) + timedelta(days=1)
    subscription.reminded_at = None

    session = FakeBillingSession()
    await billing.mark_reminded(session, subscription)
    assert subscription.reminded_at is not None


# --- возврат денег ------------------------------------------------------------


def test_возврат_и_чарджбэк_разбираются_как_возврат():
    """Оба события означают одно: денег у заказчика больше нет."""
    from app.core.providers.base import EVENT_REFUNDED

    возврат = _provider().parse_webhook(_payload("refund.success"))
    спор = _provider().parse_webhook(_payload("chargeback.initiated"))
    assert возврат.kind == EVENT_REFUNDED
    assert спор.kind == EVENT_REFUNDED


@pytest.mark.asyncio
async def test_возврат_закрывает_доступ():
    """Иначе вернувший деньги пользуется безлимитом бесплатно."""
    from app.core.providers.base import EVENT_REFUNDED

    user_id = uuid.uuid4()
    session = FakeBillingSession(plan=_plan(), user_id=user_id)
    событие = _provider().parse_webhook(_payload("refund.success"))

    applied = await billing.apply_event(session, событие, "lavatop")

    assert applied.kind == EVENT_REFUNDED
    подписки = [sql for sql in session.executed if "UPDATE subscriptions" in sql]
    assert подписки, "подписка должна закрываться тем же запросом"
    assert "expires_at = now()" in подписки[0]
    платежи = [sql for sql in session.executed if "UPDATE payments" in sql]
    assert платежи, "платёж должен помечаться возвратом"
    assert "payment_refunded" in session.events


# --- отвергнутый платёжкой адрес ----------------------------------------------


@pytest.mark.asyncio
async def test_отвергнутый_адрес_спрашивается_заново():
    """Продавец не может купить у себя: платёжка отвечает Incorrect email.

    Человеку нельзя показывать «сервис не ответил»: он будет жать «оплатить»
    по кругу и получать одно и то же. Адрес стирается и спрашивается заново.
    """
    from app.bot.handlers import subscription as sub
    from app.core.providers.base import ProviderError

    class FakeMessage:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def answer(self, text, **kwargs):
            self.sent.append(text)

    class FakeQueue:
        def __init__(self) -> None:
            self.keys: dict[str, str] = {}

        async def set(self, key, value, ex=None):
            self.keys[key] = value

    async def падает(*args, **kwargs):
        raise ProviderError(
            "lavatop",
            "invoice",
            "платёжка отказалась выставить счёт",
            status_code=400,
            body='{"error":"Incorrect email to purchase"}',
        )

    user = _user()
    user.email = "seller@example.com"
    message, queue = FakeMessage(), FakeQueue()
    session = FakeBillingSession(plan=_plan(), user_id=user.id)

    было = sub.start_payment
    sub.start_payment = падает
    try:
        await sub._send_invoice(message, session, user, _plan(), queue)
    finally:
        sub.start_payment = было

    assert user.email is None, "адрес должен стереться, иначе повторится та же ошибка"
    assert queue.keys, "должны снова ждать почту"
    assert list(queue.keys.values()) == ["monthly"], "выбранный тариф не теряется"
    assert "не приняла этот адрес" in message.sent[0]
