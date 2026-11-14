"""配置加载：环境变量优先，其次 .env 文件。

设计原则：所有可调参数都能通过环境变量覆盖，方便 Docker/NAS 里用 compose 注入。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def load_dotenv(path: Path) -> None:
    """极简 .env 解析，避免额外依赖。已存在的环境变量不覆盖。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _str(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    if value is None or not value.strip():
        return default
    return value.strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_str(key, str(default)))
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    raw = _str(key, "")
    if not raw:
        return default
    return raw.lower() in _TRUTHY


@dataclass(slots=True)
class LLMSettings:
    """文本分拣模型。任何 OpenAI 兼容端点都可以。"""

    enabled: bool = True
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.25
    timeout: float = 180.0
    concurrency: int = 3

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.api_key)


@dataclass(slots=True)
class MimoSettings:
    api_key: str = ""
    base_url: str = "https://api.xiaomimimo.com/v1"
    model: str = "mimo-v2.5-tts"
    default_voice: str = "冰糖"
    # 旁白与角色共用同一批内置音色
    narrator_voice: str = "冰糖"
    audio_format: str = "mp3"


@dataclass(slots=True)
class XfyunSettings:
    app_id: str = ""
    api_key: str = ""
    api_secret: str = ""
    # 鉴权方式一用 APIPassword，方式二用 api_key+api_secret 签名
    api_password: str = ""
    voice: str = "x6_lingxiaoxuan_pro"
    narrator_voice: str = "x6_pangbainv1_pro"
    base_url: str = "wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6"
    speed: int = 50
    volume: int = 50
    pitch: int = 50
    oral_level: str = "mid"


@dataclass(slots=True)
class TTSSettings:
    provider: str = "mock"          # mock | mimo | xfyun
    concurrency: int = 4
    retries: int = 3
    bitrate: str = "64k"
    sample_rate: int = 44100
    mimo: MimoSettings = field(default_factory=MimoSettings)
    xfyun: XfyunSettings = field(default_factory=XfyunSettings)

    def voice_for(self, provider: str, gender: str = "unknown") -> str:
        """按性别给一个后备音色（LLM 不可用时的规则兜底）。"""
        male = {"mimo": "苏打", "xfyun": "x6_lingfeiyi_pro", "mock": "mock-male"}
        female = {"mimo": "冰糖", "xfyun": "x6_lingxiaoxuan_pro", "mock": "mock-female"}
        child = {"mimo": "茉莉", "xfyun": "x6_lingyouyou_pro", "mock": "mock-child"}
        table = {"male": male, "female": female, "child": child}
        chosen = table.get(gender, female)
        if provider == "mimo":
            return self.mimo.default_voice if gender == "unknown" else chosen["mimo"]
        if provider == "xfyun":
            return self.xfyun.voice if gender == "unknown" else chosen["xfyun"]
        return chosen["mock"]


@dataclass(slots=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    llm: LLMSettings
    tts: TTSSettings
    segment_min_chars: int
    segment_max_chars: int
    chapter_gap_ms: int
    block_gap_ms: int
    ffmpeg: str
    log_level: str

    @property
    def books_dir(self) -> Path:
        return self.data_dir / "books"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "forge.db"

    @property
    def audio_sample_rate(self) -> int:
        return self.tts.sample_rate

    @property
    def audio_bitrate(self) -> str:
        return self.tts.bitrate

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.books_dir, self.uploads_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: str | Path | None = None) -> Settings:
    env_path = Path(env_file) if env_file else Path.cwd() / ".env"
    load_dotenv(env_path)

    data_dir = Path(_str("ABF_DATA_DIR", "./data")).expanduser().resolve()

    return Settings(
        data_dir=data_dir,
        host=_str("ABF_HOST", "127.0.0.1"),
        port=_int("ABF_PORT", 8848),
        llm=LLMSettings(
            enabled=_bool("ABF_LLM_ENABLED", True),
            base_url=_str("ABF_LLM_BASE_URL", "https://api.deepseek.com/v1"),
            api_key=_str("ABF_LLM_API_KEY", ""),
            model=_str("ABF_LLM_MODEL", "deepseek-chat"),
            temperature=_float("ABF_LLM_TEMPERATURE", 0.25),
            timeout=_float("ABF_LLM_TIMEOUT", 180.0),
            concurrency=_int("ABF_LLM_CONCURRENCY", 3),
        ),
        tts=TTSSettings(
            provider=_str("ABF_TTS_PROVIDER", "mock").lower(),
            concurrency=_int("ABF_TTS_CONCURRENCY", 4),
            retries=_int("ABF_TTS_RETRIES", 3),
            bitrate=_str("ABF_AUDIO_BITRATE", "64k"),
            sample_rate=_int("ABF_AUDIO_SAMPLE_RATE", 44100),
            mimo=MimoSettings(
                api_key=_str("ABF_MIMO_API_KEY", ""),
                base_url=_str("ABF_MIMO_BASE_URL", "https://api.xiaomimimo.com/v1"),
                model=_str("ABF_MIMO_MODEL", "mimo-v2.5-tts"),
                default_voice=_str("ABF_MIMO_DEFAULT_VOICE", "冰糖"),
                narrator_voice=_str("ABF_MIMO_NARRATOR_VOICE", "冰糖"),
                audio_format=_str("ABF_MIMO_FORMAT", "mp3"),
            ),
            xfyun=XfyunSettings(
                app_id=_str("ABF_XFYUN_APP_ID", ""),
                api_key=_str("ABF_XFYUN_API_KEY", ""),
                api_secret=_str("ABF_XFYUN_API_SECRET", ""),
                api_password=_str("ABF_XFYUN_API_PASSWORD", ""),
                voice=_str("ABF_XFYUN_VOICE", "x6_lingxiaoxuan_pro"),
                narrator_voice=_str("ABF_XFYUN_NARRATOR_VOICE", "x6_pangbainv1_pro"),
                speed=_int("ABF_XFYUN_SPEED", 50),
                volume=_int("ABF_XFYUN_VOLUME", 50),
                pitch=_int("ABF_XFYUN_PITCH", 50),
                oral_level=_str("ABF_XFYUN_ORAL_LEVEL", "mid"),
            ),
        ),
        segment_min_chars=_int("ABF_SEGMENT_MIN_CHARS", 12),
        segment_max_chars=_int("ABF_SEGMENT_MAX_CHARS", 120),
        chapter_gap_ms=_int("ABF_CHAPTER_GAP_MS", 1200),
        block_gap_ms=_int("ABF_BLOCK_GAP_MS", 250),
        ffmpeg=_str("ABF_FFMPEG", "ffmpeg"),
        log_level=_str("ABF_LOG_LEVEL", "INFO").upper(),
    )
