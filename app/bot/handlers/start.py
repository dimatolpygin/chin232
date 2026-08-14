"""Хендлер /start. Разбирает апдейт, зовёт сервис, рисует ответ — и всё."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.services.onboarding import handle_start
from app.db.models import User
from app.logging import get_logger

router = Router(name="start")
log = get_logger("bot")


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    session: AsyncSession,
    user: User,
    is_new_user: bool,
) -> None:
    result = await handle_start(session, user, is_new_user)
    await message.answer(result.text)
    log.info(
        "ответ отправлен",
        username=f"@{message.from_user.username or '—'}" if message.from_user else "—",
        user_id=str(user.id),
        текст=result.text.replace("\n", " ")[:200],
    )
