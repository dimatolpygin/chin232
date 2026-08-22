"""Озвучка через Fish Audio. Ответ бинарный, не JSON."""

from __future__ import annotations

from app.config import Settings
from app.core.providers.base import ProviderError, Speech, TTSProvider, Voice, call_logged
from app.core.providers.http import get_client

API_URL = "https://api.fish.audio/v1/tts"

# Каталог у сервиса открытый, и большая часть верхушки — клоны блогеров,
# ведущих и певцов. Такие голоса в списке недопустимы: бот говорит от себя, а
# не от чужого имени. Отобраны нейтральные дикторские, каждый проверен живым
# синтезом фразы «你好，很高兴认识你» — слишком торопливые и слишком тягучие
# отброшены по длительности.
VOICES = (
    Voice("23632847285a487d8e0c6ae5bc593c71", "Женский мягкий", "спокойный, по умолчанию"),
    Voice("32153ca8aff04850bf01a6fcd861c48c", "Женский тёплый", "неспешный, хорош для начала"),
    Voice("bf6c479f5a384b8d857310030035824b", "Женский живой", "быстрее и энергичнее"),
    Voice("2926cb350f1a426d800bf8c360c3cb94", "Мужской дикторский", "чёткая новостная речь"),
    Voice("0a36e464a5b54026b98d258495c5a1e2", "Мужской спокойный", "ровный, как объяснение"),
    Voice("639caf769601415082f9b67ec6f39f4f", "Мужской молодой", "лёгкий разговорный тон"),
)


class FishTTS(TTSProvider):
    name = "fish"
    VOICES = VOICES

    def __init__(self, settings: Settings) -> None:
        if not settings.fish_api_key:
            raise ProviderError(self.name, "init", "не задан FISH_API_KEY")
        self._key = settings.fish_api_key
        self._model = settings.fish_model
        self._default_voice = settings.fish_voice_id
        self._timeout = settings.provider_timeout

    async def synthesize(self, text: str, voice_id: str | None, speed: float) -> Speech:
        body: dict[str, object] = {
            "text": text,
            "format": "mp3",
            # Темп задаётся через prosody.speed — проверено, работает.
            "prosody": {"speed": speed},
        }
        voice = voice_id or self._default_voice
        if voice:
            body["reference_id"] = voice

        async with call_logged(
            self.name, "tts", знаков=len(text), голос=voice or "по умолчанию", темп=speed
        ) as details:
            client = get_client()
            response = await client.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                    "model": self._model,
                },
                json=body,
            )
            details["http_код"] = response.status_code
            details["объём_ответа_байт"] = len(response.content)
            if response.status_code >= 400:
                raise ProviderError(
                    self.name,
                    "tts",
                    "сервис озвучки вернул ошибку",
                    status_code=response.status_code,
                    # Ответ бинарный, но при ошибке приходит текст.
                    body=response.text[:1000],
                )
            if not response.content:
                raise ProviderError(
                    self.name,
                    "tts",
                    "сервис озвучки вернул пустой ответ",
                    status_code=response.status_code,
                )
            audio = response.content

        return Speech(audio=audio, fmt="mp3")
