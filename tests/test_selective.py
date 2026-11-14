"""按章合成、中止续跑、部分导出。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from audiobook_forge.cli import _parse_chapters
from audiobook_forge.config import load_settings
from audiobook_forge.db import Database
from audiobook_forge.pipeline.runner import Pipeline, RunOptions


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABF_TTS_PROVIDER", "mock")
    monkeypatch.setenv("ABF_LLM_ENABLED", "false")
    monkeypatch.setenv("ABF_TTS_CONCURRENCY", "2")
    settings = load_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    return settings, db, Pipeline(settings, db)


def _prepared(env, sample_txt: Path):
    settings, db, pipeline = env
    book = pipeline.import_file(sample_txt)
    analyzed, cast = asyncio.run(pipeline.analyze(book.id, RunOptions(skip_llm=True)))
    total = pipeline.plan(analyzed, cast, RunOptions())
    return book, total


# ------------------------------------------------------------ 章节范围解析


def test_parse_chapters_ranges() -> None:
    assert _parse_chapters("1,3,5-7") == [0, 2, 4, 5, 6]
    assert _parse_chapters("2") == [1]
    assert _parse_chapters("3-1") == [0, 1, 2]  # 反着写也认
    assert _parse_chapters("1，3") == [0, 2]  # 全角逗号
    assert _parse_chapters(None) is None
    assert _parse_chapters("") is None


def test_parse_chapters_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        _parse_chapters("abc")
    with pytest.raises(ValueError):
        _parse_chapters("0")


# ------------------------------------------------------------ 只合成指定章节


def test_synthesize_only_selected_chapter(env, sample_txt: Path) -> None:
    book, total = _prepared(env, sample_txt)
    _, db, pipeline = env

    segments = db.segments_for_book(book.id)
    chapter_one = [s for s in segments if s.chapter_index == 1]
    assert chapter_one and len(chapter_one) < total

    stats = asyncio.run(pipeline.synthesize(book.id, RunOptions(chapters=[1])))

    assert stats.total == len(chapter_one)
    assert stats.ok == len(chapter_one)
    # 第 0 章应当没被碰过
    states = {s.chapter_index: s.status.value for s in db.segments_for_book(book.id)}
    assert states[0] == "pending"
    assert stats.ok > 0


def test_selected_chapters_can_be_resumed(env, sample_txt: Path) -> None:
    book, total = _prepared(env, sample_txt)
    _, db, pipeline = env

    asyncio.run(pipeline.synthesize(book.id, RunOptions(chapters=[0])))
    asyncio.run(pipeline.synthesize(book.id, RunOptions(chapters=[1])))

    stats = db.segment_stats(book.id)
    assert stats["done"] == total
    assert stats["failed"] == 0


def test_assemble_marks_partial(env, sample_txt: Path) -> None:
    book, total = _prepared(env, sample_txt)
    _, db, pipeline = env

    asyncio.run(pipeline.synthesize(book.id, RunOptions(chapters=[1])))
    manifest = pipeline.assemble_now(book.id, RunOptions())

    assert manifest["partial"] is True
    assert manifest["segment_count"] < total
    assert Path(manifest["mp3"]).is_file()

    # 补全后不再标记为部分
    asyncio.run(pipeline.synthesize(book.id, RunOptions()))
    manifest = pipeline.assemble_now(book.id, RunOptions())
    assert manifest["partial"] is False


# ---------------------------------------------------------------- 单声线


def test_single_voice_collapses_to_one_timbre(env, sample_txt: Path) -> None:
    book, total = _prepared(env, sample_txt)
    settings, db, pipeline = env

    multi = db.segments_for_book(book.id)
    assert len({s.voice for s in multi}) > 1, "多角色分段应该有多种音色"

    analyzed = pipeline.load_book(book.id)
    cast = pipeline.load_cast(book.id)
    assert cast is not None

    pipeline.plan(analyzed, cast, RunOptions(single_voice=True))
    single = db.segments_for_book(book.id)
    assert len({s.voice for s in single}) == 1
    assert single[0].voice == cast.narrator_voice

    # 也可以指定具体音色
    pipeline.plan(analyzed, cast, RunOptions(single_voice=True, voice="茉莉"))
    assert {s.voice for s in db.segments_for_book(book.id)} == {"茉莉"}


def test_synthesize_replans_when_voice_mode_changes(env, sample_txt: Path) -> None:
    """已经按多角色分好段后，改用单声线合成，应自动重新分段。"""
    book, _ = _prepared(env, sample_txt)
    settings, db, pipeline = env

    assert len({s.voice for s in db.segments_for_book(book.id)}) > 1

    stats = asyncio.run(pipeline.synthesize(book.id, RunOptions(single_voice=True)))

    segments = db.segments_for_book(book.id)
    assert len({s.voice for s in segments}) == 1
    assert stats.ok == len(segments)
    assert stats.failed == 0
    assert db.segment_stats(book.id)["done"] == len(segments)

    # 计划签名落盘，重复合成不会再次重分段
    plan_file = pipeline.plan_file(book.id)
    assert plan_file.is_file()
    assert '"single_voice": true' in plan_file.read_text(encoding="utf-8")


def test_voice_mode_signature_prevents_replan(env, sample_txt: Path) -> None:
    book, _ = _prepared(env, sample_txt)
    settings, db, pipeline = env

    asyncio.run(pipeline.synthesize(book.id, RunOptions(single_voice=True)))
    seq_before = [s.seq for s in db.segments_for_book(book.id)]

    stats = asyncio.run(pipeline.synthesize(book.id, RunOptions(single_voice=True)))
    # 没有待合成分片，也不会重建
    assert stats.total == 0
    assert [s.seq for s in db.segments_for_book(book.id)] == seq_before


def test_missing_plan_signature_keeps_progress(env, sample_txt: Path) -> None:
    """旧版本没有 plan.json 时，不能把已完成的分片当成"设置变了"而重排。"""
    book, _ = _prepared(env, sample_txt)
    settings, db, pipeline = env

    asyncio.run(pipeline.synthesize(book.id, RunOptions()))
    assert db.segment_stats(book.id)["done"] > 0

    pipeline.plan_file(book.id).unlink()  # 模拟老数据
    stats = asyncio.run(pipeline.synthesize(book.id, RunOptions()))

    assert stats.total == 0, "不该重新生成待合成分片"
    assert db.segment_stats(book.id)["pending"] == 0
    assert pipeline.plan_file(book.id).is_file(), "应该补写签名"


def test_cancel_keeps_finished_segments(env, sample_txt: Path, monkeypatch) -> None:
    book, total = _prepared(env, sample_txt)
    _, db, pipeline = env

    import audiobook_forge.pipeline.runner as runner_mod
    from audiobook_forge.tts.mock import MockTTS

    class SlowMock(MockTTS):
        async def synthesize(self, request):  # noqa: ANN001
            await asyncio.sleep(0.06)
            return await super().synthesize(request)

    monkeypatch.setattr(runner_mod, "create_tts", lambda *a, **k: SlowMock())

    async def go() -> None:
        task = asyncio.create_task(pipeline.synthesize(book.id, RunOptions()))
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())

    stats = db.segment_stats(book.id)
    assert stats["failed"] == 0
    assert stats["done"] < total, "中止后不该把剩下的都算完成"
    # 已完成的保留，剩下的仍是 pending，可以直接续跑
    assert stats["done"] + stats["pending"] == total

    monkeypatch.undo()
    asyncio.run(pipeline.synthesize(book.id, RunOptions()))
    assert db.segment_stats(book.id)["done"] == total
