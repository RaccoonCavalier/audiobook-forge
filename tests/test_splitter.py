"""分句与分段测试：TTS 的成败很大程度取决于这里。"""

from __future__ import annotations

from audiobook_forge.textproc import to_segments
from audiobook_forge.textproc.splitter import split_sentences


def test_split_basic() -> None:
    assert split_sentences("你好。我很好！你呢？") == ["你好。", "我很好！", "你呢？"]


def test_split_keeps_closing_quote() -> None:
    parts = split_sentences("“你来了。”他说。“嗯。”")
    assert parts[0] == "“你来了。”"


def test_split_ellipsis_and_bang() -> None:
    parts = split_sentences("等等……！！然后呢？")
    assert parts[0] == "等等……！！"


def test_segments_respect_max() -> None:
    text = "这是一句很长的测试文本，" * 20
    segs = to_segments(text, min_chars=10, max_chars=50)
    assert segs
    assert all(len(s) <= 50 for s in segs)


def test_segments_merge_tiny() -> None:
    segs = to_segments("好。走吧。", min_chars=10, max_chars=80)
    assert len(segs) == 1


def test_segments_never_empty() -> None:
    assert to_segments("") == []
    assert to_segments("   ") == []


def test_long_sentence_hard_split() -> None:
    segs = to_segments("啊" * 300, min_chars=10, max_chars=40)
    assert all(len(s) <= 40 for s in segs)
    assert sum(len(s) for s in segs) == 300
