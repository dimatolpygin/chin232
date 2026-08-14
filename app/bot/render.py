"""Подготовка текста к отправке в Telegram.

Разметка у бота HTML, а в подписи и подсказки попадает текст от модели. Один
угловой скобкой в переводе Telegram отвечает 400 и сообщение не уходит вовсе —
поэтому всё чужое экранируется на границе отправки.
"""

from __future__ import annotations

import html

from app.bot.texts import ru
from app.core.providers.pronunciation.speechsuper import LOW_INTEGRITY
from app.core.services.pronunciation import AssessResult, CharResult, PracticeTarget

# Пороги окраски взяты из демо самого сервиса оценки: ниже 70 — красное, до 85 —
# жёлтое. Свои числа выдумывать нельзя, шкала не линейная.
SCORE_GOOD = 85
SCORE_FAIR = 70

# Пятый тон в китайском — нейтральный, и цифра «5» ученику ничего не говорит.
TONE_NAMES = {1: "1-й", 2: "2-й", 3: "3-й", 4: "4-й", 5: "нейтральный"}


def esc(value: str | None) -> str:
    return html.escape(value or "", quote=False)


def _mark(score: int | None, tone_ok: bool | None = None) -> str:
    # Сбитый тон окрашивает строку красным независимо от балла: именно тон —
    # то, ради чего юзер сюда пришёл, и потерять его в общем «неплохо» нельзя.
    if tone_ok is False:
        return "🔴"
    if score is None:
        return "⚪"
    if score >= SCORE_GOOD:
        return "🟢"
    if score >= SCORE_FAIR:
        return "🟡"
    return "🔴"


def _tone_name(tone: int | None) -> str:
    return TONE_NAMES.get(tone or 0, str(tone))


def render_char(char: CharResult) -> str:
    mark = _mark(char.score, char.tone_ok)
    score = "—" if char.score is None else char.score
    if char.tone_ok is False and char.tone_actual is None:
        return ru.PRON_CHAR_TONE_LOW.format(
            mark=mark,
            char=esc(char.char),
            pinyin=esc(char.pinyin),
            score=score,
            tone=_tone_name(char.tone_expected),
            tone_score="—" if char.tone_score is None else char.tone_score,
        )
    if char.tone_ok is False:
        return ru.PRON_CHAR_WRONG.format(
            mark=mark,
            char=esc(char.char),
            pinyin=esc(char.pinyin),
            score=score,
            expected=_tone_name(char.tone_expected),
            actual=_tone_name(char.tone_actual),
        )
    if char.tone_ok is True:
        return ru.PRON_CHAR_OK.format(
            mark=mark,
            char=esc(char.char),
            pinyin=esc(char.pinyin),
            score=score,
            tone=_tone_name(char.tone_expected),
        )
    return ru.PRON_CHAR_PLAIN.format(
        mark=mark, char=esc(char.char), pinyin=esc(char.pinyin), score=score
    )


def render_result(result: AssessResult) -> str:
    """Результат оценки: общий балл, эталон и разбор по иероглифам."""
    blocks = [
        ru.PRON_RESULT_HEADER.format(overall="—" if result.overall is None else result.overall)
    ]
    if result.tone is not None or result.pronunciation is not None:
        blocks.append(
            ru.PRON_DETAILS.format(
                tone="—" if result.tone is None else result.tone,
                pronunciation="—" if result.pronunciation is None else result.pronunciation,
            )
        )
    blocks.append(
        ru.PRON_REF.format(
            text_zh=esc(result.ref_text),
            pinyin=esc(" ".join(c.pinyin for c in result.chars if c.pinyin)),
        )
    )
    if result.chars:
        blocks.append("\n".join(render_char(c) for c in result.chars))
    сбито = sum(1 for c in result.chars if c.tone_ok is False)
    blocks.append(ru.PRON_TONES_WRONG.format(count=сбито) if сбито else ru.PRON_TONES_OK)
    if result.integrity is not None and result.integrity < LOW_INTEGRITY:
        # Баллам при неполной фразе верить нельзя, и молчать об этом нечестно:
        # юзер решит, что у него провальное произношение, хотя его просто не
        # дослушали.
        blocks.append(ru.PRON_PARTIAL)
    return "\n\n".join(blocks)


def render_practice(target: PracticeTarget) -> str:
    """Подпись к эталонному голосовому: что именно произносить."""
    blocks = [ru.PRACTICE_HEADER]
    if target.from_correction:
        blocks.append(ru.PRACTICE_FROM_CORRECTION)
    blocks.append(ru.PRON_REF.format(text_zh=esc(target.ref_text), pinyin=esc(target.pinyin)))
    if target.translation:
        blocks.append(esc(target.translation))
    blocks.append(ru.PRACTICE_ASK)
    return "\n\n".join(blocks)
