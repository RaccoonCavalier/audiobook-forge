"""OpenAI 兼容的语音合成（POST {base_url}/audio/speech）。

这一层同时覆盖"离线/本地自建 TTS"：openedai-speech、Kokoro-FastAPI、
LocalAI、GPT-SoVITS-OpenAI-API 等只要实现了 /v1/audio/speech 就能接。
"""

from __future__ import annotations

import httpx

from .. import __version__
from .base import BaseTTS, TTSError, TTSRequest, TTSResult

# 常见本地服务内置的音色名，作为界面下拉的兜底
COMMON_VOICES = [
    "alloy",
    "echo",
    "fable",
    "onyx",
    "nova",
    "shimmer",
    "af_sky",
    "af_bella",
    "am_michael",
    "bf_emma",
    "bm_george",
]


class OpenAITTS(BaseTTS):
    name = "openai"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "tts-1",
        voice: str = "alloy",
        *,
        audio_format: str = "mp3",
        speed: float = 1.0,
        timeout: float = 120.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.audio_format = audio_format
        self.speed = speed
        self.timeout = timeout
        self.max_retries = max_retries
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "Content-Type": "application/json",
                "User-Agent": f"audiobook-forge/{__version__}",
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                transport=self._transport,
                timeout=httpx.Timeout(self.timeout, connect=15.0),
                headers=headers,
            )
        return self._client

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        if not self.base_url:
            raise TTSError("未配置 OpenAI 兼容 TTS 的 base_url", retryable=False)

        payload = {
            "model": self.model,
            "input": request.text,
            "voice": request.voice or self.voice,
            "response_format": self.audio_format,
            "speed": round(self.speed * (request.speed or 1.0), 3),
        }

        last_error = "未知错误"
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.client.post("/audio/speech", json=payload)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await self._backoff(attempt)
                continue

            if resp.status_code < 400:
                if not resp.content:
                    raise TTSError("TTS 返回了空音频", retryable=True)
                return TTSResult(
                    audio=resp.content, format=self.audio_format, sample_rate=24000
                )

            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            # 400 常见原因是服务不认 speed / response_format，降级重试
            if resp.status_code == 400:
                payload.pop("speed", None)
                payload.pop("response_format", None)
            if resp.status_code in {401, 403, 404}:
                raise TTSError(last_error, retryable=False)
            await self._backoff(attempt)

        raise TTSError(f"合成失败：{last_error}", retryable=True)

    async def _backoff(self, attempt: int) -> None:
        import asyncio

        await asyncio.sleep(min(8.0, 0.8 * (2**attempt)))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def available_voices(self) -> list[str]:
        return COMMON_VOICES

    def health(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "未配置 base_url"
        return True, f"{self.base_url} · {self.model}/{self.voice}"
