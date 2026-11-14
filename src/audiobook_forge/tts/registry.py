"""provider 工厂：按配置创建 TTS 实例。"""

from __future__ import annotations

from ..config import Settings
from .base import BaseTTS, TTSError
from .mimo import MimoTTS
from .mock import MockTTS
from .xfyun import XfyunTTS

PROVIDERS = ("mock", "mimo", "xfyun")
PROVIDER_LABELS = {
    "mock": "本地模拟（无需密钥，用于调试）",
    "mimo": "小米 MiMo TTS",
    "xfyun": "科大讯飞 超拟人语音合成",
}


def create_tts(settings: Settings, provider: str | None = None) -> BaseTTS:
    name = (provider or settings.tts.provider or "mock").lower()

    if name == "mock":
        return MockTTS()

    if name == "mimo":
        cfg = settings.tts.mimo
        if not cfg.api_key:
            raise TTSError("未配置 ABF_MIMO_API_KEY，无法使用 MiMo", retryable=False)
        return MimoTTS(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            model=cfg.model,
            audio_format=cfg.audio_format,
        )

    if name == "xfyun":
        cfg = settings.tts.xfyun
        return XfyunTTS(
            app_id=cfg.app_id,
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            api_password=cfg.api_password,
            base_url=cfg.base_url,
            default_speed=cfg.speed,
            default_volume=cfg.volume,
            default_pitch=cfg.pitch,
            oral_level=cfg.oral_level,
        )

    raise TTSError(f"未知的 TTS provider：{name}（可选 {', '.join(PROVIDERS)}）", retryable=False)
