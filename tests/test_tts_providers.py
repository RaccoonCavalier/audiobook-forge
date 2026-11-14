"""TTS 提供商的协议层测试。

没有真实 key 也能验证：请求体构造、鉴权头/签名、响应解析、错误降级。
这是"接了但没跑过"和"接了且验证过"的分界线。
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from audiobook_forge.tts.base import TTSError, TTSRequest
from audiobook_forge.tts.mimo import MimoTTS
from audiobook_forge.tts.xfyun import XfyunTTS


def _mimo(handler, **kwargs) -> MimoTTS:
    return MimoTTS(
        api_key="test-key",
        base_url="https://example.test/v1",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _ok_body(payload: bytes = b"ID3-fake-mp3") -> dict:
    return {
        "choices": [
            {"message": {"audio": {"data": base64.b64encode(payload).decode()}}}
        ]
    }


# ------------------------------------------------------------------ MiMo


def test_mimo_builds_correct_request() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    tts = _mimo(handler, max_retries=0)
    result = asyncio.run(tts.synthesize(TTSRequest(text="夜色渐深。", voice="冰糖")))
    asyncio.run(tts.aclose())

    assert result.audio == b"ID3-fake-mp3"
    assert result.format == "mp3"
    assert captured["url"] == "https://example.test/v1/chat/completions"
    body = captured["body"]
    assert body["model"] == "mimo-v2.5-tts"
    assert body["audio"] == {"format": "mp3", "voice": "冰糖"}
    # 必须有一条 assistant 消息承载待合成文本
    assert body["messages"][-1] == {"role": "assistant", "content": "夜色渐深。"}
    assert captured["headers"]["api-key"] == "test-key"


def test_mimo_passes_style_via_user_message() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    tts = _mimo(handler, max_retries=0)
    asyncio.run(
        tts.synthesize(TTSRequest(text="你来了。", voice="苏打", style="冰冷 压低声线"))
    )
    asyncio.run(tts.aclose())

    messages = captured["body"]["messages"]
    assert messages[0]["role"] == "user"
    assert "冰冷 压低声线" in messages[0]["content"]
    assert messages[-1]["role"] == "assistant"


def test_mimo_retries_without_user_message_on_400() -> None:
    """有些模型只接受 assistant 消息；400 后应自动降级重试。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(400, text="unsupported role")
        return httpx.Response(200, json=_ok_body())

    tts = _mimo(handler, max_retries=1)
    result = asyncio.run(tts.synthesize(TTSRequest(text="走吧。", voice="冰糖", style="温柔")))
    asyncio.run(tts.aclose())

    assert len(calls) == 2
    assert len(calls[0]["messages"]) == 2
    assert [m["role"] for m in calls[1]["messages"]] == ["assistant"]
    assert result.audio


def test_mimo_missing_audio_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {}}]})

    tts = _mimo(handler, max_retries=0)
    with pytest.raises(TTSError):
        asyncio.run(tts.synthesize(TTSRequest(text="hi", voice="冰糖")))
    asyncio.run(tts.aclose())


def test_mimo_without_key_fails_fast() -> None:
    tts = MimoTTS(api_key="")
    with pytest.raises(TTSError) as exc:
        asyncio.run(tts.synthesize(TTSRequest(text="hi", voice="冰糖")))
    assert exc.value.retryable is False


def test_mimo_server_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    tts = _mimo(handler, max_retries=0)
    with pytest.raises(TTSError) as exc:
        asyncio.run(tts.synthesize(TTSRequest(text="hi", voice="冰糖")))
    assert exc.value.retryable is True
    asyncio.run(tts.aclose())


# ----------------------------------------------------------------- 讯飞


def test_xfyun_auth_with_api_password() -> None:
    tts = XfyunTTS(app_id="app1", api_password="pw123")
    url, headers = tts._auth()
    assert url == tts.base_url
    assert headers == {"x-api-key": "pw123"}


def test_xfyun_auth_hmac_signature() -> None:
    tts = XfyunTTS(app_id="app1", api_key="key1", api_secret="secret1")
    url, headers = tts._auth()

    assert headers == {}
    assert "authorization=" in url
    assert "date=" in url
    assert "host=" in url

    # 取出 authorization 解码，验证格式与官方文档一致
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    authorization = base64.b64decode(query["authorization"][0]).decode()
    assert authorization.startswith('api_key="key1"')
    assert 'algorithm="hmac-sha256"' in authorization
    assert 'headers="host date request-line"' in authorization
    assert "signature=" in authorization


def test_xfyun_health_requires_credentials() -> None:
    assert XfyunTTS(app_id="").health()[0] is False
    assert XfyunTTS(app_id="a").health()[0] is False
    assert XfyunTTS(app_id="a", api_password="p").health()[0] is True
    assert XfyunTTS(app_id="a", api_key="k", api_secret="s").health()[0] is True


def test_xfyun_style_mapping() -> None:
    tts = XfyunTTS(app_id="a", api_password="p")

    speed, pitch, volume = tts._map_style(TTSRequest(text="x", voice="v"), "温柔 舒缓")
    assert speed == 42

    speed, pitch, volume = tts._map_style(TTSRequest(text="x", voice="v"), "激动 大声")
    assert volume == 70

    speed, _pitch, _volume = tts._map_style(TTSRequest(text="x", voice="v"), "低沉 悲伤")
    assert speed == 44

    # 没有风格时用默认值
    speed, pitch, volume = tts._map_style(TTSRequest(text="x", voice="v"), None)
    assert (speed, pitch, volume) == (50, 50, 50)


def test_xfyun_speed_multiplier() -> None:
    tts = XfyunTTS(app_id="a", api_password="p")
    speed, _, _ = tts._map_style(TTSRequest(text="x", voice="v", speed=1.4), None)
    assert speed == 70
