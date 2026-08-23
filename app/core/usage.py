"""Расход на внешние сервисы: сколько вызовов и на сколько денег.

Заказчику обещано «видно расход по каждому сервису». Считать его по логам
нельзя: логи ротируются, а вопрос «сколько ушло за месяц» задают уже после
ротации. Поэтому каждый вызов внешнего сервиса ложится строкой в
`service_calls`.

Стоимость здесь — **оценка по опубликованным прайсам**, а не счёт от
провайдера: у OpenRouter она зависит от модели, Fish считает байты UTF-8,
SpeechSuper берёт фиксированную сумму за запрос. Годится ответить «что дороже
всего и куда растёт», а не сводить бухгалтерию.

Запись не должна тормозить голосовой круг, поэтому вызовы копятся в памяти
процесса и уходят в базу попутно — на ближайшем коммите чужой сессии.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.contextvars import get_contextvars

from app.logging import get_logger

log = get_logger("usage")

# --- прайсы, доллары -----------------------------------------------------------
#
# Даты и числа на 08.2026. Провайдеры меняют цены без предупреждения, поэтому
# всё лежит рядом и правится одной строкой, а не ищется по коду.

# OpenRouter, openai/gpt-4o-mini: $0.15 за миллион входных токенов и $0.60 за
# миллион выходных. Другая модель — другая цена, поэтому число приблизительное
# по определению.
PRICE_LLM_INPUT = 0.15 / 1_000_000
PRICE_LLM_OUTPUT = 0.60 / 1_000_000

# Whisper: $0.006 за минуту звука, округление вверх до секунды.
PRICE_WHISPER_SEC = 0.006 / 60

# OpenAI tts-1: $15 за миллион символов.
PRICE_TTS_CHAR = 15.0 / 1_000_000

# Fish Audio S1: $15 за миллион байт UTF-8. Иероглиф — три байта, поэтому
# китайская фраза стоит втрое дороже, чем кажется по числу знаков.
PRICE_FISH_BYTE = 15.0 / 1_000_000
BYTES_PER_HANZI = 3

# SpeechSuper: фиксированная цена за запрос, длина фразы не влияет.
PRICE_SPEECHSUPER_CALL = 0.006

# Единицы измерения. Показываются админу как есть, поэтому по-русски.
UNIT_TOKENS = "токенов"
UNIT_SECONDS = "секунд"
UNIT_CHARS = "знаков"
UNIT_CALLS = "запросов"


@dataclass(slots=True, frozen=True)
class Cost:
    """Во что обошёлся один вызов."""

    units: float
    unit: str
    cost: float


@dataclass(slots=True, frozen=True)
class Call:
    """Один вызов внешнего сервиса, ждущий записи в базу."""

    provider: str
    operation: str
    ok: bool
    ms: int
    units: float
    unit: str
    cost: float
    user_id: uuid.UUID | None
    at: datetime


def _number(fields: dict[str, Any], key: str) -> float:
    value = fields.get(key)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _llm(fields: dict[str, Any]) -> Cost:
    вход = _number(fields, "токенов_вход")
    выход = _number(fields, "токенов_выход")
    return Cost(вход + выход, UNIT_TOKENS, вход * PRICE_LLM_INPUT + выход * PRICE_LLM_OUTPUT)


def _whisper(fields: dict[str, Any]) -> Cost:
    секунд = _number(fields, "секунд")
    return Cost(секунд, UNIT_SECONDS, секунд * PRICE_WHISPER_SEC)


def _fish(fields: dict[str, Any]) -> Cost:
    знаков = _number(fields, "знаков")
    return Cost(знаков, UNIT_CHARS, знаков * BYTES_PER_HANZI * PRICE_FISH_BYTE)


def _openai_tts(fields: dict[str, Any]) -> Cost:
    знаков = _number(fields, "знаков")
    return Cost(знаков, UNIT_CHARS, знаков * PRICE_TTS_CHAR)


def _speechsuper(_fields: dict[str, Any]) -> Cost:
    return Cost(1, UNIT_CALLS, PRICE_SPEECHSUPER_CALL)


def _free(_fields: dict[str, Any]) -> Cost:
    """Платёжка денег с нас не берёт: её процент сидит внутри платежа."""
    return Cost(1, UNIT_CALLS, 0.0)


# Ключ — имя провайдера, то самое, что стоит в `ProviderName.name`.
PRICING: dict[str, Any] = {
    "openrouter": _llm,
    "openai_whisper": _whisper,
    "fish": _fish,
    "openai": _openai_tts,
    "speechsuper": _speechsuper,
    "lavatop": _free,
}


def estimate(provider: str, fields: dict[str, Any]) -> Cost:
    """Во что обошёлся вызов. Незнакомый сервис считаем бесплатным, но считаем.

    Молча выкидывать вызов нельзя: новый провайдер появляется раньше, чем его
    прайс, и до тех пор в админке важно видеть хотя бы число обращений.
    """
    rule = PRICING.get(provider)
    if rule is None:
        return Cost(1, UNIT_CALLS, 0.0)
    return rule(fields)


# --- буфер ---------------------------------------------------------------------
#
# Список на процесс, а не contextvar: копить нужно всё, что случилось, а не то,
# что случилось в конкретной задаче. Пишет и забирает один цикл событий,
# блокировка не нужна.

_pending: list[Call] = []

# Потолок на случай, если база недоступна долго: расход — не деньги юзера,
# ронять процесс ростом памяти ради него нельзя.
MAX_PENDING = 5000


def _current_user() -> uuid.UUID | None:
    """Чей это вызов. Берём из привязки логов: там уже лежит user_id реплики."""
    raw = get_contextvars().get("user_id")
    try:
        return uuid.UUID(str(raw)) if raw else None
    except (TypeError, ValueError):
        return None


def note(provider: str, operation: str, ok: bool, ms: int, fields: dict[str, Any]) -> None:
    """Запомнить вызов. Ничего не ждёт и не может ничего уронить."""
    цена = estimate(provider, fields)
    if len(_pending) >= MAX_PENDING:
        # Выкидываем самое старое: свежий расход полезнее вчерашнего.
        _pending.pop(0)
        log.warning("журнал расхода переполнен, старые записи вытесняются", предел=MAX_PENDING)
    _pending.append(
        Call(
            provider=provider,
            operation=operation,
            ok=ok,
            ms=ms,
            units=цена.units,
            unit=цена.unit,
            cost=цена.cost,
            user_id=_current_user(),
            at=datetime.now(UTC),
        )
    )


def take() -> list[Call]:
    """Забрать накопленное. Вызывающий обязан вернуть его, если не записал."""
    global _pending
    taken, _pending = _pending, []
    return taken


def give_back(rows: list[Call]) -> None:
    """Вернуть незаписанное в очередь: сессия откатилась, а деньги потрачены."""
    if rows:
        _pending[:0] = rows


def pending_count() -> int:
    return len(_pending)


async def write(session: AsyncSession, rows: list[Call]) -> None:
    """Записать вызовы. Одним запросом: их по три на каждый голосовой круг."""
    if not rows:
        return
    await session.execute(
        text(
            "INSERT INTO service_calls "
            "(provider, operation, user_id, ok, ms, units, unit, cost, created_at) "
            "VALUES (:provider, :operation, :user_id, :ok, :ms, :units, :unit, :cost, :created_at)"
        ),
        [
            {
                "provider": row.provider,
                "operation": row.operation,
                "user_id": row.user_id,
                "ok": row.ok,
                "ms": row.ms,
                "units": round(row.units, 3),
                "unit": row.unit,
                "cost": round(row.cost, 6),
                "created_at": row.at,
            }
            for row in rows
        ],
    )


async def flush_now() -> None:
    """Дописать хвост своей сессией. Нужен там, где чужого коммита не будет.

    Импорт внутри функции: `app.db.session` сам сливает буфер на коммите, и на
    верхнем уровне это был бы круг импортов.
    """
    if not _pending:
        return
    from app.db.session import session_scope

    async with session_scope():
        pass  # запись делает сам session_scope, здесь важен только коммит
