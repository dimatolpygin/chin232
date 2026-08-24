"""Конвертация звука через ffmpeg.

Телеграм показывает голосовое сообщение с волной только для ogg/opus. Если
прислать mp3, он покажет файл — а это уже не голосовое, и критерий этапа не
выполнен.
"""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar

from app.logging import get_logger

log = get_logger("audio")


class AudioError(RuntimeError):
    pass


# Сколько миллисекунд этот круг просидел в ffmpeg. Контекстная переменная, а не
# счётчик на модуль: у каждой задачи воркера свой контекст, и десять кругов
# разом не сложат своё время в одну кучу.
#
# Считается это не ради красивой цифры. ffmpeg — единственное место круга, где
# работает наш процессор, а не чужой сервер; на машине с одним ядром именно он
# упирается в потолок первым. Пока его доля в круге мала, добавлять ядра рано.
_ffmpeg_ms: ContextVar[float] = ContextVar("ffmpeg_ms", default=0.0)


def reset_ffmpeg_time() -> None:
    """Обнулить счётчик перед кругом."""
    _ffmpeg_ms.set(0.0)


def ffmpeg_time_ms() -> int:
    """Сколько миллисекунд круг потратил на конвертацию звука."""
    return round(_ffmpeg_ms.get())


async def _run_ffmpeg(args: list[str], data: bytes, операция: str) -> bytes:
    начало = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(input=data)
    мс = (time.monotonic() - начало) * 1000
    # Время засчитываем и неудачному прогону: процессор он занимал так же.
    _ffmpeg_ms.set(_ffmpeg_ms.get() + мс)

    if process.returncode != 0 or not stdout:
        tail = stderr.decode("utf-8", "replace")[-800:]
        log.error("ffmpeg не справился", операция=операция, код=process.returncode, вывод=tail)
        raise AudioError(f"ffmpeg вернул код {process.returncode}: {tail}")
    log.info(
        "ffmpeg отработал",
        операция=операция,
        вход_байт=len(data),
        выход_байт=len(stdout),
        длительность_мс=round(мс),
    )
    return stdout


async def to_voice_ogg(data: bytes, source_format: str = "mp3") -> bytes:
    """В ogg/opus для отправки голосовым сообщением."""
    return await _run_ffmpeg(
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            source_format,
            "-i",
            "pipe:0",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-f",
            "ogg",
            "pipe:1",
        ],
        data,
        "в ogg/opus",
    )


async def to_wav16k(data: bytes) -> bytes:
    """В WAV 16 кГц моно 16 бит — этого строго требует SpeechSuper (этап 3)."""
    return await _run_ffmpeg(
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-sample_fmt",
            "s16",
            "-f",
            "wav",
            "pipe:1",
        ],
        data,
        "в wav 16 кГц",
    )
