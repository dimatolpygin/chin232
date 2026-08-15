"""Вебхуки платёжки.

Единственная дверь, через которую в проект приходят деньги, поэтому здесь три
правила и ни одного исключения:

1. Чужой запрос не доезжает до биллинга — сначала подпись, потом всё остальное.
2. Повторная доставка не продлевает подписку дважды: идемпотентность в базе,
   а не в памяти процесса.
3. Отвечаем 200 всему, что разобрали, даже если применить не смогли: на 4xx и
   5xx lava.top повторит доставку девятнадцать раз, и от повторов ошибка в
   наших данных не исправится.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.providers.base import ProviderError
from app.core.providers.registry import get_payments
from app.core.services.billing import apply_event
from app.db.session import session_scope
from app.logging import bind_request, get_logger

router = APIRouter()
log = get_logger("api")


@router.post("/webhooks/lavatop")
async def lavatop_webhook(request: Request) -> JSONResponse:
    bind_request(None)
    body = await request.body()
    headers = dict(request.headers)

    try:
        provider = get_payments()
    except ProviderError as exc:
        # Ключа нет — принять деньги мы всё равно не сможем, но и молча
        # отвечать «ок» на чужой запрос нельзя.
        log.error("вебхук платёжки некому обработать", ошибка=str(exc))
        return JSONResponse({"status": "unavailable"}, status_code=503)

    if not provider.verify_webhook(headers, body):
        # Тело не логируем: в подделанном вебхуке может быть что угодно, а вот
        # адрес отправителя для разбора нужен.
        log.warning(
            "вебхук отклонён: подпись не сошлась",
            провайдер=provider.name,
            адрес=request.client.host if request.client else "?",
            размер_байт=len(body),
        )
        return JSONResponse({"status": "forbidden"}, status_code=401)

    try:
        payload: Any = json.loads(body or b"{}")
    except ValueError:
        log.error("вебхук платёжки пришёл не в JSON", размер_байт=len(body))
        return JSONResponse({"status": "bad request"}, status_code=400)
    if not isinstance(payload, dict):
        log.error("вебхук платёжки пришёл не объектом", тип=type(payload).__name__)
        return JSONResponse({"status": "bad request"}, status_code=400)

    event = provider.parse_webhook(payload)
    log.info(
        "вебхук платёжки принят",
        провайдер=provider.name,
        событие=payload.get("eventType"),
        контракт=event.external_id,
        родительский_контракт=event.parent_external_id,
        сумма=event.amount,
        валюта=event.currency,
    )

    async with session_scope() as session:
        applied = await apply_event(session, event, provider.name)

    if applied.user_id and not applied.duplicate:
        await _notify(request, applied)
    return JSONResponse({"status": "ok"})


async def _notify(request: Request, applied) -> None:
    """Сказать юзеру в боте, что с его оплатой.

    Отправляет воркер: у api нет своего экземпляра бота, и заводить его ради
    одного сообщения — значит держать вторую сессию Telegram на процесс.
    """
    queue = getattr(request.app.state, "queue", None)
    if queue is None:
        log.error("очередь недоступна, юзер не узнает об оплате", user_id=str(applied.user_id))
        return
    try:
        await queue.enqueue_job(
            "notify_payment",
            user_id=str(applied.user_id),
            kind=applied.kind,
            expires_at=applied.expires_at.isoformat() if applied.expires_at else None,
            renewed=applied.renewed,
        )
    except Exception as exc:  # noqa: BLE001  деньги уже приняты, падать поздно
        log.error(
            "не удалось поставить уведомление об оплате",
            user_id=str(applied.user_id),
            ошибка=repr(exc),
        )
