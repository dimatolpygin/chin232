"""Настройка structlog.

Требование заказчика — «каждая щель под отладку». Поэтому:
  • локально человекочитаемый вывод, на сервере JSON;
  • в каждой записи `request_id` и `user_id` через contextvars, чтобы одна
    реплика собиралась в цепочку от входящего голосового до отправленного ответа;
  • секреты маскируются процессором и в логи не попадают никогда.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

# Ключи, значения которых маскируются целиком.
SECRET_KEYS = {
    "token",
    "bot_token",
    "api_key",
    "apikey",
    # Заголовок, которым подписывается вебхук платёжки: он же ключ API.
    "x-api-key",
    "secret",
    "secret_key",
    "password",
    "authorization",
    "app_key",
    # Доступ к хранилищу копий: по этой паре открывается бакет целиком, причём
    # общий с другими проектами.
    "access_key",
    "s3_access_key",
    "s3_secret_key",
}

# Секреты, попавшие в текст сообщения (тело ответа сервиса, traceback и т.п.).
SECRET_PATTERNS = [
    # Ключи вида sk-or-v1-…, sk-proj-…, sk-fish-…: оставляем опознавательный
    # префикс, остальное режем.
    re.compile(r"\b(sk-[A-Za-z0-9]{0,4})[A-Za-z0-9_\-]{6,}"),
    # Токен Telegram. Якорь \b здесь ставить нельзя: в URL токен идёт сразу
    # после "bot" (…/bot123456789:AAxxx/sendMessage), границы слова там нет,
    # и токен утёк бы в лог целиком.
    re.compile(r"(\d{6,12}:AA)[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9_\-.]{8,}"),
]

MASK = "***"


def _mask_text(value: str) -> str:
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(lambda m: m.group(1) + MASK, value)
    return value


def mask_secrets(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Процессор structlog: вычищает секреты из значений и из текста события."""
    for key, value in list(event_dict.items()):
        if key.lower() in SECRET_KEYS and value:
            event_dict[key] = MASK
        elif isinstance(value, str):
            event_dict[key] = _mask_text(value)
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    # Библиотеки не должны забивать вывод своим debug-шумом.
    for noisy in ("aiogram.event", "asyncio", "aiosqlite", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=True)
        if fmt == "console"
        else structlog.processors.JSONRenderer(ensure_ascii=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%d.%m.%Y %H:%M:%S", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Строго ПОСЛЕ отрисовки traceback. Иначе маскировка его не видит:
            # исключение к этому моменту лежит в event_dict объектом, а строкой
            # становится ниже — и уходит в лог как есть. Живой случай: файловый
            # сервер Telegram отвечает 404, httpx кладёт в текст ошибки полный
            # адрес запроса, а в нём токен бота. Само поле «ошибка» было
            # замаскировано, а тот же токен в трейсбеке — нет.
            mask_secrets,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


def new_request_id() -> str:
    """Короткий идентификатор реплики: по нему собирается вся цепочка."""
    return uuid.uuid4().hex[:12]


def bind_request(request_id: str | None = None, **extra: Any) -> str:
    """Привязать контекст к текущей задаче. Возвращает request_id."""
    rid = request_id or new_request_id()
    bind_contextvars(request_id=rid, **extra)
    return rid


def clear_request() -> None:
    clear_contextvars()
