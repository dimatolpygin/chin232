"""Что видит юзер, когда ломается не он, а мы.

Этап 9 требует искусственно проверить недоступность каждого из внешних
сервисов. Недоступность в жизни выглядит не аккуратной пятисоткой, а обрывом
соединения или таймаутом, поэтому здесь ломается именно транспорт: клиент
каждого провайдера подменяется на такой, который не доходит до сервера.

Проверяется два уровня: что сбой доезжает наверх опознаваемым (`ProviderError`
с именем провайдера, а не голый httpx где-то в середине круга) и что юзеру при
этом уходит понятный текст, а не молчание.
"""

from __future__ import annotations

import httpx
import pytest

from app.bot.texts import ru
from app.config import Settings
from app.core.providers.base import ProviderError, SpeechUnclear
from app.core.providers.llm.openrouter import OpenRouterLLM
from app.core.providers.payments.lavatop import LavaTopPayments
from app.core.providers.pronunciation.speechsuper import SpeechSuperPronunciation
from app.core.providers.stt.openai_whisper import OpenAIWhisperSTT
from app.core.providers.tts.fish import FishTTS
from app.core.services.dialog import EmptyReply
from app.core.services.recognition import NotRecognized
from app.core.services.turn import ROUND_TTL_SEC, finish_round, start_round
from app.worker.tasks import pronunciation as pron_task
from app.worker.tasks import voice as voice_task

BASE = {
    "bot_token": "123:AAtest",
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "redis_url": "redis://localhost:6379/5",
}

KEYS = {
    "openrouter_api_key": "sk-or-v1-test",
    "openai_api_key": "sk-test",
    "fish_api_key": "fish-test",
    "speechsuper_app_key": "app-test",
    "speechsuper_secret_key": "secret-test",
    "lavatop_api_key": "lava-test",
}


class _Оборванный(httpx.AsyncClient):
    """Клиент, который не доходит до сервера. Ровно то, что видит бот, когда
    сервис лёг: не код ответа, а отсутствие ответа."""

    async def post(self, *args, **kwargs):  # type: ignore[override]
        raise httpx.ConnectError("сервис недоступен")

    async def get(self, *args, **kwargs):  # type: ignore[override]
        raise httpx.ReadTimeout("сервис не ответил вовремя")


@pytest.fixture
def оборвать_сеть(monkeypatch):
    """Подменить общий http-клиент у всех провайдеров разом."""
    клиент = _Оборванный()
    for модуль in (
        "app.core.providers.llm.openrouter",
        "app.core.providers.stt.openai_whisper",
        "app.core.providers.tts.fish",
        "app.core.providers.pronunciation.speechsuper",
        "app.core.providers.payments.lavatop",
    ):
        monkeypatch.setattr(f"{модуль}.get_client", lambda: клиент)
    return клиент


def _settings() -> Settings:
    return Settings(**BASE, **KEYS)  # type: ignore[arg-type]


# --- Недоступность каждого сервиса --------------------------------------------


async def test_диалог_недоступен_опознаётся(оборвать_сеть):
    with pytest.raises(ProviderError) as сбой:
        await OpenRouterLLM(_settings()).complete_json("промпт", [])
    assert сбой.value.provider == "openrouter"


async def test_распознавание_недоступно_опознаётся(оборвать_сеть):
    with pytest.raises(ProviderError) as сбой:
        await OpenAIWhisperSTT(_settings()).transcribe(b"ogg", "voice.ogg")
    assert сбой.value.provider == "openai_whisper"


async def test_озвучка_недоступна_опознаётся(оборвать_сеть):
    with pytest.raises(ProviderError) as сбой:
        await FishTTS(_settings()).synthesize("你好", None, 1.0)
    assert сбой.value.provider == "fish"


async def test_оценка_недоступна_опознаётся(оборвать_сеть):
    with pytest.raises(ProviderError) as сбой:
        await SpeechSuperPronunciation(_settings()).assess(b"wav", "你好", "user-1")
    assert сбой.value.provider == "speechsuper"


async def test_платёжка_недоступна_опознаётся(оборвать_сеть):
    with pytest.raises(ProviderError) as сбой:
        await LavaTopPayments(_settings()).create_invoice("offer", "a@b.ru", "RUB")
    assert сбой.value.provider == "lavatop"


# --- Что при этом видит юзер --------------------------------------------------


class _Бот:
    """Минимальный бот: запоминает, что ушло в чат."""

    def __init__(self) -> None:
        self.сообщения: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.сообщения.append(text)


@pytest.mark.parametrize(
    "провайдер",
    ["openrouter", "openai_whisper", "fish", "speechsuper"],
)
async def test_сбой_любого_сервиса_объясняется_юзеру(провайдер):
    бот = _Бот()
    сбой = ProviderError(провайдер, "call", "недоступен", status_code=None, body=None)
    await voice_task._fail(бот, 1, сбой, "круг")
    assert бот.сообщения == [ru.ERROR_GENERIC]


async def test_нераспознанная_речь_это_просьба_повторить():
    бот = _Бот()
    await voice_task._fail(бот, 1, NotRecognized("тишина"), "круг")
    assert бот.сообщения == [ru.NOT_RECOGNIZED]


async def test_пустой_ответ_модели_просит_переформулировать():
    бот = _Бот()
    await voice_task._fail(бот, 1, EmptyReply("нечего сказать"), "круг")
    assert бот.сообщения == [ru.EMPTY_REPLY]


async def test_заблокировавший_бота_юзер_не_роняет_круг():
    class _Заблокирован(_Бот):
        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            raise RuntimeError("bot was blocked by the user")

    # Ошибка отправки сообщения об ошибке — не повод падать задаче.
    await voice_task._fail(_Заблокирован(), 1, RuntimeError("что угодно"), "круг")


async def _прогнать_оценку(monkeypatch, ошибка: Exception) -> list[str]:
    """Задача оценки от начала до конца, но с заранее сломанным сервисом."""
    бот = _Бот()

    async def скачать(_bot, _file_id):
        return b"ogg"

    async def упасть(*args, **kwargs):
        raise ошибка

    async def тихо(*args, **kwargs):
        return None

    monkeypatch.setattr(pron_task, "download_voice", скачать)
    monkeypatch.setattr(pron_task, "session_scope", _пустая_сессия)
    monkeypatch.setattr(pron_task, "assess_attempt", упасть)
    monkeypatch.setattr(pron_task, "refund_quietly", тихо)
    monkeypatch.setattr(pron_task, "finish_round", тихо)

    await pron_task.process_pronunciation(
        {"bot": бот, "redis": None, "job_id": "j1"},
        user_id="00000000-0000-0000-0000-000000000001",
        chat_id=1,
        file_id="f1",
        dialog_id=7,
        ref_text="你好",
    )
    return бот.сообщения


class _ПустаяСессия:
    async def get(self, *args, **kwargs):
        # Пользователь нужен, чтобы задача дошла до вызова сервиса; всё, что от
        # него требуется дальше, подменено.
        from app.db.models import User

        return User()


def _пустая_сессия():
    class _Контекст:
        async def __aenter__(self):
            return _ПустаяСессия()

        async def __aexit__(self, *exc):
            return False

    return _Контекст()


async def test_сбой_оценки_не_ломает_разговор(monkeypatch):
    сообщения = await _прогнать_оценку(
        monkeypatch, ProviderError("speechsuper", "pronunciation", "недоступен")
    )
    assert сообщения == [ru.PRON_FAILED]


async def test_неразборчивая_запись_просит_повторить(monkeypatch):
    сообщения = await _прогнать_оценку(monkeypatch, SpeechUnclear("шум"))
    assert сообщения == [ru.PRON_UNCLEAR]


async def test_любая_другая_ошибка_оценки_объясняется(monkeypatch):
    сообщения = await _прогнать_оценку(monkeypatch, RuntimeError("что угодно"))
    assert сообщения == [ru.ERROR_GENERIC]


# --- Замок круга ---------------------------------------------------------------


class _Redis:
    """Настолько redis, насколько нужно замку: SET NX EX и DELETE."""

    def __init__(self) -> None:
        self.ключи: dict[str, str] = {}
        self.сроки: dict[str, int] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and key in self.ключи:
            return None
        self.ключи[key] = value
        if ex is not None:
            self.сроки[key] = ex
        return True

    async def delete(self, key: str) -> int:
        self.сроки.pop(key, None)
        return 1 if self.ключи.pop(key, None) is not None else 0


async def test_вторая_реплика_не_запускает_второй_круг():
    очередь = _Redis()
    assert await start_round(очередь, "u1") is True
    # Юзер сказал вторую фразу, не дождавшись ответа на первую. Круг занят —
    # значит второго платного вызова к четырём сервисам не будет.
    assert await start_round(очередь, "u1") is False


async def test_чужой_круг_не_мешает():
    очередь = _Redis()
    assert await start_round(очередь, "u1") is True
    assert await start_round(очередь, "u2") is True


async def test_после_круга_замок_снимается():
    очередь = _Redis()
    await start_round(очередь, "u1")
    await finish_round(очередь, "u1")
    assert await start_round(очередь, "u1") is True


async def test_замок_протухает_сам():
    # Воркер может умереть между постановкой задачи и её концом. Без срока
    # юзер остался бы запертым навсегда.
    очередь = _Redis()
    await start_round(очередь, "u1")
    ключ = next(iter(очередь.сроки))
    assert очередь.сроки[ключ] == ROUND_TTL_SEC
    # Дольше таймаута задачи, иначе честный долгий круг разлочит сам себя.
    assert ROUND_TTL_SEC > 120


async def test_снятие_замка_переживает_упавший_redis():
    class _Мёртвый(_Redis):
        async def delete(self, key: str) -> int:
            raise RuntimeError("connection refused")

    # Круг уже отработал и ответ юзеру ушёл: падать на уборке нельзя.
    await finish_round(_Мёртвый(), "u1")


async def test_без_очереди_замок_не_мешает_работать():
    # Задача может прийти без redis в контексте — это не повод запрещать круг.
    assert await start_round(None, "u1") is True
    await finish_round(None, "u1")
