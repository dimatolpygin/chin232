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


def test_подсказка_распознавания_двуязычная():
    """Регрессия: подсказка на одном русском уводит 你好 в «Ни хао».

    Проверено замером на живой записи — с русской подсказкой сервис возвращал
    кириллическую транслитерацию и язык russian.
    """
    from app.core.providers.stt.openai_whisper import PROMPT_HINT

    есть_иероглифы = any("一" <= ch <= "鿿" for ch in PROMPT_HINT)
    есть_кириллица = any("а" <= ch.lower() <= "я" for ch in PROMPT_HINT)
    assert есть_иероглифы, "в подсказке нет иероглифов — китайский уедет в кириллицу"
    assert есть_кириллица, "в подсказке нет кириллицы — русская речь будет распознаваться хуже"
