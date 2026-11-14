"""TXT：正则识别章节，找不到就按字数切块。"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Block, Book, Chapter
from .base import _META_LINE, chunk_long_text, is_chapter_title, read_text, split_paragraphs

_CHAPTER_LIMIT = 8000


def parse_txt(path: Path) -> Book:
    text = read_text(path)
    lines = text.split("\n")

    # 1) 找出所有章节标题所在行
    marks: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if is_chapter_title(stripped):
            marks.append((idx, stripped))

    chapters: list[Chapter] = []

    if len(marks) >= 2:
        # 正文前的部分（书名、作者、简介）单独成章；纯元信息行直接丢掉
        if marks[0][0] > 0:
            preface = "\n".join(lines[: marks[0][0]])
            blocks = [
                b for b in _blocks_from(preface)
                if not _META_LINE.match(b.text)
            ]
            if blocks:
                chapters.append(Chapter(index=0, title="前言", blocks=blocks))

        for i, (line_idx, title) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(lines)
            body = "\n".join(lines[line_idx + 1 : end])
            blocks = _blocks_from(body)
            if not blocks:
                continue
            chapters.append(Chapter(index=len(chapters), title=title, blocks=blocks))
    else:
        for i, chunk in enumerate(chunk_long_text(text, _CHAPTER_LIMIT)):
            blocks = _blocks_from(chunk)
            if blocks:
                chapters.append(Chapter(index=i, title=f"第 {i + 1} 部分", blocks=blocks))

    title = _guess_title(path, chapters)
    return Book(
        title=title,
        source_format="txt",
        source_name=path.name,
        chapters=chapters,
    )


def _blocks_from(body: str) -> list[Block]:
    return [Block(type="narration", text=p) for p in split_paragraphs(body)]


def _guess_title(path: Path, chapters: list[Chapter]) -> str:
    stem = path.stem
    # 去掉常见的下载站后缀
    stem = re.sub(r"[（(\[【].*?[)）\]】]", "", stem).strip()
    # 只剩一串哈希（历史上传命名遗留）时不要当书名
    if re.fullmatch(r"[0-9a-fA-F]{16,}", stem):
        return "未命名"
    return stem or "未命名"


_BOOK_META = re.compile(r"^\s*(?:书名|作者|简介|内容简介)\s*[:：]\s*(.+)$")


def extract_meta_preface(path: Path) -> dict[str, str]:
    """从 txt 开头的 "书名：xxx / 作者：xxx" 里抽元信息（尽力而为）。"""
    meta: dict[str, str] = {}
    head = "\n".join(read_text(path).split("\n")[:30])
    for line in head.split("\n"):
        m = _BOOK_META.match(line)
        if m:
            key = line.split(":", 1)[0].split("：", 1)[0].strip()
            meta.setdefault(key, m.group(1).strip())
    return meta
