"""Конфигурация приложения. Всё читается из переменных окружения.

При отсутствии обязательной переменной приложение падает на старте с внятным
сообщением на русском, а не позже, где-нибудь в середине голосового круга.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# Человеческие названия переменных для сообщения об ошибке.
FIELD_TITLES: dict[str, str] = {
    "bot_token": "BOT_TOKEN — токен Telegram-бота от @BotFather",
    "database_url": "DATABASE_URL — строка подключения к postgres (postgresql+asyncpg://...)",
    "redis_url": "REDIS_URL — строка подключения к redis (redis://host:port/номер_базы)",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- окружение ---
    env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # --- обязательные ---
    # min_length=1: пустая переменная в .env — ошибка более частая, чем забытая,
    # и молча стартовать с пустым токеном нельзя.
    bot_token: str = Field(min_length=1)
    database_url: str = Field(min_length=1)
    redis_url: str = Field(min_length=1)

    # --- redis ---
    redis_prefix: str = "china:"

    # Куда платёжка возвращает человека после оплаты. Ссылка на бота, а не на
    # страницу платёжки: оплатил он в боте, туда и должен вернуться. Задаётся
    # в счёте, а не только настройкой в кабинете, чтобы не зависеть от неё.
    bot_link: str = "https://t.me/ChineseToneBot"

    # --- api ---
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # --- выбор провайдеров (этап 1 и дальше) ---
    llm_provider: str = "openrouter"
    stt_provider: str = "openai_whisper"
    tts_provider: str = "fish"
    # Запасная озвучка на случай сбоя основной. Пусто — фолбэка нет.
    tts_fallback_provider: str | None = "openai"
    pronunciation_provider: str = "speechsuper"
    payment_provider: str = "lavatop"

    # --- ключи внешних сервисов, на этапе 0 не обязательны ---
    openrouter_api_key: str | None = None
    openrouter_model: str = "openai/gpt-4o-mini"
    openai_api_key: str | None = None
    whisper_model: str = "whisper-1"
    openai_tts_model: str = "tts-1"
    openai_tts_voice: str = "alloy"
    fish_api_key: str | None = None
    fish_model: str = "s1"
    # Голос Fish задаётся обязательно. Без reference_id сервис синтезирует
    # каждый раз НОВЫЙ случайный голос — на живой проверке пользователь
    # насчитал три разных собеседника подряд. Голос выбран заказчиком на слух
    # из четырёх образцов: 温柔叙事女声 — мягкий женский, повествовательный,
    # не клон реального человека.
    fish_voice_id: str | None = "23632847285a487d8e0c6ae5bc593c71"
    speechsuper_app_key: str | None = None
    # Тариф оценки. На обычном (`sent.eval.cn`) приходит балл тона на иероглиф,
    # на promax — ещё и услышанный тон. Аккаунту выдают конкретный набор, и
    # чужой coreType сервис отвергает, поэтому это настройка, а не константа.
    speechsuper_core_type: str = "sent.eval.cn"
    speechsuper_secret_key: str | None = None
    lavatop_api_key: str | None = None
    lavatop_api_url: str = "https://gate.lava.top"
    # Секрет вебхука. Его задаём мы сами при добавлении вебхука в кабинете
    # lava.top (тип аутентификации «API key вашего сервиса», до 80 символов),
    # и платёжка присылает его заголовком X-Api-Key. Пусто — сверяем с ключом
    # API, тогда в кабинете вписывается он же.
    lavatop_webhook_secret: str | None = None

    # --- голосовой круг ---
    provider_timeout: float = 60.0
    # Сколько прошлых реплик отдаём модели: длиннее — дороже и медленнее.
    dialog_history_limit: int = 10
    # Потолок длины голосового от юзера, секунды.
    max_voice_duration_sec: int = 120

    # --- копии базы на стороне ---
    #
    # Хранилище S3-совместимое и **общее с другими проектами**: свой префикс
    # обязателен, иначе копии перемешаются с чужими файлами. Пусто — задача
    # бэкапа не запускается и честно пишет об этом в лог, а не падает каждую
    # ночь.
    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "ru1"
    s3_prefix: str = "a_clients_project_2026/china_bot_backaps/"

    # Глубина хранения копий. Семь ежедневных отвечают на «вчера всё было
    # хорошо», четыре воскресных — на «когда именно это сломалось». Дамп этой
    # базы весит единицы мегабайт, так что одиннадцать копий занимают меньше,
    # чем одна фотография.
    backup_keep_daily: int = 7
    backup_keep_weekly: int = 4

    # --- админы ---
    admin_ids: str = ""

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def admin_id_list(self) -> list[int]:
        return [int(part) for part in (p.strip() for p in self.admin_ids.split(",")) if part]

    def redis_key(self, *parts: str) -> str:
        """Ключ redis с обязательным префиксом проекта: redis общий с чужими проектами."""
        return self.redis_prefix + ":".join(parts)


def _fail(errors: list[str]) -> None:
    text = "\n".join(f"  • {line}" for line in errors)
    sys.stderr.write(
        "\n"
        "╔══════════════════════════════════════════════════════════════╗\n"
        "║  ЗАПУСК ОСТАНОВЛЕН: неверная конфигурация окружения          ║\n"
        "╚══════════════════════════════════════════════════════════════╝\n"
        f"{text}\n\n"
        "Проверьте файл .env (образец — .env.example) или переменные окружения\n"
        "контейнера, затем запустите заново.\n\n"
    )
    raise SystemExit(1)


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        errors: list[str] = []
        for err in exc.errors():
            field = str(err["loc"][0]) if err["loc"] else "?"
            title = FIELD_TITLES.get(field, field.upper())
            if err["type"] == "missing":
                errors.append(f"не задана обязательная переменная {title}")
            else:
                errors.append(f"переменная {title}: {err['msg']}")
        _fail(errors)
        raise  # недостижимо, нужно для типизации
