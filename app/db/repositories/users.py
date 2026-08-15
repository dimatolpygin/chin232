"""Доступ к пользователям и их способам входа."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Identity, User

PROVIDER_TELEGRAM = "telegram"


async def get_by_identity(session: AsyncSession, provider: str, ext_id: str) -> User | None:
    stmt = (
        select(User)
        .join(Identity, Identity.user_id == User.id)
        .where(Identity.provider == provider, Identity.ext_id == ext_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_or_create_telegram_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
) -> tuple[User, bool]:
    """Вернуть пользователя по telegram_id, создав его при первом обращении.

    Второй элемент кортежа — признак того, что пользователь только что создан.
    """
    ext_id = str(telegram_id)
    identity = await session.get(Identity, (PROVIDER_TELEGRAM, ext_id))
    if identity is not None:
        # Профиль в Telegram мог измениться с прошлого раза.
        if username != identity.username or first_name != identity.first_name:
            identity.username = username
            identity.first_name = first_name
        user = await session.get(User, identity.user_id)
        assert user is not None
        return user, False

    user = User()
    session.add(user)
    await session.flush()
    session.add(
        Identity(
            provider=PROVIDER_TELEGRAM,
            ext_id=ext_id,
            user_id=user.id,
            username=username,
            first_name=first_name,
        )
    )
    await session.flush()
    return user, True


async def telegram_chat_id(session: AsyncSession, user_id) -> int | None:
    """Куда писать пользователю в Telegram.

    В личной переписке chat_id совпадает с telegram_id, а он лежит в
    `identities`: в самом `users` его нет намеренно, иначе вход через вебапп
    потребовал бы миграции всей базы.
    """
    ext_id = await session.scalar(
        select(Identity.ext_id).where(
            Identity.user_id == user_id, Identity.provider == PROVIDER_TELEGRAM
        )
    )
    try:
        return int(ext_id) if ext_id else None
    except (TypeError, ValueError):
        return None
