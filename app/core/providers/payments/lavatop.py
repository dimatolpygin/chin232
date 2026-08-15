"""Приём денег через lava.top.

Счёт создаётся на почту покупателя и на конкретный оффер (цену) из кабинета,
в ответ приходит ссылка на виджет оплаты — карта и СБП выбираются уже в нём.
Обратно платёжка стучится вебхуком.
"""

from __future__ import annotations

import base64
import hmac

from app.config import Settings
from app.core.providers.base import (
    EVENT_CANCELLED,
    EVENT_FAILED,
    EVENT_PAID,
    EVENT_UNKNOWN,
    Invoice,
    PaymentEvent,
    PaymentProvider,
    ProviderError,
    call_logged,
)
from app.core.providers.http import get_client
from app.logging import get_logger

log = get_logger("providers")

INVOICE_PATH = "/api/v3/invoice"

# Типы событий вебхука по документации lava.top.
PAID_EVENTS = {"payment.success", "subscription.recurring.payment.success"}
FAILED_EVENTS = {"payment.failed", "subscription.recurring.payment.failed"}
CANCELLED_EVENTS = {"subscription.cancelled"}
RECURRING_EVENTS = {
    "subscription.recurring.payment.success",
    "subscription.recurring.payment.failed",
}


def _same(left: str, right: str) -> bool:
    """Сравнение секретов за постоянное время.

    Сравниваем байты, а не строки: `compare_digest` на строках с не-ASCII
    символами не сравнивает их, а бросает TypeError — и вебхук отвечал бы 500
    на каждую доставку, если в секрете окажется хоть одна кириллическая буква.
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _str_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


class LavaTopPayments(PaymentProvider):
    name = "lavatop"

    def __init__(self, settings: Settings) -> None:
        if not settings.lavatop_api_key:
            raise ProviderError(self.name, "init", "не задан LAVATOP_API_KEY")
        self._key = settings.lavatop_api_key
        self._base = settings.lavatop_api_url.rstrip("/")
        # Вебхук подписывается тем же ключом, если отдельный не задан: в
        # кабинете lava.top для вебхука так и выбирается «API-ключ». Отдельная
        # переменная оставлена на случай, когда там задают свой секрет.
        self._webhook_secret = settings.lavatop_webhook_secret or settings.lavatop_api_key
        self._timeout = settings.provider_timeout

    async def create_invoice(
        self,
        offer_id: str,
        email: str,
        currency: str,
        periodicity: str | None = None,
        **extra: object,
    ) -> Invoice:
        body: dict[str, object] = {"email": email, "offerId": offer_id, "currency": currency}
        if periodicity:
            body["periodicity"] = periodicity
        body.update({k: v for k, v in extra.items() if v is not None})

        async with call_logged(
            self.name, "invoice", оффер=offer_id, валюта=currency, периодичность=periodicity
        ) as details:
            response = await get_client().post(
                f"{self._base}{INVOICE_PATH}",
                headers={"X-Api-Key": self._key, "Content-Type": "application/json"},
                json=body,
                timeout=self._timeout,
            )
            details["http_код"] = response.status_code
            if response.status_code >= 400:
                raise ProviderError(
                    self.name,
                    "invoice",
                    "платёжка отказалась выставить счёт",
                    status_code=response.status_code,
                    body=response.text[:1000],
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError(
                    self.name,
                    "invoice",
                    "платёжка ответила не JSON",
                    status_code=response.status_code,
                    body=response.text[:1000],
                ) from exc

        contract_id = _str_or_none(data.get("id"))
        if not contract_id:
            # Без идентификатора контракта оплату не с чем связать: вебхук
            # придёт в пустоту, а деньги у человека спишутся.
            raise ProviderError(
                self.name, "invoice", "в ответе нет идентификатора контракта", body=str(data)[:1000]
            )
        total = data.get("amountTotal") or {}
        return Invoice(
            external_id=contract_id,
            payment_url=_str_or_none(data.get("paymentUrl")),
            amount=total.get("amount") if isinstance(total, dict) else None,
            currency=(total.get("currency") if isinstance(total, dict) else None) or currency,
            status=_str_or_none(data.get("status")),
            raw=data,
        )

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Проверка подписи вебхука.

        lava.top не считает HMAC от тела, а присылает согласованный секрет —
        заголовком `X-Api-Key` либо basic-авторизацией. Сравниваем постоянным
        по времени сравнением: обычное `==` выходит из цикла на первом
        несовпавшем символе и выдаёт секрет подбором по времени ответа.
        """
        lower = {k.lower(): v for k, v in headers.items()}
        api_key = lower.get("x-api-key")
        if api_key and _same(api_key, self._webhook_secret):
            return True

        auth = lower.get("authorization") or ""
        if auth.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8", "replace")
            except (ValueError, IndexError):
                return False
            # Секрет может быть задан как паролем, так и целиком строкой.
            password = decoded.split(":", 1)[1] if ":" in decoded else decoded
            return _same(password, self._webhook_secret) or _same(decoded, self._webhook_secret)
        return False

    def parse_webhook(self, payload: dict[str, object]) -> PaymentEvent:
        event_type = str(payload.get("eventType") or "")
        if event_type in PAID_EVENTS:
            kind = EVENT_PAID
        elif event_type in FAILED_EVENTS:
            kind = EVENT_FAILED
        elif event_type in CANCELLED_EVENTS:
            kind = EVENT_CANCELLED
        else:
            kind = EVENT_UNKNOWN

        buyer = payload.get("buyer")
        email = _str_or_none(buyer.get("email")) if isinstance(buyer, dict) else None
        amount = payload.get("amount")
        return PaymentEvent(
            kind=kind,
            external_id=str(payload.get("contractId") or ""),
            parent_external_id=_str_or_none(payload.get("parentContractId")),
            email=email,
            amount=float(amount) if isinstance(amount, int | float) else None,
            currency=_str_or_none(payload.get("currency")),
            recurring=event_type in RECURRING_EVENTS,
            expires_at=_str_or_none(payload.get("willExpireAt")),
            error=_str_or_none(payload.get("errorMessage")),
            raw=payload,
        )
