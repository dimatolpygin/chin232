"""Базовый класс моделей. Импортируется alembic'ом для автогенерации ревизий."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
