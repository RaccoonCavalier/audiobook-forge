"""Markdown：按标题层级切章，并清掉代码块、表格、图片等不该朗读的噪声。"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Block, Book, Chapter
from .base import read_text

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_CODE_FENCE = re.compile(r"^\s*(```|~~~)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~|`)")
_HR = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_FRONT_MATTER = re.compile(r"^---\s*$")


def parse_markdown(path: Path) -> Book:
    text = read_text(path)
    lines = text.split("\n")

    # 跳过 YAML front matter
    start = 0
    if lines and _FRONT_MATTER.match(lines[0]):
        for i in range(1, len(lines)):
            if _FRONT_MATTER.match(lines[i]):
                start = i + 1
                break

    chapters: list[Chapter] = []
    current_title: str | None = None
    current_blocks: list[Block] = []
    in_code = False

    def flush() -> None:
        nonlocal current_title, current_blocks
        if current_blocks:
            title = current_title or ("正文" if not chapters else f"第 {len(chapters) + 1} 节")
            chapters.append(Chapter(index=len(chapters), title=title, blocks=current_blocks))
        current_blocks = []

    for raw in lines[start:]:
        line = raw.rstrip()

        if _CODE_FENCE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        if _HR.match(line) or _TABLE_ROW.match(line):
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = _clean_inline(heading.group(2))
            if level <= 2:
                flush()
                current_title = title
                current_blocks = []
            elif title:
                current_blocks.append(Block(type="heading", text=title))
            continue

        para = _clean_inline(line)
        if para:
            current_blocks.append(Block(type="narration", text=para))

    flush()

    # 一本书只有一个 h1 时，用文件名兜底
    if len(chapters) == 1:
        chapters[0].title = chapters[0].title or path.stem

    return Book(
        title=path.stem,
        source_format="md",
        source_name=path.name,
        chapters=chapters,
    )


def _clean_inline(line: str) -> str:
    line = _IMAGE.sub("", line)
    line = _LINK.sub(r"\1", line)
    line = _HTML_TAG.sub("", line)
    line = _EMPHASIS.sub("", line)
    line = line.replace("\\", "")
    line = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", line)  # 列表符号
    line = re.sub(r"^\s*>\s?", "", line)               # 引用
    line = re.sub(r"[ \t]+", " ", line)
    return line.strip()
