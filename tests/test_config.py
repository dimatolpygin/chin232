"""Конфиг обязан падать на старте, а не позже."""

from __future__ import annotations

import pytest

from app.config import Settings, get_settings


def _env(monkeypatch, **values: str) -> None:
    for key in ("BOT_TOKEN", "DATABASE_URL", "REDIS_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


FULL = {
    "BOT_TOKEN": "123:AAtest",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "REDIS_URL": "redis://localhost:6379/5",
}


def test_отсутствие_обязательной_переменной_роняет_старт(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # чтобы не подхватился .env проекта
    _env(monkeypatch, DATABASE_URL=FULL["DATABASE_URL"], REDIS_URL=FULL["REDIS_URL"])
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as exc:
        get_settings()
    assert exc.value.code == 1
    get_settings.cache_clear()


def test_полный_набор_переменных_читается(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _env(monkeypatch, **FULL)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.bot_token == FULL["BOT_TOKEN"]
    assert settings.is_dev
    get_settings.cache_clear()


def test_ключи_redis_всегда_с_префиксом():
    settings = Settings(**{k.lower(): v for k, v in FULL.items()})  # type: ignore[arg-type]
    assert settings.redis_key("limit", "42").startswith("china:")


def test_список_админов_разбирается():
    settings = Settings(admin_ids=" 1, 2 ,3 ", **{k.lower(): v for k, v in FULL.items()})  # type: ignore[arg-type]
    assert settings.admin_id_list == [1, 2, 3]
