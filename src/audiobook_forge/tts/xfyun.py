"""科大讯飞 超拟人语音合成（WebSocket 双向流式）。

本实现只用"一次性发完、收完"的简单模式（status=2），足够有声书离线合成。
协议文档：https://www.xfyun.cn/doc/spark/super%20smart-tts.html
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime
from time import mktime
from urllib.parse import urlencode, urlparse

from .base import BaseTTS, TTSError, TTSRequest, TTSResult

log = logging.getLogger(__name__)

# 常用中文发音人（其余可在控制台开通后用 ABF_XFYUN_VOICE 指定）
COMMON_VOICES = [
    "x6_lingxiaoxuan_pro",
    "x6_lingfeiyi_pro",
    "x6_pangbainv1_pro",
    "x6_pangbainan1_pro",
    "x6_lingxiaoyue_pro",
    "x6_lingyuyan_pro",
]

# 风格关键词 → (speed, pitch, volume) 的粗映射。讯飞没有自然语言风格参数。
_STYLE_RULES: list[tuple[tuple[str, ...], dict[str, int]]] = [
    (("慢", "舒缓", "温柔"), {"speed": 42, "pitch": 48}),
    (("快", "急促", "焦急"), {"speed": 66, "pitch": 55}),
    (("激动", "愤怒", "大声", "喊"), {"volume": 70, "pitch": 60}),
    (("低沉", "悲伤", "压抑", "冰冷"), {"pitch": 38, "speed": 44}),
    (("开心", "轻快", "笑"), {"pitch": 58, "speed": 56}),
]


class XfyunTTS(BaseTTS):
    name = "xfyun"

    def __init__(
        self,
        app_id: str,
        api_key: str = "",
        api_secret: str = "",
        api_password: str = "",
        *,
        base_url: str = "wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6",
        default_speed: int = 50,
        default_volume: int = 50,
        default_pitch: int = 50,
        oral_level: str = "mid",
        timeout: float = 90.0,
    ) -> None:
        self.app_id = app_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_password = api_password
        self.base_url = base_url
        self.default_speed = default_speed
        self.default_volume = default_volume
        self.default_pitch = default_pitch
        self.oral_level = oral_level
        self.timeout = timeout

    @property
    def available_voices(self) -> list[str]:
        return list(COMMON_VOICES)

    def health(self) -> tuple[bool, str]:
        if not self.app_id:
            return False, "未配置 ABF_XFYUN_APP_ID"
        if not self.api_password and not (self.api_key and self.api_secret):
            return False, "需要 ABF_XFYUN_API_PASSWORD，或 ABF_XFYUN_API_KEY + ABF_XFYUN_API_SECRET"
        return True, "ok"

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        ok, reason = self.health()
        if not ok:
            raise TTSError(reason, retryable=False)

        text = request.text.strip()
        if not text:
            raise TTSError("待合成文本为空", retryable=False)

        speed, pitch, volume = self._map_style(request, request.style)

        payload = {
            "header": {"app_id": self.app_id, "status": 2},
            "parameter": {
                "oral": {
                    "oral_level": self.oral_level,
                    "spark_assist": 1,
                    "stop_split": 0,
                    "remain": 0,
                },
                "tts": {
                    "vcn": request.voice,
                    "speed": speed,
                    "volume": volume,
                    "pitch": pitch,
                    "bgs": 0,
                    "reg": 0,
                    "rdn": 0,
                    "rhy": 0,
                    "audio": {
                        "encoding": "lame",
                        "sample_rate": 24000,
                        "channels": 1,
                        "bit_depth": 16,
                        "frame_size": 0,
                    },
                },
            },
            "payload": {
                "text": {
                    "encoding": "utf8",
                    "compress": "raw",
                    "format": "plain",
                    "status": 2,
                    "seq": 0,
                    "text": text,
                }
            },
        }

        url, headers = self._auth()
        chunks: list[bytes] = []

        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover
            raise TTSError("缺少 websockets 依赖，请 pip install websockets", retryable=False) from exc

        try:
            async with connect(
                url,
                additional_headers=headers,
                open_timeout=self.timeout,
                close_timeout=5,
                max_size=None,
            ) as ws:
                await ws.send(json.dumps(payload, ensure_ascii=False))

                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.timeout)
                    message = json.loads(raw)
                    header = message.get("header", {})
                    code = header.get("code", 0)
                    if code != 0:
                        raise TTSError(
                            f"讯飞错误 {code}: {header.get('message', '')}",
                            retryable=code not in {10010, 11200, 11201, 10313},
                        )

                    audio = (message.get("payload") or {}).get("audio") or {}
                    data = audio.get("audio")
                    if data:
                        chunks.append(base64.b64decode(data))

                    if header.get("status") == 2:
                        break

        except asyncio.TimeoutError as exc:
            raise TTSError("讯飞合成超时") from exc
        except TTSError:
            raise
        except Exception as exc:  # websockets 的异常类型较杂
            raise TTSError(f"讯飞连接失败：{exc}") from exc

        audio_bytes = b"".join(chunks)
        if not audio_bytes:
            raise TTSError("讯飞返回的音频为空")
        return TTSResult(audio=audio_bytes, format="mp3", sample_rate=24000)

    # ------------------------------------------------------------------ 鉴权

    def _auth(self) -> tuple[str, dict[str, str]]:
        """优先用 APIPassword（简单），否则走 HMAC 签名。"""
        if self.api_password:
            return self.base_url, {"x-api-key": self.api_password}

        parsed = urlparse(self.base_url)
        host = parsed.hostname or ""
        date = _http_date()
        signature_origin = f"host: {host}\ndate: {date}\nGET {parsed.path} HTTP/1.1"
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        signature = base64.b64encode(digest).decode()
        authorization_origin = (
            f'api_key="{self.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode()).decode()
        query = urlencode({"host": host, "date": date, "authorization": authorization})
        return f"{self.base_url}?{query}", {}

    def _map_style(
        self, request: TTSRequest, style: str | None
    ) -> tuple[int, int, int]:
        speed = self.default_speed
        volume = self.default_volume
        pitch = self.default_pitch

        if request.speed and request.speed > 0:
            speed = max(0, min(100, int(50 * request.speed)))

        if not style:
            return speed, pitch, volume

        for keywords, adjustments in _STYLE_RULES:
            if any(word in style for word in keywords):
                speed = adjustments.get("speed", speed)
                pitch = adjustments.get("pitch", pitch)
                volume = adjustments.get("volume", volume)
                break
        return speed, pitch, volume


def _http_date() -> str:
    now = datetime.utcnow()
    return now.strftime("%a, %d %b %Y %H:%M:%S GMT")
