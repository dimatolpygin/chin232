"""Диалог, грамматика и перевод через OpenRouter. Работает с текстом, звук не слышит."""

from __future__ import annotations

import json
import re

from app.config import Settings
from app.core.providers.base import LLMProvider, LlmReply, ProviderError, call_logged
from app.core.providers.http import get_client

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Модели любят обернуть JSON в ```json ... ``` вопреки инструкции.
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class OpenRouterLLM(LLMProvider):
    name = "openrouter"

    def __init__(self, settings: Settings) -> None:
        if not settings.openrouter_api_key:
            raise ProviderError(self.name, "init", "не задан OPENROUTER_API_KEY")
        self._key = settings.openrouter_api_key
        self._model = settings.openrouter_model
        self._timeout = settings.provider_timeout

    async def reply(self, system_prompt: str, history: list[dict[str, str]]) -> LlmReply:
        messages = [{"role": "system", "content": system_prompt}, *history]
        async with call_logged(
            self.name, "chat", модель=self._model, реплик_в_истории=len(history)
        ) as details:
            client = get_client()
            response = await client.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 260,
                    "response_format": {"type": "json_object"},
                },
            )
            details["http_код"] = response.status_code
            details["объём_ответа_байт"] = len(response.content)
            if response.status_code >= 400:
                raise ProviderError(
                    self.name,
                    "chat",
                    "модель вернула ошибку",
                    status_code=response.status_code,
                    body=response.text,
                )
            payload = response.json()
            usage = payload.get("usage") or {}
            details["токенов_вход"] = usage.get("prompt_tokens")
            details["токенов_выход"] = usage.get("completion_tokens")

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(
                self.name, "chat", "в ответе нет содержимого", body=json.dumps(payload)[:1000]
            ) from exc

        return self._parse(content)

    def _parse(self, content: str) -> LlmReply:
        cleaned = FENCE.sub("", content.strip())
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # Модель не выдержала формат. Терять ответ из-за этого нельзя:
            # иероглифы у нас есть, значит круг можно докрутить.
            return LlmReply(reply_zh=cleaned)
        return LlmReply(
            reply_zh=(data.get("reply_zh") or "").strip(),
            pinyin=(data.get("pinyin") or None),
            translation=(data.get("translation") or None),
            correction=(data.get("correction") or None),
            heard=(data.get("heard") or None),
        )
