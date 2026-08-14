"""Оценка произношения через SpeechSuper.

Единственный сервис в цепочке, который слышит звук. Всё остальное (диалог,
перевод, поправки) работает с текстом и о качестве тонов не знает ничего.

Формат запроса и подписей взят из официального примера сервиса: два блока,
`connect` и `start`, склеенные в поле формы `text`, звук — файлом в той же
multipart-форме.
"""

from __future__ import annotations

import hashlib
import json
import time

from app.config import Settings
from app.core.providers.base import (
    CharScore,
    Pronunciation,
    PronunciationProvider,
    ProviderError,
    SpeechUnclear,
    call_logged,
)
from app.core.providers.http import get_client
from app.logging import get_logger

log = get_logger("providers")

BASE_URL = "https://api.speechsuper.com/"

# Вес тона в общем балле. Треть — значение из демо самого сервиса: тона важны,
# но балл не должен схлопываться в оценку одних только тонов.
TONE_WEIGHT = 0.33

# Знаки препинания сервис возвращает наравне с иероглифами и помечает
# charType == 1. Оценивать их нечего.
CHAR_TYPE_PUNCTUATION = 1

# Полнота (integrity) — сколько от эталона реально прозвучало.
#
# Отсекать по ней запись нельзя: на живой проверке полноту 20 получали и
# молчание, и настоящая речь человека — по цифрам они неразличимы. Порог,
# поставленный по молчанию, отбил вообще все живые попытки. Поэтому здесь
# остаётся только честный ноль, а низкая полнота идёт юзеру подсказкой
# «проговорите фразу целиком» вместе с разбором.
LOW_INTEGRITY = 60


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()  # noqa: S324  так требует сервис


def _int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _tone_or_none(value: object) -> int | None:
    """Тон приходит строкой «tone3», а иногда числом. Нужен номер."""
    if isinstance(value, str):
        digits = value.strip().removeprefix("tone").strip()
        return _int_or_none(digits)
    return _int_or_none(value)


class SpeechSuperPronunciation(PronunciationProvider):
    name = "speechsuper"

    def __init__(self, settings: Settings) -> None:
        if not settings.speechsuper_app_key or not settings.speechsuper_secret_key:
            raise ProviderError(
                self.name, "init", "не заданы SPEECHSUPER_APP_KEY и SPEECHSUPER_SECRET_KEY"
            )
        self._app_key = settings.speechsuper_app_key
        self._secret_key = settings.speechsuper_secret_key
        self._core_type = settings.speechsuper_core_type
        self._timeout = settings.provider_timeout

    def _params(self, ref_text: str, user_id: str) -> dict[str, object]:
        timestamp = str(int(time.time()))
        return {
            "connect": {
                "cmd": "connect",
                "param": {
                    "sdk": {"version": 16777472, "source": 9, "protocol": 2},
                    "app": {
                        "applicationId": self._app_key,
                        "sig": _sha1(self._app_key + timestamp + self._secret_key),
                        "timestamp": timestamp,
                    },
                },
            },
            "start": {
                "cmd": "start",
                "param": {
                    "app": {
                        "userId": user_id,
                        "applicationId": self._app_key,
                        "timestamp": timestamp,
                        "sig": _sha1(self._app_key + timestamp + user_id + self._secret_key),
                    },
                    # Строго то, что отдаёт наш ffmpeg: WAV 16 кГц моно 16 бит.
                    # Соврать здесь нельзя — сервис разбирает файл по этим числам.
                    "audio": {
                        "audioType": "wav",
                        "channel": 1,
                        "sampleBytes": 2,
                        "sampleRate": 16000,
                    },
                    "request": {
                        "coreType": self._core_type,
                        "refText": ref_text,
                        "tokenId": "tokenId",
                        "tone_weight": TONE_WEIGHT,
                    },
                },
            },
        }

    async def assess(self, audio_wav: bytes, ref_text: str, user_id: str) -> Pronunciation:
        async with call_logged(
            self.name, "pronunciation", эталон=ref_text, байт=len(audio_wav)
        ) as details:
            response = await get_client().post(
                BASE_URL + self._core_type,
                data={"text": json.dumps(self._params(ref_text, user_id))},
                files={"audio": ("speech.wav", audio_wav, "audio/wav")},
                headers={"Request-Index": "0"},
                timeout=self._timeout,
            )
            details["http_код"] = response.status_code
            if response.status_code >= 400:
                raise ProviderError(
                    self.name,
                    "pronunciation",
                    "сервис оценки вернул ошибку",
                    status_code=response.status_code,
                    body=response.text[:1000],
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError(
                    self.name,
                    "pronunciation",
                    "сервис оценки вернул не JSON",
                    status_code=response.status_code,
                    body=response.text[:1000],
                ) from exc

            # Отказ приходит с кодом 200 и полем errId: неверный coreType,
            # исчерпанная квота, просроченная подпись. Это авария на нашей
            # стороне, а не плохая запись, и юзеру нужен другой текст.
            if not isinstance(data, dict) or "result" not in data:
                raise ProviderError(
                    self.name,
                    "pronunciation",
                    f"сервис оценки отказал: {_error_text(data)}",
                    status_code=response.status_code,
                    body=json.dumps(data, ensure_ascii=False)[:1000],
                )

            result = _parse(data)
            details["балл"] = result.overall
            details["иероглифов"] = len(result.chars)

        # Тишина и шум приходят не ошибкой, а околонулевыми баллами: сервис
        # честно оценил то, чего нет. Показывать юзеру «9 из 100» и пять
        # красных строк — хуже, чем попросить перезаписать.
        if not result.chars or _no_speech(result):
            # Разбор пишем в лог целиком: без него «не расслышал» неотличимо от
            # «сервис слышит не то», а запись юзера уже не переспросить.
            log.info(
                "разбор признан пустым",
                балл=result.overall,
                полнота=result.integrity,
                беглость=result.fluency,
                чистота=result.pronunciation,
                тоны=result.tone,
                по_иероглифам=[f"{c.char}:{c.overall}/{c.tone}" for c in result.chars],
            )
            raise SpeechUnclear(
                f"сервис не услышал речь: балл {result.overall}, полнота {result.integrity}"
            )
        return result


def _error_text(data: object) -> str:
    if isinstance(data, dict):
        return str(data.get("error") or data.get("errId") or data)[:300]
    return str(data)[:300]


def _no_speech(result: Pronunciation) -> bool:
    """Речи в записи нет вовсе: сервис проставил нули по всей фразе."""
    return (result.overall or 0) == 0 and all((c.overall or 0) == 0 for c in result.chars)


def _parse(data: dict[str, object]) -> Pronunciation:
    result = data.get("result")
    if not isinstance(result, dict):
        return Pronunciation(raw=data)

    chars: list[CharScore] = []
    words = result.get("words")
    for item in words if isinstance(words, list) else []:
        if not isinstance(item, dict):
            continue
        if _int_or_none(item.get("charType")) == CHAR_TYPE_PUNCTUATION:
            continue
        scores = item.get("scores")
        scores = scores if isinstance(scores, dict) else {}
        chars.append(
            CharScore(
                char=str(item.get("word") or ""),
                overall=_int_or_none(scores.get("overall")),
                tone=_int_or_none(scores.get("tone")),
                # Пиньинь от сервиса точнее локального: он считает сандхи по
                # той самой фразе, которую разбирал.
                pinyin=str(item.get("symbolpinyin") or "").strip() or None,
                tone_expected=_tone_or_none(item.get("tone")),
                # Услышанный тон есть только в тарифе promax.
                tone_actual=_tone_or_none(item.get("tone_sound_like")),
            )
        )

    return Pronunciation(
        overall=_int_or_none(result.get("overall")),
        pronunciation=_int_or_none(result.get("pronunciation")),
        tone=_int_or_none(result.get("tone")),
        fluency=_int_or_none(result.get("fluency")),
        integrity=_int_or_none(result.get("integrity")),
        chars=chars,
        raw=data,
    )
