"""文本清洗：把解析出来的脏文本洗成适合朗读的干净文本。"""

from __future__ import annotations

import re

_URL = re.compile(r"(?:https?://|www\.)\S+")
_HTML = re.compile(r"<[^>]+>")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff]")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_ONLY_SYMBOLS = re.compile(r"^[\W_]+$", re.UNICODE)
# 网文常见的作者话/广告行
_AD_LINE = re.compile(
    r"^\s*(?:本章未完|请记住本站|手机阅读|最新网址|章节错误|加入书签|投推荐票|求月票|感谢.*?的.*?票)",
)
_CHAPTER_NOISE = re.compile(r"^\s*[（(]?\s*(?:第\s*\d+\s*[页章]\s*/\s*共\s*\d+\s*[页章]|本章完)\s*[)）]?\s*$")

# 常见全角/半角与不规范标点统一
_PUNCT_MAP = {
    "\u201c": "“", "\u201d": "”",
    "\u2018": "‘", "\u2019": "’",
    "\u2026": "…",
    "\u3002": "。",
    "\u00a0": " ",
    "\u3000": " ",
}


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = _ZERO_WIDTH.sub("", text)
    text = _HTML.sub("", text)
    for src, dst in _PUNCT_MAP.items():
        text = text.replace(src, dst)
    text = _URL.sub("", text)
    text = _MULTI_SPACE.sub(" ", text)
    return text.strip()


def is_speakable(text: str) -> bool:
    """过滤空行、纯符号行、页码、"本章完" 之类噪声。"""
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    if _ONLY_SYMBOLS.match(stripped):
        return False
    if _AD_LINE.match(stripped) or _CHAPTER_NOISE.match(stripped):
        return False
    # 至少有一个中日韩字或拉丁字母或数字
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", stripped):
        return False
    return True


def clean_block_text(text: str) -> str | None:
    cleaned = clean_text(text)
    return cleaned if is_speakable(cleaned) else None


def estimate_seconds(chars: int, chars_per_second: float = 5.2) -> float:
    """中文朗读经验值：约 5 字/秒。"""
    return chars / chars_per_second


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m} 分"
    if m:
        return f"{m} 分 {s} 秒"
    return f"{s} 秒"


def strip_speaker_prefix(text: str) -> str:
    """去掉 "张三说：" 这样的说话人前缀（已由 speaker 字段承载）。"""
    return re.sub(r"^[^，。！？：:“”]{1,12}(?:说|道|问|答|喊|笑道|说道|问道)[：:]\s*", "", text)


def spoken_length(text: str) -> int:
    """计算"实际会读出来"的字数，用于估算时长。"""
    return len(re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE))
