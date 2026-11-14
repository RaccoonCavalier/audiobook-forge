"""解析层公共工具：编码探测、段落切分、章节标题识别。"""

from __future__ import annotations

import re
from pathlib import Path

# 常见中文小说章节标题。刻意收紧，避免把 "第一，……" 这种正文误判成标题。
_CHAPTER_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9零〇一二三四五六七八九十百千两]{1,6}\s*[章回节卷部篇集]"
    r"|(?:序章|序言|前言|楔子|引子|尾声|后记|终章|番外(?:篇)?|结局)"
    r"|Chapter\s+[0-9IVXLCivxlc]+"
    r"|CHAPTER\s+[0-9IVXLC]+"
    r")\s*$",
    re.IGNORECASE,
)

_CHAPTER_RE_LOOSE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9零〇一二三四五六七八九十百千两]{1,6}\s*[章回节卷部篇集]\s*[：:、.．\-\s].*"
    r"|Chapter\s+[0-9IVXLCivxlc]+\s*[：:、.．\-\s].*"
    r")\s*$",
    re.IGNORECASE,
)

MAX_TITLE_LEN = 60

# "书名：xxx / 作者：xxx" 这类元信息行，不应被当成正文段落合并朗读
_META_LINE = re.compile(
    r"^\s*(?:书名|作者|简介|内容简介|分类|标签|状态|字数|更新|来源|下载)\s*[:：]"
)


def detect_encoding(path: Path) -> str:
    """按 BOM → 严格 utf-8 → 常见中文编码 的顺序探测。"""
    raw = path.read_bytes()[:65536]
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "gb18030"


def read_text(path: Path) -> str:
    enc = detect_encoding(path)
    data = path.read_bytes()
    try:
        text = data.decode(enc, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def is_chapter_title(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > MAX_TITLE_LEN:
        return False
    return bool(_CHAPTER_RE.match(line) or _CHAPTER_RE_LOOSE.match(line))


def clean_line(line: str) -> str:
    line = line.replace("\u3000", " ").replace("\xa0", " ")
    line = re.sub(r"[ \t]+", " ", line)
    return line.strip()


def split_paragraphs(text: str) -> list[str]:
    """以空行为主的段落切分，兼容只有单换行的 txt。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text)
    out: list[str] = []
    for block in blocks:
        lines = [clean_line(x) for x in block.split("\n")]
        lines = [x for x in lines if x]
        if not lines:
            continue
        # 块内如果每行都很短（网文常见逐句换行），合并成一段；
        # 但元信息行不能被并进去
        if (
            all(len(x) < 40 for x in lines)
            and len(lines) > 1
            and not any(_META_LINE.match(x) for x in lines)
        ):
            out.append("".join(lines))
        else:
            out.extend(lines)
    return out


def chunk_long_text(text: str, limit: int) -> list[str]:
    """在没有章节标记时，按段落边界把长文切成若干块。"""
    paragraphs = split_paragraphs(text)
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for para in paragraphs:
        if size + len(para) > limit and buf:
            chunks.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += len(para) + 1
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks
