"""Все модели импортируются здесь: alembic видит их через app.db.base.Base.metadata."""

from app.db.models.billing import Payment, Plan, Subscription
from app.db.models.daily_usage import DailyUsage
from app.db.models.dialog import Dialog
from app.db.models.event import Event
from app.db.models.identity import Identity
from app.db.models.prompt import Prompt
from app.db.models.pronunciation import PronunciationCheck
from app.db.models.setting import Setting
from app.db.models.user import User

__all__ = [
    "DailyUsage",
    "Dialog",
    "Event",
    "Identity",
    "Payment",
    "Plan",
    "Prompt",
    "PronunciationCheck",
    "Setting",
    "Subscription",
    "User",
]
