"""Общий HTTP-клиент для всех внешних сервисов.

Клиент один на процесс и живёт до остановки: на каждый вызов заново поднимать
TLS-соединение — это лишние сотни миллисекунд в круге, который и так упирается
в 12 секунд.
"""

from __future__ import annotations

import httpx

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=300.0),
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# Хосты внешних сервисов. Прогрев — не украшательство: холодный круг с новым
# TLS-рукопожатием на каждый сервис занимал 10.4 секунды против 2.5 на тёплых
# соединениях, и хуже всех приходилось именно первому сообщению пользователя.
WARMUP_HOSTS = (
    "https://api.openai.com/v1/models",
    "https://openrouter.ai/api/v1/models",
    "https://api.fish.audio/",
    "https://api.speechsuper.com/",
    # Telegram здесь не лишний: воркер скачивает оттуда голосовые.
    "https://api.telegram.org/",
)


async def warmup() -> None:
    """Поднять TLS-соединения заранее. Ответы не важны, важен сам коннект."""
    import asyncio

    from app.logging import get_logger

    log = get_logger("providers")
    client = get_client()

    async def touch(url: str) -> str:
        try:
            response = await client.get(url, timeout=10.0)
            return f"{url} → {response.status_code}"
        except Exception as exc:  # noqa: BLE001  прогрев не должен ничего ронять
            return f"{url} → недоступен: {exc!r}"

    results = await asyncio.gather(*(touch(url) for url in WARMUP_HOSTS))
    log.info("соединения прогреты", результаты=", ".join(results))
