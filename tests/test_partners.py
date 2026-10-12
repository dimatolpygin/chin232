"""Партнёры: закрепление по ссылке, снимок партнёра в платеже, отчёт.

Цена ошибки здесь — деньги в обе стороны: юзер, закреплённый не за тем,
уводит процент чужому блогеру, а незакреплённый оставляет блогера без выплаты.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.bot.handlers.subscription import link_text
from app.bot.keyboards.admin import (
    SECTION_PARTNER,
    parse_admin_action,
    partner_keyboard,
    partners_keyboard,
)
from app.bot.render import render_partner_report, render_partners
from app.core.services import billing
from app.core.services import partners as ptn
from app.core.services.partners import (
    PERIOD_ALL,
    PERIOD_MONTH,
    PERIOD_PREV,
    PartnerError,
    Report,
    apply_promo,
    attach_by_start,
    code_from_start,
    normalize_promo,
    period_bounds,
    save_partner,
    save_promo,
    set_active,
)
from app.db.models import Partner, Payment, PromoCode, User
from tests.test_billing import (
    FakeBillingSession,
    FakeInvoiceProvider,
    _payload,
    _plan,
    _provider,
    _user,
)


def партнёр(code: str = "laoshi", percent: str = "20", active: bool = True) -> Partner:
    return Partner(
        id=uuid.uuid4(), code=code, name="Иван Петров", percent=Decimal(percent), active=active
    )


class FakeSession:
    """Партнёры в памяти: поиск по коду и запись событий."""

    def __init__(self, *партнёры: Partner) -> None:
        self.партнёры = {p.code: p for p in партнёры}
        self.added: list[object] = []

    async def scalar(self, statement):
        params = statement.compile().params
        return self.партнёры.get(next(iter(params.values()), None))

    def add(self, obj) -> None:
        self.added.append(obj)
        if isinstance(obj, Partner):
            self.партнёры[obj.code] = obj

    async def flush(self) -> None:
        return None

    @property
    def events(self) -> list[str]:
        return [getattr(o, "type", "") for o in self.added]


# --- ссылка ----------------------------------------------------------------------


def test_код_из_ссылки_разбирается():
    assert code_from_start("ref_LaoShi") == "laoshi"
    assert code_from_start("REF_laoshi-2") == "laoshi-2"


def test_чужой_и_битый_параметр_старта_не_код():
    assert code_from_start(None) is None
    assert code_from_start("") is None
    assert code_from_start("promo_laoshi") is None
    assert code_from_start("ref_") is None
    assert code_from_start("ref_a") is None
    assert code_from_start("ref_лаоши") is None


def test_ссылка_партнёра_ведёт_в_бота_с_кодом():
    assert ptn.partner_link("laoshi").endswith("?start=ref_laoshi")


# --- закрепление -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_новый_юзер_по_ссылке_закрепляется():
    p = партнёр()
    session = FakeSession(p)
    user = _user()

    found = await attach_by_start(session, user, True, "ref_laoshi")

    assert found is p
    assert user.partner_id == p.id
    assert "partner_attached" in session.events


@pytest.mark.asyncio
async def test_старый_юзер_по_чужой_ссылке_не_перезакрепляется():
    """Главное правило договора: юзера, которого клиент привёл сам, блогер не забирает."""
    session = FakeSession(партнёр())
    user = _user()

    assert await attach_by_start(session, user, False, "ref_laoshi") is None
    assert user.partner_id is None

    свой = uuid.uuid4()
    user.partner_id = свой
    assert await attach_by_start(session, user, False, "ref_laoshi") is None
    assert user.partner_id == свой
    assert "partner_ref_ignored" in session.events


@pytest.mark.asyncio
async def test_неизвестный_и_отключённый_код_как_обычный_старт():
    session = FakeSession(партнёр("off", active=False))
    user = _user()

    assert await attach_by_start(session, user, True, "ref_nobody") is None
    assert await attach_by_start(session, user, True, "ref_off") is None
    assert user.partner_id is None
    assert session.events.count("partner_ref_unknown") == 2


@pytest.mark.asyncio
async def test_обычный_старт_партнёра_не_ищет():
    session = FakeSession(партнёр())
    user = _user()
    assert await attach_by_start(session, user, True, None) is None
    assert session.events == []


# --- заведение партнёра -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_партнёр_заводится_и_правится_той_же_командой():
    session = FakeSession()
    p, заведён = await save_partner(session, "LaoShi", "20", "Иван Петров")
    assert заведён is True
    assert (p.code, p.percent, p.name) == ("laoshi", Decimal("20"), "Иван Петров")

    p.active = False
    тот_же, заведён = await save_partner(session, "laoshi", "12,5%", "Иван")
    assert заведён is False
    assert тот_же is p
    assert p.percent == Decimal("12.5")
    assert p.active is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "percent", "name", "поле"),
    [
        ("л", "20", "Иван", "code"),
        ("laoshi", "двадцать", "Иван", "percent"),
        ("laoshi", "120", "Иван", "percent"),
        ("laoshi", "-1", "Иван", "percent"),
        ("laoshi", "20", "   ", "name"),
    ],
)
async def test_мусор_в_команде_не_заводит_партнёра(code, percent, name, поле):
    session = FakeSession()
    with pytest.raises(PartnerError) as exc:
        await save_partner(session, code, percent, name)
    assert str(exc.value) == поле
    assert session.партнёры == {}


@pytest.mark.asyncio
async def test_отключение_по_коду():
    p = партнёр()
    session = FakeSession(p)
    assert await set_active(session, "LAOSHI", False) is p
    assert p.active is False
    assert await set_active(session, "nobody", False) is None


# --- деньги ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_счёт_несёт_партнёра_юзера():
    provider = FakeInvoiceProvider()
    plan = _plan()
    user = _user()
    user.email = "client@example.com"
    user.partner_id = uuid.uuid4()
    session = FakeBillingSession(plan=plan, user_id=user.id)

    import app.core.services.billing as mod

    было = mod.get_payments
    mod.get_payments = lambda: provider
    try:
        await billing.start_payment(session, user, plan)
    finally:
        mod.get_payments = было

    платёж = next(o for o in session.added if isinstance(o, Payment))
    assert платёж.partner_id == user.partner_id


@pytest.mark.asyncio
async def test_автопродление_тоже_засчитывается_партнёру():
    """Продление приходит новым контрактом: партнёр снимается у юзера при вставке."""
    session = FakeBillingSession(_plan(), uuid.uuid4(), partner=партнёр())
    applied = await billing.apply_event(
        session,
        _provider().parse_webhook(
            _payload(
                "subscription.recurring.payment.success",
                contractId="renewal-1",
                parentContractId="first-1",
            )
        ),
        "lavatop",
    )
    assert applied.renewed is True
    вставка = next(sql for sql in session.executed if "INSERT INTO payments" in sql)
    assert "(SELECT partner_id FROM users WHERE id = :user_id)" in вставка


@pytest.mark.asyncio
async def test_оплата_без_партнёра_идёт_как_раньше():
    session = FakeBillingSession(_plan(), uuid.uuid4(), partner=None)
    applied = await billing.apply_event(session, _provider().parse_webhook(_payload()), "lavatop")
    assert applied.duplicate is False
    assert session.subscription is not None


def test_причитается_по_проценту_и_по_валютам():
    report = Report(
        partner=партнёр(percent="12.5"),
        period=PERIOD_ALL,
        users=10,
        paid_users=3,
        payments=4,
        revenue={"RUB": Decimal("2196"), "USD": Decimal("10")},
    )
    assert report.due == {"RUB": Decimal("274.50"), "USD": Decimal("1.25")}


def test_периоды_по_московскому_календарю():
    assert period_bounds(PERIOD_ALL) == (None, None)

    начало, конец = period_bounds(PERIOD_MONTH, date(2026, 1, 15))
    assert (начало.date(), конец.date()) == (date(2026, 1, 1), date(2026, 2, 1))
    assert начало.utcoffset().total_seconds() == 3 * 3600

    начало, конец = period_bounds(PERIOD_PREV, date(2026, 1, 15))
    assert (начало.date(), конец.date()) == (date(2025, 12, 1), date(2026, 1, 1))


# --- админка -------------------------------------------------------------------------


def test_кнопки_отчёта_разбираются_обратно():
    """Код и период едут одним полем: четвёртое `parse_admin_action` читает числом."""
    for row in partners_keyboard([партнёр()]).inline_keyboard[:-1] + [
        partner_keyboard("laoshi", PERIOD_MONTH).inline_keyboard[0]
    ]:
        for button in row:
            action = parse_admin_action(button.callback_data)
            assert action is not None
            assert action.section == SECTION_PARTNER
            код, _, период = action.value.partition(".")
            assert код == "laoshi"
            assert период in ptn.PERIODS
            assert len(button.callback_data.encode()) <= 64


def test_отчёт_показывает_деньги_и_долю():
    текст = render_partner_report(
        Report(
            partner=партнёр(percent="20"),
            period=PERIOD_MONTH,
            users=12,
            paid_users=3,
            payments=4,
            revenue={"RUB": Decimal("2196")},
        )
    )
    assert "12" in текст and "2 196 RUB" in текст and "439 RUB" in текст
    assert "?start=ref_laoshi" in текст


def test_пустой_отчёт_пишет_ноль_с_валютой():
    """На проверке в боте «Оплат: 0 на 0» читалось как опечатка."""
    текст = render_partner_report(
        Report(partner=партнёр(), period=PERIOD_ALL, users=0, paid_users=0, payments=0)
    )
    assert "<b>0</b> на <b>0 RUB</b>" in текст


def test_список_партнёров_с_ссылками_и_экранированием():
    p = партнёр(active=False)
    p.name = "<b>Иван</b>"
    текст = render_partners([p])
    assert "&lt;b&gt;Иван" in текст
    assert "отключён" in текст
    assert "?start=ref_laoshi" in текст
    assert "Партнёров пока нет" in render_partners([])


def test_модель_юзера_и_платежа_знают_партнёра():
    assert "partner_id" in User.__table__.c
    assert "partner_id" in Payment.__table__.c


# --- промокоды ---------------------------------------------------------------------


class FakePromoSession(FakeSession):
    """Партнёры и промокоды в памяти."""

    def __init__(self, *партнёры: Partner, промокоды: tuple = ()) -> None:
        super().__init__(*партнёры)
        self.промокоды = {p.code: p for p in промокоды}

    async def scalar(self, statement):
        значение = next(iter(statement.compile().params.values()), None)
        if "promo_codes" in str(statement):
            return self.промокоды.get(значение)
        return self.партнёры.get(значение)

    async def get(self, model, key):
        return next((p for p in self.партнёры.values() if p.id == key), None)

    def add(self, obj) -> None:
        super().add(obj)
        if isinstance(obj, PromoCode):
            self.промокоды[obj.code] = obj


def промо(code: str = "KITAYAN", partner: Partner | None = None, active: bool = True):
    return PromoCode(code=code, partner_id=partner.id if partner else None, active=active)


def test_промокод_нормализуется_как_в_платёжке():
    assert normalize_promo(" kitayan ") == "KITAYAN"
    assert normalize_promo("kit ayan") == "KITAYAN"
    assert normalize_promo("ki") is None
    assert normalize_promo("китаян") is None


@pytest.mark.asyncio
async def test_промокод_запоминается_и_закрепляет_юзера_без_партнёра():
    p = партнёр()
    session = FakePromoSession(p, промокоды=(промо(partner=p),))
    user = _user()

    applied = await apply_promo(session, user, "kitayan")

    assert applied.ok and applied.code == "KITAYAN"
    assert user.promo_code == "KITAYAN"
    assert user.partner_id == p.id
    assert applied.attached is p


@pytest.mark.asyncio
async def test_промокод_не_перезакрепляет_чужого_юзера():
    p = партнёр()
    session = FakePromoSession(p, промокоды=(промо(partner=p),))
    user = _user()
    свой = uuid.uuid4()
    user.partner_id = свой

    applied = await apply_promo(session, user, "KITAYAN")

    assert applied.ok and user.promo_code == "KITAYAN"
    assert user.partner_id == свой
    assert applied.attached is None


@pytest.mark.asyncio
async def test_неизвестный_и_отключённый_промокод_не_принимается():
    session = FakePromoSession(промокоды=(промо("OLD", active=False),))
    user = _user()
    assert (await apply_promo(session, user, "NOPE")).ok is False
    assert (await apply_promo(session, user, "old")).ok is False
    assert user.promo_code is None
    assert "promo_rejected" in session.events


@pytest.mark.asyncio
async def test_промокод_заводится_с_партнёром_и_без():
    p = партнёр()
    session = FakePromoSession(p)
    promo, partner, заведён = await save_promo(session, "kitayan", "laoshi")
    assert (promo.code, promo.partner_id, partner, заведён) == ("KITAYAN", p.id, p, True)

    promo, partner, заведён = await save_promo(session, "KITAYAN", None)
    assert promo.partner_id is None and partner is None and заведён is False

    with pytest.raises(PartnerError, match="partner"):
        await save_promo(session, "KITAYAN", "nobody")
    with pytest.raises(PartnerError, match="promo"):
        await save_promo(session, "к", None)


@pytest.mark.asyncio
async def test_счёт_несёт_промокод_и_цена_не_пугает_лог():
    """С кодом сумма у платёжки меньше цены тарифа — это скидка, а не расхождение."""
    provider = FakeInvoiceProvider()
    plan = _plan()
    user = _user()
    user.email = "client@example.com"
    user.promo_code = "KITAYAN"
    session = FakeBillingSession(plan=plan, user_id=user.id)

    import app.core.services.billing as mod

    было = mod.get_payments
    mod.get_payments = lambda: provider
    try:
        started = await billing.start_payment(session, user, plan)
    finally:
        mod.get_payments = было

    assert provider.kwargs["promoCode"] == "KITAYAN"
    assert started.promo_code == "KITAYAN"
    платёж = next(o for o in session.added if isinstance(o, Payment))
    assert платёж.promo_code == "KITAYAN"


@pytest.mark.asyncio
async def test_без_промокода_поле_в_счёт_не_уходит():
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
    # Провайдер выкидывает None из тела, поэтому старые счета не меняются.
    assert provider.kwargs.get("promoCode") is None


def test_текст_ссылки_со_скидкой_и_без():
    plan = _plan()  # 590 ₽
    обычный = billing.Started(payment_url="u", external_id="e", plan=plan)
    assert "590" in link_text(обычный, plan, "")

    скидка = billing.Started(
        payment_url="u", external_id="e", plan=plan, amount=Decimal("472"), promo_code="KITAYAN"
    )
    assert "472" in link_text(скидка, plan, "") and "KITAYAN" in link_text(скидка, plan, "")

    без_суммы = billing.Started(
        payment_url="u", external_id="e", plan=plan, amount=Decimal("590"), promo_code="KITAYAN"
    )
    assert "странице оплаты" in link_text(без_суммы, plan, "")


def test_кнопка_промокода_в_предложении():
    from app.bot.keyboards.subscription import offer_keyboard

    markup = offer_keyboard([(_plan(), "590", "₽")])
    assert markup.inline_keyboard[-1][0].callback_data == "sub:promo"


def test_промокоды_видны_в_списке_партнёров():
    p = партнёр()
    текст = render_partners([p], {p.id: [промо(partner=p)], None: [промо("FREE", active=False)]})
    assert "<code>KITAYAN</code>" in текст
    assert "<s>FREE</s>" in текст
    assert "lava.top" in текст


@pytest.mark.asyncio
async def test_неоплаченный_счёт_с_промокодом_отдаётся_повторно():
    """lava.top тратит код на почту уже при выставлении: второй счёт она отвергнет."""

    class Сессия(FakeBillingSession):
        async def execute(self, statement, params=None):
            if "payments.promo_code" in str(statement):
                return type(
                    "R",
                    (),
                    {"first": lambda self: ("old-1", {"paymentUrl": "https://pay/old-1"}, 494.1)},
                )()
            return await super().execute(statement, params)

    provider = FakeInvoiceProvider()
    plan = _plan()
    user = _user()
    user.email = "client@example.com"
    user.promo_code = "KITAYAN"
    session = Сессия(plan=plan, user_id=user.id)

    import app.core.services.billing as mod

    было = mod.get_payments
    mod.get_payments = lambda: provider
    try:
        started = await billing.start_payment(session, user, plan)
    finally:
        mod.get_payments = было

    assert provider.kwargs == {}  # к платёжке не ходили
    assert started.payment_url == "https://pay/old-1"
    assert started.amount == Decimal("494.1")
