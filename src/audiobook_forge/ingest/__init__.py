"""统一入口：按扩展名/魔数分派到具体解析器。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from ..models import Book
from .epub import parse_epub
from .markdown import parse_markdown
from .txt import extract_meta_preface, parse_txt

SUPPORTED = {".epub": "epub", ".txt": "txt", ".md": "md", ".markdown": "md"}


def detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in SUPPORTED:
        return SUPPORTED[suffix]
    # 扩展名不可信时按内容猜
    if zipfile.is_zipfile(path):
        return "epub"
    return "txt"


def load_book(path: Path) -> Book:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到文件：{path}")

    fmt = detect_format(path)
    if fmt == "epub":
        return parse_epub(path)
    if fmt == "md":
        return parse_markdown(path)
    book = parse_txt(path)
    meta = extract_meta_preface(path)
    if meta.get("作者") and not book.author:
        book.author = meta["作者"]
    if meta.get("书名"):
        book.title = meta["书名"]
    return book
