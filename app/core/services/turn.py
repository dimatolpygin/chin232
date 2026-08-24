"""Замок «один круг за раз».

Кнопки от дублей закрыты своими замками, а вот запись голосового не закрыта
ничем: юзер говорит фразу, не дожидается ответа и говорит вторую. Телеграм
доставляет оба апдейта, бот честно ставит два круга, и оба оплачиваются на
всех четырёх сервисах — при том, что ответ на первый юзер ещё даже не слышал.

Поэтому пока круг в работе, следующая реплика не принимается: юзеру говорится,
что ответ уже готовится, и его сообщение не списывает норму.

Замок ставит бот перед постановкой задачи, снимает воркер, когда круг
закончился — чем бы он ни закончился. TTL страхует случай, когда воркер умер
между этими двумя моментами: замок протухнет сам, а не запрёт человека
навсегда. Отсюда и величина — чуть больше таймаута задачи, чтобы честный
долгий круг не разлочился у себя под ногами.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.logging import get_logger

log = get_logger("turn")

# Таймаут задачи в воркере — 120 секунд. Замок живёт чуть дольше.
ROUND_TTL_SEC = 150


def _key(user_id: str) -> str:
    # Префикс обязателен: redis общий с чужими проектами.
    return get_settings().redis_key("round", user_id)


async def start_round(queue: Any, user_id: str) -> bool:
    """Занять круг. False — предыдущий ещё считается."""
    if queue is None:
        return True
    return bool(await queue.set(_key(user_id), "1", nx=True, ex=ROUND_TTL_SEC))


async def finish_round(queue: Any, user_id: str) -> None:
    """Освободить круг. Зовётся и после удачи, и после любой аварии."""
    if queue is None:
        return
    try:
        await queue.delete(_key(user_id))
    except Exception as exc:  # noqa: BLE001  снятие замка не главнее ответа юзеру
        log.warning("не удалось снять замок круга", user_id=user_id, ошибка=repr(exc))
