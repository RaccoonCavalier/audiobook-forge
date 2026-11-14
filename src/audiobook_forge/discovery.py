"""从 OpenAI 兼容服务拉取模型 / 音色列表，供设置面板下拉选择。"""

from __future__ import annotations

import httpx

_TTS_VOICE_PATHS = ("/audio/voices", "/voices")


def _base(base_url: str) -> str:
    return (base_url or "").rstrip("/")


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _collect(items) -> list[str]:
    out: list[str] = []
    if isinstance(items, dict):
        # {"data": [...]} / {"models": [...]} / {"voices": {...}}
        for key in ("data", "models", "voices", "items"):
            if key in items:
                out.extend(_collect(items[key]))
                break
        else:
            out.extend(str(v) for v in items.values() if isinstance(v, str))
        return out
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                for key in ("id", "name", "voice", "model"):
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        out.append(value)
                        break
    return out


async def list_models(
    base_url: str, api_key: str = "", *, timeout: float = 15.0
) -> list[str]:
    base = _base(base_url)
    if not base:
        return []
    async with httpx.AsyncClient(timeout=timeout, headers=_headers(api_key)) as client:
        resp = await client.get(f"{base}/models")
        resp.raise_for_status()
        return sorted(dict.fromkeys(_collect(resp.json())))


async def list_voices(
    base_url: str, api_key: str = "", *, timeout: float = 15.0
) -> list[str]:
    """尽力而为：不同服务把音色挂在 /audio/voices 或 /voices 上。"""
    base = _base(base_url)
    if not base:
        return []
    found: list[str] = []
    async with httpx.AsyncClient(timeout=timeout, headers=_headers(api_key)) as client:
        for path in _TTS_VOICE_PATHS:
            try:
                resp = await client.get(f"{base}{path}")
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue
            try:
                found.extend(_collect(resp.json()))
            except ValueError:
                continue
            if found:
                break
    return sorted(dict.fromkeys(found))


async def discover(
    kind: str, base_url: str, api_key: str = "", *, timeout: float = 15.0
) -> dict:
    """kind: llm | tts。返回 models / voices，失败抛异常由上层转成提示。"""
    models = await list_models(base_url, api_key, timeout=timeout)
    voices = await list_voices(base_url, api_key, timeout=timeout) if kind == "tts" else []
    return {"models": models, "voices": voices}
