"""LLM 抽象：只要求"给 system+user，返回合法 JSON"。"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class LLMError(RuntimeError):
    pass


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, other: "LLMUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    data: dict
    raw: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)


class BaseLLM(ABC):
    @abstractmethod
    async def chat_json(self, system: str, user: str) -> LLMResponse:
        """发送请求并返回解析后的 JSON。"""

    async def aclose(self) -> None:
        return None


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def extract_json(text: str) -> dict:
    """从模型输出里尽力挖出 JSON：直接解析 → 去代码围栏 → 截取首尾括号。"""
    text = (text or "").strip()
    if not text:
        raise LLMError("模型返回为空")

    candidates = [text]
    match = _FENCE.search(text)
    if match:
        candidates.append(match.group(1))

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    l_start, l_end = text.find("["), text.rfind("]")
    if l_start != -1 and l_end > l_start:
        candidates.append(text[l_start : l_end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}

    raise LLMError(f"无法解析为 JSON：{text[:200]}")
