"""Асинхронный движок и фабрика сессий SQLAlchemy."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.core import usage
from app.logging import get_logger

log = get_logger("db")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия с автокоммитом на выходе и откатом при исключении.

    Заодно дописывает журнал расхода на внешние сервисы. Провайдеры копят его
    в памяти, чтобы не платить лишним запросом посреди голосового круга, а
    уезжает он попутно — на ближайшем коммите, чьим бы тот ни был. Откат
    возвращает записи в буфер: круг сорвался, а деньги за вызовы уже потрачены,
    и в расходах они обязаны быть видны.
    """
    async with get_session_factory()() as session:
        spent: list[usage.Call] = []
        try:
            yield session
            spent = usage.take()
            await _write_spending(session, spent)
            spent = []
            await session.commit()
        except Exception:
            await session.rollback()
            usage.give_back(spent)
            raise


async def _write_spending(session: AsyncSession, spent: list[usage.Call]) -> None:
    """Дописать журнал расхода, не ставя под удар саму работу.

    Отдельной точкой сохранения: неудачный INSERT в постгресе рушит всю
    транзакцию целиком, и сбой в диагностическом журнале утащил бы за собой
    ответ пользователю. Записи при этом выбрасываем, а не возвращаем в буфер:
    те же строки упадут точно так же, а расход — не деньги юзера, чтобы ради
    него ломать разговор.
    """
    if not spent:
        return
    try:
        async with session.begin_nested():
            await usage.write(session, spent)
    except Exception as exc:  # noqa: BLE001  журнал расхода не главнее разговора
        log.error("журнал расхода не записан", вызовов=len(spent), ошибка=repr(exc))


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
