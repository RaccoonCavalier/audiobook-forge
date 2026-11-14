"""TTS 抽象：屏蔽各家协议的差异，上层只管"给我一段文字，还我一段音频"。"""

from __future__ import annotations

import abc
from dataclasses import dataclass


class TTSError(RuntimeError):
    """可重试的合成失败。"""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class TTSRequest:
    text: str
    voice: str
    style: str | None = None
    # 语速倍率，1.0 为正常；provider 支持时映射到自身参数
    speed: float = 1.0


@dataclass
class TTSResult:
    audio: bytes
    format: str = "mp3"
    sample_rate: int = 24000


class BaseTTS(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def synthesize(self, request: TTSRequest) -> TTSResult:
        ...

    async def aclose(self) -> None:
        return None

    @property
    def available_voices(self) -> list[str]:
        return []

    def health(self) -> tuple[bool, str]:
        return True, "ok"
