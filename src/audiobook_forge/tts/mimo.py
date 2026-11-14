"""小米 MiMo TTS。

协议是 OpenAI 兼容的 chat/completions：
  - messages 里必须有一条 role=assistant 的消息，其 content 就是待合成的文本；
  - audio.voice 指定内置音色；
  - 音频以 base64 放在 choices[0].message.audio.data。

文档：https://mimo.mi.com/docs/zh-CN/api/audio/tts
"""

from __future__ import annotations

import asyncio
import base64
import logging

import httpx

from .. import __version__
from .base import BaseTTS, TTSError, TTSRequest, TTSResult

log = logging.getLogger(__name__)

BUILTIN_VOICES = ["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]


class MimoTTS(BaseTTS):
    name = "mimo"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.xiaomimimo.com/v1",
        model: str = "mimo-v2.5-tts",
        *,
        audio_format: str = "mp3",
        timeout: float = 120.0,
        max_retries: int = 2,
        style_via_user_message: bool = True,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.audio_format = audio_format
        self.timeout = timeout
        self.max_retries = max_retries
        self.style_via_user_message = style_via_user_message
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=20.0),
                headers={
                    "api-key": self.api_key,
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": f"audiobook-forge/{__version__}",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def available_voices(self) -> list[str]:
        return list(BUILTIN_VOICES)

    def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "未配置 ABF_MIMO_API_KEY"
        return True, "ok"

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        if not self.api_key:
            raise TTSError("未配置 MiMo API Key", retryable=False)

        messages: list[dict] = []
        if request.style and self.style_via_user_message:
            messages.append(
                {"role": "user", "content": f"请用这样的语气朗读：{request.style}"}
            )
        messages.append({"role": "assistant", "content": request.text.strip()})

        payload = {
            "model": self.model,
            "messages": messages,
            "audio": {"format": self.audio_format, "voice": request.voice},
            "stream": False,
        }

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.client.post(f"{self.base_url}/chat/completions", json=payload)
                if resp.status_code >= 400:
                    # 有些模型不接受 user message，去掉后再试一次
                    if attempt == 0 and len(payload["messages"]) > 1:
                        payload["messages"] = [
                            m for m in messages if m["role"] == "assistant"
                        ]
                        await asyncio.sleep(0.2)
                        continue
                    raise TTSError(
                        f"MiMo HTTP {resp.status_code}: {resp.text[:300]}",
                        retryable=resp.status_code >= 500 or resp.status_code == 429,
                    )

                body = resp.json()
                audio_obj = body["choices"][0]["message"].get("audio") or {}
                data = audio_obj.get("data")
                if not data:
                    raise TTSError("MiMo 返回中没有 audio.data")

                audio = base64.b64decode(data)
                if not audio:
                    raise TTSError("MiMo 返回的音频为空")

                return TTSResult(audio=audio, format=self.audio_format, sample_rate=24000)

            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(1.2 * (attempt + 1))

        raise TTSError(f"MiMo 合成失败：{last_error}")
