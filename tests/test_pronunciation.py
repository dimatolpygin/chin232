"""Оценка произношения: разбор ответа сервиса, выбор эталона, отрисовка тонов."""

from __future__ import annotations

import json
import uuid

import pytest

from app.bot.keyboards.answer import (
    ACTION_AGAIN,
    ACTION_LISTEN,
    ACTION_PRON,
    answer_keyboard,
    parse_action,
    practice_keyboard,
    result_keyboard,
)
from app.bot.render import render_practice, render_result
from app.config import Settings
from app.core.providers.base import (
    CharScore,
    Pronunciation,
    ProviderError,
    SpeechUnclear,
)
from app.core.providers.pronunciation import speechsuper
from app.core.providers.pronunciation.speechsuper import (
    SpeechSuperPronunciation,
    _parse,
    _sha1,
)
from app.core.services import pronunciation as pron
from app.core.services.breakdown import ReplyNotFound
from app.db.models import Dialog, User

BASE = {
    "bot_token": "123:AAtest",
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "redis_url": "redis://localhost:6379/5",
}

# Ответ в том виде, в каком он приходит на самом деле: тон — строкой «tone3»,
# пиньинь со знаками тонов считает сам сервис, пунктуация идёт отдельным
# «словом» с charType 1. Снято с живого вызова sent.eval.cn.
ANSWER = {
    "result": {
        "overall": 78,
        "pronunciation": 85,
        "tone": 64,
        "fluency": 90,
        "integrity": 100,
        "words": [
            {
                "word": "你",
                "charType": 0,
                "tone": "tone3",
                "symbolpinyin": "nǐ",
                "scores": {"overall": 92, "tone": 95, "overall_pron": 92},
            },
            {
                "word": "好",
                "charType": 0,
                "tone": "tone3",
                "symbolpinyin": "hǎo",
                "scores": {"overall": 48, "tone": 12, "overall_pron": 60},
            },
            {"word": "！", "charType": 1, "scores": {}},
        ],
    }
}

# Тариф promax дополнительно говорит, какой тон он услышал. Ключ его пока не
# открывает, но разбор обязан быть готов: включение — смена одной переменной.
ANSWER_PROMAX = {
    "result": {
        "overall": 70,
        "words": [
            {
                "word": "好",
                "charType": 0,
                "tone": "tone3",
                "tone_sound_like": "tone2",
                "symbolpinyin": "hǎo",
                "scores": {"overall": 48, "tone": 12},
            }
        ],
    }
}


class FakeSession:
    def __init__(self, rows: dict[int, Dialog]) -> None:
        self._rows = rows
        self.added: list[object] = []

    async def get(self, _model, key):
        return self._rows.get(key)

    def add(self, obj) -> None:
        obj.id = len(self.added) + 1
        self.added.append(obj)

    async def flush(self) -> None:
        pass


class FakeRedis:
    """Подмена redis: хранит строки и помнит, с каким TTL их клали."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttl[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


def _user() -> User:
    user = User()
    user.id = uuid.uuid4()
    user.hsk_level = "hsk12"
    return user


def _reply(user: User, dialog_id: int = 1, **kwargs) -> Dialog:
    row = Dialog(user_id=user.id, role="assistant", text_zh="你好！", **kwargs)
    row.id = dialog_id
    return row


# --- разбор ответа сервиса ---------------------------------------------------


def test_ответ_сервиса_разбирается_по_иероглифам():
    result = _parse(ANSWER)

    assert result.overall == 78
    assert result.tone == 64
    # Пунктуация выброшена: оценивать восклицательный знак нечего.
    assert [c.char for c in result.chars] == ["你", "好"]
    assert result.chars[1].tone_expected == 3
    # Услышанного тона в этом тарифе нет, и сбитый тон виден по его баллу.
    assert result.chars[0].tone_ok is True
    assert result.chars[1].tone_ok is False


def test_услышанный_тон_разбирается_если_тариф_его_даёт():
    char = _parse(ANSWER_PROMAX).chars[0]
    assert char.tone_expected == 3
    assert char.tone_actual == 2
    assert char.tone_ok is False


def test_полный_ответ_сохраняется_целиком():
    """Критерий этапа: в detail ложится весь JSON, а не выжимка."""
    assert _parse(ANSWER).raw == ANSWER


def test_дробные_баллы_округляются():
    # Сервис возвращает и float, и строки — в колонку Integer это не положить.
    data = {"result": {"overall": 78.6, "words": [{"word": "你", "scores": {"overall": "91.2"}}]}}
    result = _parse(data)
    assert result.overall == 79
    assert result.chars[0].overall == 91


def test_разбор_без_слов_не_падает():
    assert _parse({"result": {"overall": 0}}).chars == []


def test_подпись_считается_как_требует_сервис():
    # sha1(appKey + timestamp + secretKey) — формат из официального примера.
    assert _sha1("app" + "100" + "secret") == _sha1("app100secret")
    assert len(_sha1("app100secret")) == 40


def test_запрос_собирается_под_wav_16к():
    provider = SpeechSuperPronunciation(
        Settings(speechsuper_app_key="app", speechsuper_secret_key="secret", **BASE)  # type: ignore[arg-type]
    )
    params = provider._params("你好", "user-1")
    audio = params["start"]["param"]["audio"]

    # Соврать здесь нельзя: сервис разбирает файл по этим числам, а ffmpeg
    # отдаёт ровно 16 кГц моно 16 бит.
    assert audio == {"audioType": "wav", "channel": 1, "sampleBytes": 2, "sampleRate": 16000}
    assert params["start"]["param"]["request"]["coreType"] == "sent.eval.cn"
    assert params["start"]["param"]["request"]["refText"] == "你好"
    # Подпись старта включает userId, подпись соединения — нет.
    assert params["connect"]["param"]["app"]["sig"] != params["start"]["param"]["app"]["sig"]


def test_провайдер_без_ключей_не_создаётся():
    with pytest.raises(ProviderError):
        SpeechSuperPronunciation(Settings(speechsuper_app_key=None, **BASE))  # type: ignore[arg-type]


class FakeResponse:
    def __init__(self, payload, status_code=200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload, status_code=200) -> None:
        self._response = FakeResponse(payload, status_code)

    async def post(self, *_args, **_kwargs):
        return self._response


def _provider() -> SpeechSuperPronunciation:
    return SpeechSuperPronunciation(
        Settings(speechsuper_app_key="app", speechsuper_secret_key="secret", **BASE)  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_тишина_просит_перезаписать_а_не_показывает_ноль(monkeypatch):
    """Критерий этапа: пустая запись даёт понятное сообщение, а не «0 из 100».

    Тишину и шум сервис не считает ошибкой — он честно ставит нули по всей
    фразе. Снято с живого вызова: overall 0 и нули на каждом иероглифе.
    """
    пусто = {
        "result": {
            "overall": 0,
            "tone": 0,
            "words": [{"word": "你", "tone": "tone3", "scores": {"overall": 0, "tone": 0}}],
        }
    }
    monkeypatch.setattr(speechsuper, "get_client", lambda: FakeClient(пусто))
    with pytest.raises(SpeechUnclear):
        await _provider().assess(b"wav", "你好", "user-1")


@pytest.mark.asyncio
async def test_отказ_сервиса_не_выдаётся_за_плохую_запись(monkeypatch):
    """errId — это наша авария (квота, тариф, подпись), а не шум в микрофоне."""
    отказ = {"errId": 41030, "error": "invalid coreType", "eof": 1}
    monkeypatch.setattr(speechsuper, "get_client", lambda: FakeClient(отказ))
    with pytest.raises(ProviderError) as exc:
        await _provider().assess(b"wav", "你好", "user-1")
    # Тело ответа обязано попасть в исключение: без него отладка слепая.
    assert "invalid coreType" in str(exc.value)
    assert "41030" in (exc.value.body or "")


@pytest.mark.asyncio
async def test_нормальный_ответ_доходит_целиком(monkeypatch):
    monkeypatch.setattr(speechsuper, "get_client", lambda: FakeClient(ANSWER))
    result = await _provider().assess(b"wav", "你好！", "user-1")
    assert result.overall == 78
    assert len(result.chars) == 2


# --- выбор эталона -----------------------------------------------------------


@pytest.mark.asyncio
async def test_эталон_по_умолчанию_это_фраза_бота():
    user = _user()
    row = _reply(user, pinyin="nǐ hǎo", audio_file_id="AgAD-file")
    target = await pron.choose_target(FakeSession({1: row}), user, 1)

    assert target.ref_text == "你好！"
    assert target.from_correction is False
    # Голос реплики уже в Telegram: переозвучивать его — платить второй раз.
    assert target.audio_file_id == "AgAD-file"


@pytest.mark.asyncio
async def test_исправленная_фраза_перебивает_реплику_бота():
    """Критерий этапа: при ошибке тренируем то, в чём юзер ошибся."""
    user = _user()
    row = _reply(user, corrected_zh="我有三本书", audio_file_id="AgAD-file")
    target = await pron.choose_target(FakeSession({1: row}), user, 1)

    assert target.ref_text == "我有三本书"
    assert target.from_correction is True
    # Эту фразу вслух ещё никто не говорил — file_id от чужого текста подсунуть
    # нельзя, иначе юзер услышит одно, а тренировать будет другое.
    assert target.audio_file_id is None


@pytest.mark.asyncio
async def test_чужая_реплика_не_отдаётся():
    user = _user()
    чужая = _reply(_user())
    with pytest.raises(ReplyNotFound):
        await pron.choose_target(FakeSession({1: чужая}), user, 1)


@pytest.mark.asyncio
async def test_реплика_без_текста_не_становится_эталоном():
    user = _user()
    row = _reply(user)
    row.text_zh = ""
    with pytest.raises(ReplyNotFound):
        await pron.choose_target(FakeSession({1: row}), user, 1)


# --- режим ожидания записи ---------------------------------------------------


@pytest.mark.asyncio
async def test_режим_тренировки_переживает_перезапуск():
    """Бот и воркер — разные контейнеры, состояние обязано жить в redis."""
    user, redis = _user(), FakeRedis()
    target = pron.PracticeTarget(
        dialog_id=7, ref_text="你好", pinyin="nǐ hǎo", translation=None, from_correction=True
    )
    await pron.start_practice(redis, user, target)

    ключ = pron.practice_key(user)
    assert ключ.startswith("china:")  # redis общий с чужими проектами
    assert redis.ttl[ключ] == pron.PRACTICE_TTL_SEC

    восстановлен = await pron.load_practice(redis, user)
    assert восстановлен.ref_text == "你好"
    assert восстановлен.dialog_id == 7
    assert восстановлен.from_correction is True

    await pron.stop_practice(redis, user)
    assert await pron.load_practice(redis, user) is None


@pytest.mark.asyncio
async def test_битое_состояние_не_роняет_разговор():
    user, redis = _user(), FakeRedis()
    redis.store[pron.practice_key(user)] = "{не json"
    assert await pron.load_practice(redis, user) is None


@pytest.mark.asyncio
async def test_голос_эталона_запоминается_для_повторного_прослушивания():
    user, redis = _user(), FakeRedis()
    await pron.start_practice(
        redis,
        user,
        pron.PracticeTarget(
            dialog_id=7, ref_text="你好", pinyin="nǐ hǎo", translation=None, from_correction=True
        ),
    )
    await pron.remember_reference_audio(redis, user, "AgAD-synth")

    сохранено = json.loads(redis.store[pron.practice_key(user)])
    assert сохранено["audio_file_id"] == "AgAD-synth"


# --- сборка результата -------------------------------------------------------


def _result(chars: list[pron.CharResult], overall: int = 78) -> pron.AssessResult:
    return pron.AssessResult(
        overall=overall,
        tone=64,
        pronunciation=85,
        fluency=90,
        ref_text="你好",
        chars=chars,
        check_id=1,
    )


def test_пиньинь_берётся_у_сервиса():
    merged = pron._merge(_parse(ANSWER), "你好！")
    assert [c.char for c in merged] == ["你", "好"]
    assert [c.pinyin for c in merged] == ["nǐ", "hǎo"]


def test_пиньинь_считается_локально_если_сервис_его_не_прислал():
    result = Pronunciation(chars=[CharScore(char="你"), CharScore(char="好")])
    # Тон третьего перед третьим считается по фразе целиком, а не по знаку.
    assert [c.pinyin for c in pron._merge(result, "你好")] == ["nǐ", "hǎo"]


def test_сбитый_тон_виден_в_тексте_ответа():
    """Критерий этапа: видно, ГДЕ ошибка, а не только общий балл."""
    text = render_result(_result(pron._merge(_parse(ANSWER), "你好！")))

    assert "🎯 <b>Произношение: 78</b>" in text
    assert "🟢 你 nǐ — тон 3-й ✓, звуки 92" in text
    assert "🔴 好 hǎo — тон 3-й сбит (12), звуки 48" in text
    assert "Сбитых тонов: 1" in text


def test_услышанный_тон_показывается_если_он_известен():
    merged = pron._merge(_parse(ANSWER_PROMAX), "好")
    assert "нужен тон 3-й, а прозвучал 2-й" in render_result(_result(merged))


def test_ровные_тоны_не_пугают_юзера():
    chars = [
        pron.CharResult(
            char="你", pinyin="nǐ", score=92, tone_expected=3, tone_actual=3, tone_ok=True
        )
    ]
    assert "Тоны все на месте" in render_result(_result(chars))


def test_нейтральный_тон_называется_словом():
    # Цифра «5» ученику ничего не говорит, а нейтральный тон встречается часто.
    chars = [
        pron.CharResult(
            char="吗", pinyin="ma", score=88, tone_expected=5, tone_actual=5, tone_ok=True
        )
    ]
    assert "тон нейтральный" in render_result(_result(chars))


def test_пропущенные_баллы_не_ломают_отрисовку():
    chars = [
        pron.CharResult(char="你", pinyin="", score=None, tone_expected=None, tone_actual=None)
    ]
    text = render_result(_result(chars, overall=None))
    assert "Произношение: —" in text


def test_эталон_экранируется():
    # Разметка у бота HTML: угловая скобка в тексте роняет отправку целиком.
    target = pron.PracticeTarget(
        dialog_id=1, ref_text="<b>你好", pinyin="nǐ hǎo", translation=None, from_correction=False
    )
    assert "&lt;b&gt;你好" in render_practice(target)


def test_подпись_эталона_объясняет_откуда_фраза():
    target = pron.PracticeTarget(
        dialog_id=1, ref_text="我有三本书", pinyin="wǒ", translation=None, from_correction=True
    )
    assert "без ошибки" in render_practice(target)


def test_рассинхрон_пиньиня_не_подписывает_чужие_слоги():
    # Лучше без пиньиня, чем с чужим: подписать 好 слогом от 你 — прямая ложь.
    result = Pronunciation(chars=[CharScore(char="x"), CharScore(char="好")])
    merged = pron._merge(result, "x好")
    assert [c.pinyin for c in merged] == ["", ""]


# --- кнопки ------------------------------------------------------------------


def test_кнопка_оценки_живёт_отдельным_рядом():
    markup = answer_keyboard(42)
    assert len(markup.inline_keyboard) == 2
    assert parse_action(markup.inline_keyboard[1][0].callback_data) == (ACTION_PRON, 42)


def test_нажатая_оценка_исчезает_а_остальные_остаются():
    markup = answer_keyboard(42, with_pron=False)
    данные = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert all(f":{ACTION_PRON}:" not in d for d in данные)
    assert len(данные) == 2


def test_кнопки_тренировки_и_результата_разбираются():
    слушать = practice_keyboard(42).inline_keyboard[0][0].callback_data
    ещё = result_keyboard(42).inline_keyboard[0][1].callback_data
    assert parse_action(слушать) == (ACTION_LISTEN, 42)
    assert parse_action(ещё) == (ACTION_AGAIN, 42)
