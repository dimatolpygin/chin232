"""Логирование каждого входящего апдейта и привязка request_id к цепочке."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.logging import bind_request, clear_request, get_logger

log = get_logger("bot")


def _describe(update: Update) -> tuple[Any, str, str]:
    """Вернуть (пользователь, тип апдейта, содержимое) в пригодном для лога виде."""
    event = update.event
    if isinstance(event, Message):
        content = event.text or event.caption or f"({event.content_type})"
        return event.from_user, "сообщение", content
    if isinstance(event, CallbackQuery):
        return event.from_user, "кнопка", event.data or "(без данных)"
    return getattr(event, "from_user", None), update.event_type, "(без текста)"


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update: Update = event  # type: ignore[assignment]
        user, kind, content = _describe(update)

        request_id = bind_request(
            update_id=update.update_id,
            telegram_id=getattr(user, "id", None),
            username=getattr(user, "username", None) or "—",
        )
        data["request_id"] = request_id

        log.info(
            "входящее",
            тип=kind,
            username=f"@{getattr(user, 'username', None) or '—'}",
            user_id=getattr(user, "id", None),
            first_name=getattr(user, "first_name", None) or "—",
            текст=content,
        )

        started = time.monotonic()
        try:
            return await handler(event, data)
        except Exception as exc:
            log.exception(
                "апдейт упал с ошибкой",
                ошибка=repr(exc),
                длительность_мс=round((time.monotonic() - started) * 1000),
            )
            raise
        finally:
            log.debug(
                "апдейт обработан", длительность_мс=round((time.monotonic() - started) * 1000)
            )
            clear_request()
