"""Все модели импортируются здесь: alembic видит их через app.db.base.Base.metadata."""

from app.db.models.dialog import Dialog
from app.db.models.event import Event
from app.db.models.identity import Identity
from app.db.models.prompt import Prompt
from app.db.models.setting import Setting
from app.db.models.user import User

__all__ = ["Dialog", "Event", "Identity", "Prompt", "Setting", "User"]
