"""Сервис входа пользователя. Логика живёт здесь, не в хендлере."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import track
from app.core.services.partners import attach_by_start
from app.db.models import User


@dataclass(slots=True)
class StartResult:
    is_new_user: bool
    need_level: bool


async def handle_start(
    session: AsyncSession, user: User, is_new_user: bool, args: str | None = None
) -> StartResult:
    """`args` — параметр после `/start`: по нему юзер закрепляется за партнёром."""
    await attach_by_start(session, user, is_new_user, args)
    need_level = user.hsk_level is None
    await track(
        session,
        "start",
        user_id=user.id,
        is_new_user=is_new_user,
        нужен_уровень=need_level,
        параметр=args or None,
    )
    return StartResult(is_new_user=is_new_user, need_level=need_level)
