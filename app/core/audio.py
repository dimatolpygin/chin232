"""Конвертация звука через ffmpeg.

Телеграм показывает голосовое сообщение с волной только для ogg/opus. Если
прислать mp3, он покажет файл — а это уже не голосовое, и критерий этапа не
выполнен.
"""

from __future__ import annotations

import asyncio

from app.logging import get_logger

log = get_logger("audio")


class AudioError(RuntimeError):
    pass


async def _run_ffmpeg(args: list[str], data: bytes, операция: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(input=data)
    if process.returncode != 0 or not stdout:
        tail = stderr.decode("utf-8", "replace")[-800:]
        log.error("ffmpeg не справился", операция=операция, код=process.returncode, вывод=tail)
        raise AudioError(f"ffmpeg вернул код {process.returncode}: {tail}")
    log.debug("ffmpeg отработал", операция=операция, вход_байт=len(data), выход_байт=len(stdout))
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
