"""OpenAI 兼容的 chat/completions 客户端。

DeepSeek、通义、Kimi、MiMo、Ollama、vLLM、LM Studio 都能用这一套。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .base import BaseLLM, LLMError, LLMResponse, LLMUsage, extract_json


class OpenAICompatLLM(BaseLLM):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        temperature: float = 0.25,
        timeout: float = 180.0,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        self._supports_json_mode = True

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=20.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat_json(self, system: str, user: str) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "stream": False,
        }
        if self._supports_json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.client.post(f"{self.base_url}/chat/completions", json=payload)
                if resp.status_code == 400 and self._supports_json_mode and attempt == 0:
                    # 有些端点不认 response_format，去掉再试
                    self._supports_json_mode = False
                    payload.pop("response_format", None)
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")

                body = resp.json()
                content = body["choices"][0]["message"]["content"] or ""
                usage = body.get("usage") or {}
                return LLMResponse(
                    data=extract_json(content),
                    raw=content,
                    usage=LLMUsage(
                        prompt_tokens=int(usage.get("prompt_tokens", 0)),
                        completion_tokens=int(usage.get("completion_tokens", 0)),
                    ),
                )
            except (httpx.HTTPError, KeyError, json.JSONDecodeError, LLMError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(1.5 * (attempt + 1))

        raise LLMError(f"LLM 调用失败（{self.model}）：{last_error}")
