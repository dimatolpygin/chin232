"""Пиньинь со знаками тонов локально, без обращения к платным сервисам.

Страховка для кнопки «Текст»: обычно пиньинь приходит от модели вместе с
ответом и лежит в `dialogs`, но модель уже ломала формат JSON, и в таком ответе
поля pinyin нет. Кнопка при этом обязана показать тоны — значит нужен источник,
который не стоит денег и не может отказать.
"""

from __future__ import annotations

from app.logging import get_logger

log = get_logger("pinyin")

try:  # pragma: no cover — ветка зависит от окружения, а не от логики
    from pypinyin import Style
    from pypinyin import pinyin as _pinyin

    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False


def to_pinyin_list(text: str) -> list[str]:
    """Пиньинь по слогам, в порядке иероглифов исходной строки.

    Считается по фразе целиком, а не по одному знаку: тон третьего в 你好 и
    тон 不 перед четвёртым зависят от соседей, посимвольный вызов их теряет.
    """
    if not text or not AVAILABLE:
        return []
    try:
        parts = _pinyin(text, style=Style.TONE, errors="ignore")
    except Exception as exc:  # noqa: BLE001  разбор текста не должен ронять кнопку
        log.warning("не удалось посчитать пиньинь локально", ошибка=repr(exc))
        return []
    return [p[0] for p in parts if p and p[0]]


def to_pinyin(text: str) -> str:
    """Пиньинь со знаками тонов (nǐ hǎo). Пустая строка, если посчитать нечем."""
    return " ".join(to_pinyin_list(text)).strip()
