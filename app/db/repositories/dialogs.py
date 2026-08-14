"""Доступ к репликам диалога и промптам."""

from __future__ import annotations

import uuid

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Dialog, Prompt

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


async def add_reply(
    session: AsyncSession,
    user_id: uuid.UUID,
    role: str,
    text_zh: str | None,
    pinyin: str | None = None,
    translation: str | None = None,
    correction: str | None = None,
    audio_file_id: str | None = None,
) -> Dialog:
    row = Dialog(
        user_id=user_id,
        role=role,
        text_zh=text_zh,
        pinyin=pinyin,
        translation=translation,
        correction=correction,
        audio_file_id=audio_file_id,
    )
    session.add(row)
    await session.flush()
    return row


async def recent_history(
    session: AsyncSession, user_id: uuid.UUID, limit: int
) -> list[dict[str, str]]:
    """Последние реплики в формате сообщений для LLM, от старых к новым."""
    stmt = (
        select(Dialog)
        .where(Dialog.user_id == user_id)
        .order_by(desc(Dialog.created_at), desc(Dialog.id))
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars())
    rows.reverse()
    return [{"role": r.role, "content": r.text_zh or ""} for r in rows if r.text_zh]


async def history_until(
    session: AsyncSession, user_id: uuid.UUID, dialog_id: int, limit: int
) -> list[dict[str, str]]:
    """История по состоянию на конкретную реплику включительно.

    Кнопки живут под сообщением сколько угодно долго: юзер может нажать
    «Помощь» под ответом получасовой давности, когда разговор уже ушёл вперёд.
    Модели в этом случае нужен тот контекст, а не текущий конец диалога.
    """
    stmt = (
        select(Dialog)
        .where(Dialog.user_id == user_id, Dialog.id <= dialog_id)
        .order_by(desc(Dialog.id))
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars())
    rows.reverse()
    return [{"role": r.role, "content": r.text_zh or ""} for r in rows if r.text_zh]


async def last_assistant_reply(session: AsyncSession, user_id: uuid.UUID) -> Dialog | None:
    stmt = (
        select(Dialog)
        .where(Dialog.user_id == user_id, Dialog.role == ROLE_ASSISTANT)
        .order_by(desc(Dialog.created_at), desc(Dialog.id))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_audio_file_id(session: AsyncSession, dialog_id: int, file_id: str) -> None:
    """Запомнить file_id отправленного голосового у конкретной реплики.

    Именно по id, а не «последняя реплика бота»: два круга одного юзера могут
    идти параллельно, и последней окажется чужая.
    """
    row = await session.get(Dialog, dialog_id)
    if row is not None:
        row.audio_file_id = file_id


async def get_prompt(session: AsyncSession, code: str) -> str:
    """Тело промпта из базы. Промпты правятся без деплоя."""
    prompt = await session.get(Prompt, code)
    if prompt is None:
        raise LookupError(f"промпт {code} не найден в базе")
    return prompt.body
