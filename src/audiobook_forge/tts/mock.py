"""Mock TTS：本地生成一段带音调的正弦波，用来在没有 key 的情况下跑通全链路。

时长按中文 5.2 字/秒估算，不同音色用不同基频，方便听出"换人说话了"。
"""

from __future__ import annotations

import io
import math
import wave
from array import array

from .base import BaseTTS, TTSRequest, TTSResult

_CHARS_PER_SECOND = 5.2
_SAMPLE_RATE = 24000

_FREQ = {
    "mock-narrator": 180,
    "mock-female-a": 260,
    "mock-female-b": 300,
    "mock-male-a": 120,
    "mock-male-b": 145,
    "mock-child": 380,
}


class MockTTS(BaseTTS):
    name = "mock"

    @property
    def available_voices(self) -> list[str]:
        return list(_FREQ)

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        text = request.text.strip()
        if not text:
            return TTSResult(audio=_wav_bytes(0.2, 180), format="wav", sample_rate=_SAMPLE_RATE)

        freq = _FREQ.get(request.voice, 200)
        seconds = max(0.4, len(text) / _CHARS_PER_SECOND)
        if request.speed and request.speed > 0:
            seconds /= request.speed
        return TTSResult(
            audio=_wav_bytes(seconds, freq),
            format="wav",
            sample_rate=_SAMPLE_RATE,
        )

    def health(self) -> tuple[bool, str]:
        return True, "mock provider 始终可用"


def _wav_bytes(seconds: float, freq: int) -> bytes:
    """生成一个近似方波的正弦音，用 array 重复避免逐样本 Python 循环。"""
    total = max(1, int(seconds * _SAMPLE_RATE))
    period = max(2, int(_SAMPLE_RATE / max(1, freq)))
    one_cycle = array(
        "h",
        (int(2600 * math.sin(2 * math.pi * i / period)) for i in range(period)),
    )
    samples = (one_cycle * (total // period + 1))[:total]

    # 首尾各 15ms 淡入淡出，避免拼接处爆音
    fade = min(len(samples) // 2, int(0.015 * _SAMPLE_RATE))
    for i in range(fade):
        factor = i / fade
        samples[i] = int(samples[i] * factor)
        samples[-1 - i] = int(samples[-1 - i] * factor)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_SAMPLE_RATE)
        wav.writeframes(samples.tobytes())
    return buffer.getvalue()
