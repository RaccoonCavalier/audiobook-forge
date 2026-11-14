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
