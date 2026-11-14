"""规则分拣的回归测试 —— 说话人抽取很容易出错，单独盯住。"""

from __future__ import annotations

from audiobook_forge.config import load_settings
from audiobook_forge.llm.analyzer import Analyzer, _rule_one_block
from audiobook_forge.models import Block, Book, Chapter


def test_speaker_not_greedy() -> None:
    """'沈砚说道' 不能抽成 '沈砚说'。"""
    block = Block(type="narration", text="“你终于来了。”沈砚说道，声音很低。")
    btype, speaker, _ = _rule_one_block(block, ["沈砚", "苏晚"])
    assert btype == "dialogue"
    assert speaker == "沈砚"


def test_speaker_at_line_start() -> None:
    block = Block(type="narration", text="张三说道：“别来无恙？”")
    btype, speaker, _ = _rule_one_block(block, ["张三"])
    assert btype == "dialogue"
    assert speaker == "张三"


def test_unknown_speaker_kept() -> None:
    block = Block(type="narration", text="“走吧。”另一个人说道。")
    btype, speaker, _ = _rule_one_block(block, ["张三"])
    assert btype == "dialogue"
    assert speaker is not None


def test_narration_without_quotes() -> None:
    block = Block(type="narration", text="夜色渐深，远处的灯火像碎金。")
    btype, speaker, _ = _rule_one_block(block, ["张三"])
    assert btype == "narration"
    assert speaker is None


def test_knows_is_not_speaker() -> None:
    """'他知道' 里的 '道' 不能被当成说话动词。"""
    block = Block(type="narration", text="“这件事他知道。”")
    _btype, speaker, _ = _rule_one_block(block, ["他知"])
    assert speaker != "他知"


def test_rule_cast_collects_characters() -> None:
    book = Book(
        title="t",
        source_format="txt",
        chapters=[
            Chapter(
                index=0,
                title="一",
                blocks=[
                    Block(text="“你来了。”沈砚说道。"),
                    Block(text="“嗯。”苏晚答道。"),
                ],
            )
        ],
    )
    analyzer = Analyzer(None, load_settings())
    cast = analyzer._rule_cast(book, "mock", "mock-narrator")
    names = {a.speaker for a in cast.assignments}
    assert "沈砚" in names
    assert "苏晚" in names
