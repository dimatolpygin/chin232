"""Журнал событий: единственная точка записи в таблицу `events`.

Правило простое — что не записано в `events`, того для аналитики не было.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event
from app.logging import get_logger

log = get_logger("events")


async def track(
    session: AsyncSession,
    type_: str,
    user_id: uuid.UUID | None = None,
    **payload: Any,
) -> None:
    """Записать событие. Не коммитит — коммит делает вызывающий scope."""
    session.add(Event(user_id=user_id, type=type_, payload=payload or {}))
    log.debug("событие записано", событие=type_, user_id=str(user_id) if user_id else None)
