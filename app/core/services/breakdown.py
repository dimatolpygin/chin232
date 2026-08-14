"""Разбор реплики бота: текст и варианты ответа.

Логика здесь, а не в хендлерах: те же два вызова понадобятся вебаппу.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import track
from app.core.providers.registry import get_llm
from app.core.services.pinyin import to_pinyin
from app.db.models import Dialog, User
from app.db.repositories.dialogs import ROLE_ASSISTANT, get_prompt, history_until
from app.logging import get_logger

log = get_logger("breakdown")

MAX_SUGGESTIONS = 3


class ReplyNotFound(LookupError):
    """Реплики нет в базе — например, база чистилась после отправки сообщения."""


@dataclass(slots=True)
class TextBreakdown:
    text_zh: str
    pinyin: str
    translation: str | None
    pinyin_offline: bool


@dataclass(slots=True)
class Suggestion:
    zh: str
    pinyin: str
    ru: str | None


async def _load(session: AsyncSession, user: User, dialog_id: int) -> Dialog:
    row = await session.get(Dialog, dialog_id)
    # Проверка владельца обязательна: callback_data приходит от клиента, и
    # подставить туда чужой id ничего не стоит.
    if row is None or row.user_id != user.id or row.role != ROLE_ASSISTANT:
        raise ReplyNotFound(f"реплика {dialog_id} недоступна пользователю {user.id}")
    return row


async def get_text_breakdown(session: AsyncSession, user: User, dialog_id: int) -> TextBreakdown:
    """Иероглифы, пиньинь и перевод. Ни одного обращения к платным сервисам.

    Пиньинь и перевод посчитала модель на шаге круга и они лежат в `dialogs`.
    Если модель сломала формат ответа, пиньинь не сохранился — тогда считаем
    его локально библиотекой. Перевод локально взять неоткуда, и выдумывать
    его нельзя: лучше честно показать, что перевода нет.
    """
    row = await _load(session, user, dialog_id)
    text = row.text_zh or ""
    offline = False
    pinyin = (row.pinyin or "").strip()
    if not pinyin:
        pinyin = to_pinyin(text)
        offline = True

    await track(
        session,
        "breakdown_shown",
        user_id=user.id,
        реплика=dialog_id,
        пиньинь_локально=offline,
        есть_перевод=bool(row.translation),
    )
    log.info(
        "разбор показан",
        user_id=str(user.id),
        реплика=dialog_id,
        пиньинь_локально=offline,
    )
    return TextBreakdown(
        text_zh=text,
        pinyin=pinyin,
        translation=(row.translation or "").strip() or None,
        pinyin_offline=offline,
    )


def _parse_suggestions(data: dict[str, object]) -> list[Suggestion]:
    raw = data.get("suggestions")
    if not isinstance(raw, list):
        return []
    out: list[Suggestion] = []
    # Ограничение считается по годным вариантам, а не по сырому списку: иначе
    # три мусорные записи в начале съедали бы всю подсказку.
    for item in raw:
        if len(out) >= MAX_SUGGESTIONS:
            break
        if not isinstance(item, dict):
            continue
        zh = str(item.get("zh") or "").strip()
        if not zh:
            continue
        pinyin = str(item.get("pinyin") or "").strip() or to_pinyin(zh)
        ru = str(item.get("ru") or "").strip() or None
        out.append(Suggestion(zh=zh, pinyin=pinyin, ru=ru))
    return out


async def get_suggestions(
    session: AsyncSession, user: User, dialog_id: int
) -> tuple[list[Suggestion], bool]:
    """Варианты ответа под уровень юзера. Возвращает список и признак «из кэша».

    Единственный платный вызов этапа. Результат кладётся к реплике: подсказка
    к одной и той же фразе не меняется, а платить за неё дважды незачем.
    """
    from app.core.services.dialog import TOPIC_FREE, describe_level

    row = await _load(session, user, dialog_id)
    if row.help_text:
        log.info("подсказка взята из кэша", user_id=str(user.id), реплика=dialog_id)
        return _decode(row.help_text), True

    settings = get_settings()
    history = await history_until(session, user.id, dialog_id, settings.dialog_history_limit)
    if not history:
        # Реплика уже в базе, значит история пустой быть не может; но если
        # обрезание истории её съело, модели нужен хотя бы сам вопрос.
        history = [{"role": ROLE_ASSISTANT, "content": row.text_zh or ""}]

    template = await get_prompt(session, "help_suggestions")
    prompt = template.format(hsk_level=describe_level(user), topic=user.topic or TOPIC_FREE)
    data = await get_llm(settings).complete_json(prompt, history)
    suggestions = _parse_suggestions(data)

    if suggestions:
        row.help_text = _encode(suggestions)
        await session.flush()
    await track(
        session,
        "help_shown",
        user_id=user.id,
        реплика=dialog_id,
        вариантов=len(suggestions),
        из_кэша=False,
    )
    log.info(
        "подсказка собрана",
        user_id=str(user.id),
        реплика=dialog_id,
        вариантов=len(suggestions),
    )
    return suggestions, False


# Кэш хранится текстом, а не JSON-колонкой: подсказка — плоский список из трёх
# строк, отдельный тип колонки ради него избыточен.
FIELD_SEP = "\x1f"
ITEM_SEP = "\x1e"


def _encode(items: list[Suggestion]) -> str:
    return ITEM_SEP.join(FIELD_SEP.join([s.zh, s.pinyin, s.ru or ""]) for s in items)


def _decode(blob: str) -> list[Suggestion]:
    out: list[Suggestion] = []
    for chunk in blob.split(ITEM_SEP):
        parts = chunk.split(FIELD_SEP)
        if len(parts) != 3 or not parts[0]:
            continue
        out.append(Suggestion(zh=parts[0], pinyin=parts[1], ru=parts[2] or None))
    return out
