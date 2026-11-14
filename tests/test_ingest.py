"""解析层测试：三种格式都要能把章节和段落正确抽出来。"""

from __future__ import annotations

from pathlib import Path

from audiobook_forge.ingest import load_book, detect_format


def test_detect_format(tmp_path: Path) -> None:
    txt = tmp_path / "a.txt"
    txt.write_text("x", encoding="utf-8")
    assert detect_format(txt) == "txt"
    md = tmp_path / "a.md"
    md.write_text("# x", encoding="utf-8")
    assert detect_format(md) == "md"


def test_txt_chapters(sample_txt: Path) -> None:
    book = load_book(sample_txt)
    assert book.source_format == "txt"
    assert len(book.chapters) == 2
    assert "开端" in book.chapters[0].title
    assert "相遇" in book.chapters[1].title
    # 书名/作者应从正文头部抽出来
    assert book.title == "测试小说"
    assert book.author == "测试作者"
    assert book.char_count > 40


def test_txt_paragraphs_not_merged(sample_txt: Path) -> None:
    book = load_book(sample_txt)
    texts = [b.text for c in book.chapters for b in c.blocks]
    assert any("张三说道" in t for t in texts)
    assert any("堵车" in t for t in texts)


def test_markdown_cleans_syntax(sample_md: Path) -> None:
    book = load_book(sample_md)
    assert book.source_format == "md"
    joined = "\n".join(b.text for c in book.chapters for b in c.blocks)
    assert "代码块" not in joined           # 代码块被剔除
    assert "print" not in joined
    assert ">" not in joined.split("\n")[0]
    assert "引用的一句话" in joined          # 引用正文保留
    assert "列表项一" in joined


def test_epub_metadata_and_spine(sample_epub: Path) -> None:
    book = load_book(sample_epub)
    assert book.source_format == "epub"
    assert book.title == "测试 EPUB"
    assert book.author == "测试作者"
    assert len(book.chapters) == 2
    assert "第一章" in book.chapters[0].title or "起点" in book.chapters[0].title
    joined = "\n".join(b.text for c in book.chapters for b in c.blocks)
    assert "天亮了" in joined
    assert "天又黑了" in joined
