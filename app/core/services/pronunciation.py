"""Оценка произношения: «повтори за мной» и разбор записи по тонам.

Логика здесь, а не в хендлерах: тот же цикл понадобится вебаппу словаря, где
тренировка фразы идёт без Telegram.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import track
from app.core.providers.base import Pronunciation, SpeechUnclear
from app.core.providers.registry import get_pronunciation
from app.core.services.breakdown import ReplyNotFound
from app.core.services.pinyin import to_pinyin, to_pinyin_list
from app.db.models import Dialog, PronunciationCheck, User
from app.db.repositories.dialogs import ROLE_ASSISTANT
from app.logging import get_logger

log = get_logger("pronunciation")

# Сколько живёт режим «жду запись». Дольше часа он вреден: юзер давно ушёл в
# обычный разговор и не поймёт, почему его реплика уехала на оценку.
PRACTICE_TTL_SEC = 1800

# Длинную фразу сервис оценивает хуже, да и повторить её начинающему нечем.
MAX_REF_CHARS = 60


@dataclass(slots=True)
class PracticeTarget:
    """Что именно юзер должен произнести и чем это озвучить."""

    dialog_id: int
    ref_text: str
    pinyin: str
    translation: str | None
    # Эталон — исправленная фраза самого юзера, а не реплика бота.
    from_correction: bool
    # file_id уже отправленного голосового с этой фразой. Есть только когда
    # эталон — реплика бота: её Telegram уже хранит, и переслать её бесплатно.
    audio_file_id: str | None = None


@dataclass(slots=True)
class CharResult:
    """Строка разбора: иероглиф, его пиньинь и что случилось с тоном."""

    char: str
    pinyin: str
    score: int | None
    tone_expected: int | None
    tone_actual: int | None
    tone_score: int | None = None
    # Считает провайдер: где-то есть услышанный тон, где-то только балл.
    tone_ok: bool | None = None


@dataclass(slots=True)
class AssessResult:
    overall: int | None
    tone: int | None
    pronunciation: int | None
    fluency: int | None
    ref_text: str
    chars: list[CharResult]
    check_id: int
    # Сколько эталона сервис реально услышал. Низкая полнота — повод не верить
    # баллам и переписать фразу, но не повод скрывать разбор.
    integrity: int | None = None


async def choose_target(session: AsyncSession, user: User, dialog_id: int) -> PracticeTarget:
    """Что тренируем: исправленную фразу юзера, а иначе реплику бота.

    Если юзер только что ошибся, тренировать правильный ответ бота бессмысленно
    — ему нужно произнести именно то, в чём он ошибся. Поэтому исправленный
    вариант перебивает реплику бота, когда он есть.
    """
    row = await session.get(Dialog, dialog_id)
    # Владелец проверяется обязательно: callback_data приходит от клиента.
    if row is None or row.user_id != user.id or row.role != ROLE_ASSISTANT:
        raise ReplyNotFound(f"реплика {dialog_id} недоступна пользователю {user.id}")

    corrected = (row.corrected_zh or "").strip()
    if corrected:
        return PracticeTarget(
            dialog_id=dialog_id,
            ref_text=corrected[:MAX_REF_CHARS],
            pinyin=to_pinyin(corrected[:MAX_REF_CHARS]),
            translation=None,
            from_correction=True,
        )

    text = (row.text_zh or "").strip()[:MAX_REF_CHARS]
    if not text:
        raise ReplyNotFound(f"у реплики {dialog_id} нет текста для эталона")
    return PracticeTarget(
        dialog_id=dialog_id,
        ref_text=text,
        pinyin=(row.pinyin or "").strip() or to_pinyin(text),
        translation=(row.translation or "").strip() or None,
        from_correction=False,
        # Голос реплики бота уже отправлен и лежит в Telegram — переозвучивать
        # его значит платить второй раз за то же самое.
        audio_file_id=row.audio_file_id if len(row.text_zh or "") <= MAX_REF_CHARS else None,
    )


# --- режим ожидания записи ---------------------------------------------------
#
# Пока режим включён, голосовое юзера уходит не в разговорный круг, а на
# оценку. Состояние живёт в redis, а не в памяти процесса: бот и воркер — разные
# контейнеры, и переживать перезапуск оно тоже обязано.


def practice_key(user: User) -> str:
    return get_settings().redis_key("practice", str(user.id))


async def start_practice(queue, user: User, target: PracticeTarget) -> None:
    await queue.set(
        practice_key(user),
        json.dumps(
            {
                "dialog_id": target.dialog_id,
                "ref_text": target.ref_text,
                "pinyin": target.pinyin,
                "translation": target.translation,
                "from_correction": target.from_correction,
                "audio_file_id": target.audio_file_id,
            },
            ensure_ascii=False,
        ),
        ex=PRACTICE_TTL_SEC,
    )
    log.info(
        "режим «повтори за мной» включён",
        user_id=str(user.id),
        эталон=target.ref_text,
        из_исправления=target.from_correction,
    )


async def load_practice(queue, user: User) -> PracticeTarget | None:
    raw = await queue.get(practice_key(user))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("состояние тренировки не разобралось", user_id=str(user.id))
        return None
    return PracticeTarget(
        dialog_id=int(data.get("dialog_id") or 0),
        ref_text=str(data.get("ref_text") or ""),
        pinyin=str(data.get("pinyin") or ""),
        translation=data.get("translation"),
        from_correction=bool(data.get("from_correction")),
        audio_file_id=data.get("audio_file_id"),
    )


async def stop_practice(queue, user: User) -> None:
    await queue.delete(practice_key(user))
    log.info("режим «повтори за мной» выключен", user_id=str(user.id))


async def remember_reference_audio(queue, user: User, file_id: str) -> None:
    """Запомнить голос эталона, чтобы «Послушать» не синтезировал заново."""
    target = await load_practice(queue, user)
    if target is None:
        return
    target.audio_file_id = file_id
    await start_practice(queue, user, target)


# --- сама оценка -------------------------------------------------------------


def _merge(result: Pronunciation, ref_text: str) -> list[CharResult]:
    """Сложить баллы сервиса с локальным пиньинем.

    Пиньинь считается по строке из тех же иероглифов, что вернул сервис, а не
    по эталону: так соответствие строго один к одному, а тоны сандхи (你好,
    不是) остаются правильными, потому что контекст фразы сохранён.
    """
    chars = [c for c in result.chars if c.char]
    # Пиньинь от сервиса точнее: он считал сандхи по той же фразе. Локальный
    # нужен, когда сервис его не прислал.
    if all(c.pinyin for c in chars):
        syllables = [c.pinyin or "" for c in chars]
    else:
        syllables = to_pinyin_list("".join(c.char for c in chars))
    if len(syllables) != len(chars):
        # Рассинхрон возможен на редких знаках: лучше показать без пиньиня,
        # чем подписать иероглифы чужими слогами.
        log.warning(
            "пиньинь не сошёлся с разбором посимвольно",
            иероглифов=len(chars),
            слогов=len(syllables),
            эталон=ref_text,
        )
        syllables = []
    return [
        CharResult(
            char=c.char,
            pinyin=syllables[i] if i < len(syllables) else "",
            score=c.overall,
            tone_expected=c.tone_expected,
            tone_actual=c.tone_actual,
            tone_score=c.tone,
            tone_ok=c.tone_ok,
        )
        for i, c in enumerate(chars)
    ]


async def assess_attempt(
    session: AsyncSession,
    user: User,
    audio: bytes,
    target: PracticeTarget,
) -> AssessResult:
    """Оценить запись юзера по эталону. Звук приходит как есть, из Telegram."""
    from app.core.audio import to_wav16k

    # Сервис принимает и ogg, но заявленные в запросе частота и разрядность
    # должны совпадать с файлом, иначе разбор едет. Своя конвертация надёжнее
    # догадок о том, что именно прислал Telegram.
    wav = await to_wav16k(audio)

    try:
        result = await get_pronunciation().assess(wav, target.ref_text, str(user.id))
    except SpeechUnclear:
        await track(
            session,
            "pronunciation_unclear",
            user_id=user.id,
            реплика=target.dialog_id,
            эталон=target.ref_text,
        )
        raise

    row = PronunciationCheck(
        user_id=user.id,
        dialog_id=target.dialog_id or None,
        ref_text=target.ref_text,
        overall=result.overall,
        pronunciation=result.pronunciation,
        tone=result.tone,
        fluency=result.fluency,
        integrity=result.integrity,
        # Ответ сервиса сохраняется целиком: в нём пофонемная разбивка, которая
        # сегодня не рисуется, а переспросить по той же записи уже нельзя.
        detail=result.raw,
    )
    session.add(row)
    await session.flush()

    chars = _merge(result, target.ref_text)
    сбито = sum(1 for c in chars if c.tone_ok is False)
    await track(
        session,
        "pronunciation_checked",
        user_id=user.id,
        реплика=target.dialog_id,
        балл=result.overall,
        балл_тонов=result.tone,
        иероглифов=len(chars),
        сбитых_тонов=сбито,
        из_исправления=target.from_correction,
    )
    log.info(
        "произношение оценено",
        user_id=str(user.id),
        эталон=target.ref_text,
        балл=result.overall,
        балл_тонов=result.tone,
        сбитых_тонов=сбито,
        оценка_id=row.id,
    )
    return AssessResult(
        overall=result.overall,
        tone=result.tone,
        pronunciation=result.pronunciation,
        fluency=result.fluency,
        ref_text=target.ref_text,
        chars=chars,
        check_id=row.id,
        integrity=result.integrity,
    )
