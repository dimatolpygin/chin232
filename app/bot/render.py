"""Подготовка текста к отправке в Telegram.

Разметка у бота HTML, а в подписи и подсказки попадает текст от модели. Один
угловой скобкой в переводе Telegram отвечает 400 и сообщение не уходит вовсе —
поэтому всё чужое экранируется на границе отправки.
"""

from __future__ import annotations

import html


def esc(value: str | None) -> str:
    return html.escape(value or "", quote=False)
