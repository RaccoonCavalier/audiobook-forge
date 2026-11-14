"""端到端：导入 → 分析（规则）→ 分段 → 合成（mock）→ 拼接导出。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from audiobook_forge.config import load_settings
from audiobook_forge.db import Database
from audiobook_forge.models import SegmentStatus
from audiobook_forge.pipeline.runner import Pipeline, RunOptions


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABF_TTS_PROVIDER", "mock")
    monkeypatch.setenv("ABF_LLM_ENABLED", "false")
    monkeypatch.setenv("ABF_TTS_CONCURRENCY", "4")
    settings = load_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    return settings, db, Pipeline(settings, db)


def test_import_and_analyze(env, sample_txt: Path) -> None:
    settings, db, pipeline = env
    book = pipeline.import_file(sample_txt)
    assert book.id

    options = RunOptions(skip_llm=True)
    analyzed, cast = asyncio.run(pipeline.analyze(book.id, options))

    assert cast.narrator_voice
    blocks = [b for c in analyzed.chapters for b in c.blocks]
    assert any(b.type == "dialogue" for b in blocks)
    # 规则分拣至少要认出一个说话人
    assert cast.assignments, "应该从 '张三说道' 里抽到角色"


def test_full_build_mock(env, sample_txt: Path) -> None:
    settings, db, pipeline = env
    book = pipeline.import_file(sample_txt)

    manifest = asyncio.run(
        pipeline.run_all(book.id, RunOptions(skip_llm=True, make_m4b=True))
    )

    assert Path(manifest["mp3"]).is_file()
    assert Path(manifest["mp3"]).stat().st_size > 1000
    assert manifest["duration_ms"] > 0
    assert manifest["chapters"]

    stats = db.segment_stats(book.id)
    assert stats["done"] > 0
    assert stats["failed"] == 0


def test_resume_after_partial(env, sample_txt: Path) -> None:
    """模拟中途崩溃：只跑一部分，再续跑应补齐且不重复计费（缓存命中）。"""
    settings, db, pipeline = env
    book = pipeline.import_file(sample_txt)
    analyzed, cast = asyncio.run(pipeline.analyze(book.id, RunOptions(skip_llm=True)))
    total = pipeline.plan(analyzed, cast, RunOptions())

    segments = db.segments_for_book(book.id)
    # 手工把后一半标记为已完成，模拟断点
    for seg in segments[len(segments) // 2 :]:
        db.mark_segment(seg.id, SegmentStatus.done, audio_path=None)

    stats = asyncio.run(pipeline.synthesize(book.id, RunOptions()))
    assert stats.ok > 0
    final = db.segment_stats(book.id)
    assert final["done"] == total
    assert final["pending"] == 0
    assert final["failed"] == 0


def test_rerun_is_idempotent(env, sample_txt: Path) -> None:
    settings, db, pipeline = env
    book = pipeline.import_file(sample_txt)
    asyncio.run(pipeline.run_all(book.id, RunOptions(skip_llm=True, make_m4b=False)))

    first = db.segment_stats(book.id)
    # 再跑一次合成，应该全部走缓存
    stats = asyncio.run(pipeline.synthesize(book.id, RunOptions()))
    assert stats.ok == 0  # 没有 pending 段
    assert db.segment_stats(book.id) == first


def test_estimate_and_plan(env, sample_txt: Path) -> None:
    settings, db, pipeline = env
    book = pipeline.import_file(sample_txt)
    analyzed, cast = asyncio.run(pipeline.analyze(book.id, RunOptions(skip_llm=True)))
    total = pipeline.plan(analyzed, cast, RunOptions())
    segments = db.segments_for_book(book.id)
    assert total == len(segments)
    assert all(s.voice for s in segments)
    assert all(len(s.text) <= settings.segment_max_chars for s in segments)
