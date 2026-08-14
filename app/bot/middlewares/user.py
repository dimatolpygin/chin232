"""Сессия БД и пользователь продукта в контексте хендлера."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.core.events import track
from app.db.repositories.users import get_or_create_telegram_user
from app.db.session import session_scope
from app.logging import bind_request, get_logger

log = get_logger("bot")


class UserMiddleware(BaseMiddleware):
    """Открывает сессию, находит или заводит пользователя, кладёт в data.

    Хендлер получает готовые `session` и `user` и не занимается ни тем, ни другим.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update: Update = event  # type: ignore[assignment]
        tg_user = getattr(update.event, "from_user", None)
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        async with session_scope() as session:
            user, created = await get_or_create_telegram_user(
                session,
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
            )
            bind_request(data.get("request_id"), user_id=str(user.id))
            if created:
                log.info(
                    "новый пользователь заведён",
                    user_id=str(user.id),
                    telegram_id=tg_user.id,
                    username=f"@{tg_user.username or '—'}",
                )
                await track(
                    session,
                    "user_registered",
                    user_id=user.id,
                    source="telegram",
                    telegram_id=tg_user.id,
                )

            data["session"] = session
            data["user"] = user
            data["is_new_user"] = created
            return await handler(event, data)
