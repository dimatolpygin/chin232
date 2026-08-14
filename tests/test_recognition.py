"""Отсев выдуманного распознавания."""

from __future__ import annotations

import pytest

from app.core.providers.base import Transcript
from app.core.services.recognition import NotRecognized, ensure_recognized


def test_нормальная_китайская_речь_проходит():
    t = Transcript(text="我今天想去中国餐馆吃饭", language="chinese", no_speech_prob=0.02)
    assert ensure_recognized(t) == "我今天想去中国餐馆吃饭"


def test_русская_речь_проходит():
    t = Transcript(text="Привет, как дела?", language="russian", no_speech_prob=0.1)
    assert ensure_recognized(t) == "Привет, как дела?"


def test_японская_заготовка_отсекается():
    # Ровно то, что сервис выдал на живой проверке: 2.4 секунды тишины.
    t = Transcript(text="ご清聴ありがとうございました。", language="japanese")
    with pytest.raises(NotRecognized):
        ensure_recognized(t)


def test_посторонний_язык_отсекается():
    t = Transcript(text="Danke schön", language="german")
    with pytest.raises(NotRecognized):
        ensure_recognized(t)


def test_тишина_отсекается_по_вероятности():
    t = Transcript(text="嗯", language="chinese", no_speech_prob=0.95)
    with pytest.raises(NotRecognized):
        ensure_recognized(t)


def test_thanks_for_watching_отсекается():
    t = Transcript(text="Thanks for watching!", language="english", no_speech_prob=0.1)
    with pytest.raises(NotRecognized):
        ensure_recognized(t)


def test_пустой_текст_отсекается():
    with pytest.raises(NotRecognized):
        ensure_recognized(Transcript(text="   ", language="chinese"))


def test_одни_знаки_препинания_отсекаются():
    with pytest.raises(NotRecognized):
        ensure_recognized(Transcript(text="... !", language="chinese"))


def test_подсказка_уходит_в_запрос_и_она_двуязычная():
    """Регрессия: подсказка на одном русском уводит 你好 в «Ни хао».

    Проверяем тело запроса, а не константу рядом: в прошлый раз константу
    поправили, а литерал внутри вызова остался прежним, и зелёный тест
    прикрывал мёртвый фикс.
    """
    from app.config import Settings
    from app.core.providers.stt.openai_whisper import OpenAIWhisperSTT

    stt = OpenAIWhisperSTT(
        Settings(
            openai_api_key="sk-test",
            bot_token="123:AAtest",
            database_url="postgresql+asyncpg://u:p@localhost/db",
            redis_url="redis://localhost:6379/5",
        )  # type: ignore[call-arg]
    )
    подсказка = stt._request_data(None)["prompt"]
    assert any(
        "一" <= ch <= "鿿" for ch in подсказка
    ), "нет иероглифов — китайский уедет в кириллицу"
    assert any(
        "а" <= ch.lower() <= "я" for ch in подсказка
    ), "нет кириллицы — русский распознается хуже"


def test_принудительный_язык_попадает_в_запрос():
    from app.config import Settings
    from app.core.providers.stt.openai_whisper import OpenAIWhisperSTT

    stt = OpenAIWhisperSTT(
        Settings(
            openai_api_key="sk-test",
            bot_token="123:AAtest",
            database_url="postgresql+asyncpg://u:p@localhost/db",
            redis_url="redis://localhost:6379/5",
        )  # type: ignore[call-arg]
    )
    assert "language" not in stt._request_data(None)
    assert stt._request_data("zh")["language"] == "zh"


def test_метка_варианта_срезается():
    from app.core.services.recognition import strip_variant_prefix

    assert strip_variant_prefix("вариант 2: 你好") == "你好"
    assert strip_variant_prefix("Вариант 1： Привет") == "Привет"
    assert strip_variant_prefix("你好") == "你好"


def test_иероглифы_опознаются():
    from app.core.services.recognition import has_han

    assert has_han("你好")
    assert not has_han("Мил!")
