"""输出目录与按章导出。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from audiobook_forge.config import load_settings
from audiobook_forge.db import Database
from audiobook_forge.pipeline.assemble import _chapter_suffix
from audiobook_forge.pipeline.runner import Pipeline, RunOptions


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABF_TTS_PROVIDER", "mock")
    monkeypatch.setenv("ABF_LLM_ENABLED", "false")
    monkeypatch.setenv("ABF_TTS_CONCURRENCY", "2")
    monkeypatch.setenv("ABF_OUTPUT_DIR", str(tmp_path / "out"))
    settings = load_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    return settings, db, Pipeline(settings, db)


def test_output_dir_is_configurable(env, sample_txt: Path) -> None:
    settings, db, pipeline = env
    assert settings.output_dir == (settings.data_dir.parent / "out").resolve()

    book = pipeline.import_file(sample_txt)
    asyncio.run(pipeline.run_all(book.id, RunOptions(skip_llm=True, make_m4b=False)))

    manifest = pipeline.manifest(book.id)
    mp3 = Path(manifest["mp3"])
    assert mp3.is_file()
    # 每本书一个子目录，名字用书名而不是 id
    assert mp3.parent == settings.output_dir / book.title
    assert manifest["output_dir"] == str(settings.output_dir / book.title)


def test_per_chapter_files(env, sample_txt: Path) -> None:
    settings, db, pipeline = env
    book = pipeline.import_file(sample_txt)
    asyncio.run(pipeline.run_all(book.id, RunOptions(skip_llm=True, make_m4b=False)))

    manifest = pipeline.manifest(book.id)
    files = manifest["chapter_files"]
    assert files, "应该每章导出一个文件"
    assert len(files) == len(book.chapters)

    for item in files:
        path = Path(item["file"])
        assert path.is_file()
        assert path.name.startswith(f"{book.title}-")
        assert item["duration_ms"] > 0

    names = [Path(f["file"]).name for f in files]
    assert any("第01章-" in n for n in names)


def test_chapter_filename_rules() -> None:
    assert _chapter_suffix(0, "第一章 雨夜") == "第01章-雨夜"
    assert _chapter_suffix(2, "第三章：风起") == "第03章-风起"
    assert _chapter_suffix(0, "序章") == "序章"
    assert _chapter_suffix(1, "番外 后日谈") == "番外 后日谈"
    assert _chapter_suffix(0, "前言") == "前言"


# ---------------------------------------------------------------- 停顿


def test_gap_between_three_levels(env) -> None:
    from audiobook_forge.models import Segment
    from audiobook_forge.pipeline.assemble import _gap_between

    settings, db, pipeline = env

    def seg(chapter: int, block: int, seq: int) -> Segment:
        return Segment(
            book_id="b", chapter_index=chapter, block_index=block, seq=seq,
            voice="v", text=f"t{seq}",
        )

    a, b = seg(0, 0, 1), seg(0, 0, 2)
    c = seg(0, 1, 3)
    d = seg(1, 0, 4)

    assert _gap_between(a, b, settings) == settings.intra_block_gap_ms
    assert _gap_between(b, c, settings) == settings.block_gap_ms
    assert _gap_between(c, d, settings) == settings.chapter_gap_ms
    # 段落停顿要明显长于句间停顿，否则听感上还是连着的
    assert settings.block_gap_ms > settings.intra_block_gap_ms * 2
    assert settings.chapter_gap_ms > settings.block_gap_ms


def test_planner_assigns_block_index(env, sample_txt: Path) -> None:
    settings, db, pipeline = env
    book = pipeline.import_file(sample_txt)
    analyzed, cast = asyncio.run(pipeline.analyze(book.id, RunOptions(skip_llm=True)))
    pipeline.plan(analyzed, cast, RunOptions())

    segments = db.segments_for_book(book.id)
    assert segments[0].block_index == 0

    # block_index 在每章内从 0 递增，所以只在章内比较
    first_chapter = [s for s in segments if s.chapter_index == segments[0].chapter_index]
    assert len({s.block_index for s in first_chapter}) > 1
    assert [s.block_index for s in first_chapter] == sorted(
        s.block_index for s in first_chapter
    )
    # 同一段切开的分片共享同一个 block_index
    block_zero = [s for s in first_chapter if s.block_index == 0]
    assert block_zero and all(s.chapter_index == segments[0].chapter_index for s in block_zero)


def test_gaps_actually_added_to_audio(env, sample_txt: Path) -> None:
    settings, db, pipeline = env
    book = pipeline.import_file(sample_txt)
    asyncio.run(pipeline.run_all(book.id, RunOptions(skip_llm=True, make_m4b=False)))

    segments = [s for s in db.segments_for_book(book.id) if s.status.value == "done"]
    speech_ms = sum(
        __import__("audiobook_forge.pipeline.audio", fromlist=["x"]).probe_duration_ms(
            Path(s.audio_path), settings.ffmpeg
        )
        for s in segments
    )

    manifest = pipeline.manifest(book.id)
    silence_ms = manifest["duration_ms"] - speech_ms
    # 200 段以上的书里，停顿总时长不可能为零
    assert silence_ms > 0
    assert silence_ms >= settings.intra_block_gap_ms * (len(segments) - 1)
