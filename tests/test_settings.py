"""Раздел «Настройки»: уровень, голос, скорость, тема.

Настройки — единственное место, где юзер меняет поведение бота руками, и
ошибка здесь не падает, а тихо озвучивает не тем голосом или не тем темпом.
Поэтому проверяем не «функция вызвалась», а что именно легло в `users` и что
увидел юзер на экране.
"""

from __future__ import annotations

import pytest

from app.bot.keyboards.menu import MENU_BUTTONS, main_menu
from app.bot.keyboards.settings import (
    ACTION_OPEN,
    ACTION_PICK,
    ACTION_PLAY,
    SECTION_LEVEL,
    SECTION_VOICE,
    level_keyboard,
    parse_settings_action,
    speed_keyboard,
    topic_keyboard,
    voice_keyboard,
)
from app.bot.texts import ru
from app.config import Settings
from app.core.providers.base import Speech, TTSProvider, Voice
from app.core.providers.registry import tts_voices
from app.core.providers.tts.fish import VOICES as FISH_VOICES
from app.core.services import dialog as dialog_service
from app.core.services import settings as settings_service
from app.core.services.settings import (
    SPEEDS,
    TOPICS,
    current_speed,
    current_topic,
    current_voice,
    set_speed,
    set_topic,
    set_voice,
)
from app.db.models import User

BASE = {
    "bot_token": "123:AAtest",
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "redis_url": "redis://localhost:6379/5",
}

CATALOGUE = (
    Voice("v-one", "Женский мягкий", "спокойный"),
    Voice("v-two", "Мужской дикторский", "чёткий"),
)


class FakeSession:
    """Ровно то, что нужно сервису настроек: `add` для событий и `flush`."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    @property
    def events(self) -> list[str]:
        return [getattr(o, "type", "") for o in self.added]


def _user(**fields) -> User:
    user = User()
    user.hsk_level = fields.get("level", "hsk12")
    user.voice_id = fields.get("voice_id")
    user.speech_speed = fields.get("speed", 1.0)
    user.topic = fields.get("topic")
    return user


@pytest.fixture
def каталог(monkeypatch):
    monkeypatch.setattr(settings_service, "tts_voices", lambda _s=None: CATALOGUE)
    return CATALOGUE


# --- скорость речи ------------------------------------------------------------


def test_скорость_читается_ближайшим_шагом():
    # В базе лежит множитель, а не код: ищем ближайший шаг, чтобы настройка не
    # потерялась, если шаги когда-нибудь сдвинутся.
    assert current_speed(_user(speed=0.8)).code == "slow"
    assert current_speed(_user(speed=1.0)).code == "normal"
    assert current_speed(_user(speed=1.2)).code == "fast"
    assert current_speed(_user(speed=0.83)).code == "slow"


def test_пустая_скорость_считается_обычной():
    assert current_speed(_user(speed=None)).code == "normal"


async def test_выбор_скорости_меняет_множитель():
    session, user = FakeSession(), _user(speed=1.0)
    chosen = await set_speed(session, user, "slow")
    assert chosen.value == 0.8
    assert user.speech_speed == 0.8
    assert session.flushed == 1
    assert "speed_set" in session.events


async def test_неизвестная_скорость_падает_на_обычную():
    session, user = FakeSession(), _user(speed=1.2)
    chosen = await set_speed(session, user, "чепуха")
    assert chosen.code == "normal"
    assert user.speech_speed == 1.0


async def test_скорость_доезжает_до_озвучки():
    """Настройка должна быть слышна, а не просто лежать в базе."""
    session, user = FakeSession(), _user(level="hsk56")
    await set_speed(session, user, "slow")
    медленно = dialog_service._speed(user)
    await set_speed(session, user, "fast")
    быстро = dialog_service._speed(user)
    assert медленно < быстро


# --- тема разговора -----------------------------------------------------------


def test_тема_читается_по_сохранённой_фразе():
    еда = next(t for t in TOPICS if t.code == "food")
    assert current_topic(_user(topic=еда.prompt)).code == "food"


def test_пустая_и_чужая_тема_считаются_свободной():
    assert current_topic(_user(topic=None)).code == "free"
    assert current_topic(_user(topic="что-то из прошлой версии")).code == "free"


async def test_выбор_темы_кладёт_в_базу_фразу_для_промпта():
    session, user = FakeSession(), _user()
    chosen = await set_topic(session, user, "road")
    # В промпт уходит поле как есть, поэтому там фраза для модели, а не код.
    assert user.topic == chosen.prompt
    assert "дорога" in user.topic
    assert "topic_set" in session.events


async def test_свободная_тема_стирает_поле():
    session, user = FakeSession(), _user(topic="еда и кафе: заказ блюд, вкусы, счёт")
    chosen = await set_topic(session, user, "free")
    assert chosen.code == "free"
    # Не строка «свободная», а пусто: своё слово подставит сам промпт.
    assert user.topic is None


# --- голос --------------------------------------------------------------------


def test_голос_по_умолчанию_никак_не_отмечен(каталог):
    assert current_voice(_user(voice_id=None)) is None


def test_чужой_идентификатор_голоса_не_показывается_выбранным(каталог):
    """После смены сервиса озвучки сохранённый id ничего не значит.

    Показать его выбранным — соврать: галочка стояла бы у голоса, которым бот
    не говорит.
    """
    assert current_voice(_user(voice_id="id-от-другого-сервиса")) is None


def test_голос_из_каталога_находится(каталог):
    assert current_voice(_user(voice_id="v-two")).title == "Мужской дикторский"


async def test_выбор_голоса_сохраняется(каталог):
    session, user = FakeSession(), _user()
    chosen = await set_voice(session, user, "v-one")
    assert chosen.id == "v-one"
    assert user.voice_id == "v-one"
    assert "voice_set" in session.events


async def test_голос_не_из_каталога_не_сохраняется(каталог):
    """Иначе следующий же ответ уйдёт в сервис озвучки с чужим id и упадёт."""
    session, user = FakeSession(), _user(voice_id="v-one")
    assert await set_voice(session, user, "подделка") is None
    assert user.voice_id == "v-one"
    assert session.events == []


async def test_образец_звучит_выбранным_голосом_а_не_текущим(monkeypatch):
    """«Прослушать» нажимают до выбора: образец обязан идти чужим голосом."""
    прозвучал = {}

    class _TTS(TTSProvider):
        name = "fish"

        async def synthesize(self, text: str, voice_id: str | None, speed: float) -> Speech:
            прозвучал["voice_id"] = voice_id
            return Speech(audio=b"mp3", fmt="mp3")

    async def fake_convert(data: bytes, source_format: str = "mp3") -> bytes:
        return b"ogg"

    monkeypatch.setattr(dialog_service, "get_tts", lambda _s: _TTS())
    monkeypatch.setattr("app.core.audio.to_voice_ogg", fake_convert)

    settings = Settings(**BASE)  # type: ignore[arg-type]
    await dialog_service.synthesize_voice(
        "你好", _user(voice_id="свой-голос"), settings, voice_id="образец"
    )
    assert прозвучал["voice_id"] == "образец"


def test_каталог_голосов_без_дублей_и_с_описаниями():
    ids = [v.id for v in FISH_VOICES]
    assert len(ids) == len(set(ids)), "один и тот же голос дважды в списке"
    assert all(v.title and v.note for v in FISH_VOICES)
    assert len(FISH_VOICES) >= 4, "выбирать не из чего"


def test_каталог_берётся_у_действующего_провайдера():
    fish = Settings(tts_provider="fish", **BASE)  # type: ignore[arg-type]
    openai = Settings(tts_provider="openai", **BASE)  # type: ignore[arg-type]
    # Идентификаторы у сервисов разные, и списки не должны пересекаться.
    assert {v.id for v in tts_voices(fish)} & {v.id for v in tts_voices(openai)} == set()
    assert tts_voices(Settings(tts_provider="чепуха", **BASE)) == ()  # type: ignore[arg-type]


# --- кнопки -------------------------------------------------------------------


def test_разбор_нажатий():
    открыть = parse_settings_action("set:voice")
    assert (открыть.section, открыть.action) == (SECTION_VOICE, ACTION_OPEN)

    выбор = parse_settings_action("set:level:hsk34")
    assert (выбор.section, выбор.action, выбор.value) == (SECTION_LEVEL, ACTION_PICK, "hsk34")

    образец = parse_settings_action("set:voice:play:abc123")
    assert (образец.action, образец.value) == (ACTION_PLAY, "abc123")

    assert parse_settings_action("sub:pay:monthly") is None
    assert parse_settings_action("") is None


def test_выбранное_отмечено_галочкой():
    тексты = [b.text for row in level_keyboard("hsk34").inline_keyboard for b in row]
    assert any(t.startswith("✅") and "HSK 3-4" in t for t in тексты)
    assert sum(t.startswith("✅") for t in тексты) == 1

    темп = [b.text for row in speed_keyboard("fast").inline_keyboard for b in row]
    assert sum(t.startswith("✅") for t in темп) == 1


def test_у_каждого_голоса_есть_кнопка_прослушать():
    rows = voice_keyboard(CATALOGUE, "v-one").inline_keyboard
    голоса = rows[:-1]
    assert len(голоса) == len(CATALOGUE)
    for row in голоса:
        assert len(row) == 2
        assert row[1].text == ru.BTN_VOICE_PLAY
        assert f":{ACTION_PLAY}:" in row[1].callback_data
    # Последняя строка — «назад», иначе из раздела не выйти.
    assert rows[-1][0].text == ru.BTN_BACK


def test_все_темы_попадают_на_клавиатуру():
    коды = [
        b.callback_data.split(":")[-1]
        for row in topic_keyboard("free").inline_keyboard[:-1]
        for b in row
    ]
    assert коды == [t.code for t in TOPICS]


def test_шаги_скорости_различимы_на_слух():
    # Шаг в 10% на живой проверке от обычного темпа не отличался.
    значения = sorted(s.value for s in SPEEDS)
    assert all(b / a >= 1.15 for a, b in zip(значения, значения[1:], strict=False))


def test_нижнее_меню_ведёт_в_существующие_разделы():
    """Регрессия: надпись на кнопке и фильтр хендлера — одна константа.

    Разойдутся хоть на пробел — кнопка уедет в разговорный роутер и бот ответит
    на неё по-китайски.
    """
    ряды = [[b.text for b in row] for row in main_menu().keyboard]
    assert ряды == [[ru.MENU_TALK, ru.MENU_PROFILE], [ru.MENU_SUBSCRIPTION]]
    assert [t for row in ряды for t in row] == list(MENU_BUTTONS)
