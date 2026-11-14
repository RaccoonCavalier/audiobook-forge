"""编排：解析 → 分析 → 分段 → 合成 → 拼接。每一步都可单独重跑（断点续跑）。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from ..config import Settings
from ..db import Database
from ..ingest import load_book
from ..llm.analyzer import Analyzer
from ..llm.base import BaseLLM
from ..llm.openai_compat import OpenAICompatLLM
from ..models import Book, Cast, Job, JobStatus, SegmentStatus
from ..tts.base import BaseTTS
from ..tts.registry import create_tts
from .assemble import assemble
from .planner import build_segments
from .synthesize import SynthStats, Synthesizer

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int, str], Awaitable[None] | None]


@dataclass
class RunOptions:
    provider: str | None = None
    read_headings: bool = True
    make_m4b: bool = True
    skip_llm: bool = False
    force_reanalyze: bool = False


class Pipeline:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    # ------------------------------------------------------------------ 路径

    def book_dir(self, book_id: str) -> Path:
        return self.settings.books_dir / book_id

    def book_json(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "book.json"

    def audio_dir(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "audio"

    def out_dir(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "out"

    # ------------------------------------------------------------- 读取/写入

    def load_book(self, book_id: str) -> Book:
        path = self.book_json(book_id)
        if not path.is_file():
            raise FileNotFoundError(f"找不到书籍数据：{path}")
        return Book.model_validate_json(path.read_text(encoding="utf-8"))

    def save_book(self, book: Book) -> None:
        directory = self.book_dir(book.id)
        directory.mkdir(parents=True, exist_ok=True)
        self.book_json(book.id).write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )
        self.db.save_book(book)

    def load_cast(self, book_id: str) -> Cast | None:
        path = self.book_dir(book_id) / "cast.json"
        if not path.is_file():
            return None
        return Cast.model_validate_json(path.read_text(encoding="utf-8"))

    def save_cast(self, book_id: str, cast: Cast) -> None:
        path = self.book_dir(book_id) / "cast.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cast.model_dump_json(indent=2), encoding="utf-8")

    def manifest(self, book_id: str) -> dict | None:
        path = self.out_dir(book_id) / "manifest.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ 步骤

    def import_file(self, source: Path) -> Book:
        book = load_book(source)
        if not book.chapters:
            raise ValueError("没有解析出任何章节，请检查文件内容")
        self.save_book(book)
        return book

    async def analyze(
        self,
        book_id: str,
        options: RunOptions,
        progress: ProgressFn | None = None,
    ) -> tuple[Book, Cast]:
        book = self.load_book(book_id)
        llm: BaseLLM | None = None
        if not options.skip_llm and self.settings.llm.usable:
            llm = OpenAICompatLLM(
                base_url=self.settings.llm.base_url,
                api_key=self.settings.llm.api_key,
                model=self.settings.llm.model,
                temperature=self.settings.llm.temperature,
                timeout=self.settings.llm.timeout,
            )

        analyzer = Analyzer(llm, self.settings)
        try:
            await _emit(progress, "选角", 0, 1, "正在识别角色")
            cast = await analyzer.discover_cast(book)
            self.save_cast(book_id, cast)

            await _emit(progress, "标注", 0, len(book.chapters), "正在标注对白与情绪")
            analyzed = await analyzer.analyze_book(book, cast, _as_callback(progress, "标注"))
        finally:
            if llm is not None:
                await llm.aclose()

        self.save_book(analyzed)
        return analyzed, cast

    def plan(
        self,
        book: Book,
        cast: Cast,
        options: RunOptions,
    ) -> int:
        segments = build_segments(
            book, cast, self.settings, read_headings=options.read_headings
        )
        if not segments:
            raise ValueError("没有可朗读的内容（可能全部被判定为 skip）")
        self.db.replace_segments(book.id, segments)
        return len(segments)

    async def synthesize(
        self,
        book_id: str,
        options: RunOptions,
        progress: ProgressFn | None = None,
    ) -> SynthStats:
        book = self.load_book(book_id)
        pending = self.db.pending_segments(book_id)
        if not pending:
            stats = SynthStats(total=0)
            await _emit(progress, "合成", 0, 0, "所有分片已就绪")
            return stats

        tts = create_tts(self.settings, options.provider)
        synthesizer = Synthesizer(
            tts, self.settings, self.audio_dir(book_id), self.settings.cache_dir
        )

        total = len(pending)
        state = {"done": 0}

        async def on_segment(segment, status, error) -> None:  # noqa: ANN001
            state["done"] += 1
            # SQLite 写是同步 IO，放线程池，避免卡住事件循环（Web 端轮询会卡顿）
            await asyncio.to_thread(
                self.db.mark_segment,
                segment.id,
                status,
                audio_path=str(segment.audio_path) if segment.audio_path else None,
                error=error,
                bump_attempts=(status == SegmentStatus.failed),
            )
            if progress:
                await _emit(
                    progress, "合成", state["done"], total,
                    f"{segment.chapter_title} · {segment.seq}/{total}",
                )

        try:
            stats = await synthesizer.run(pending, on_segment)
        finally:
            await tts.aclose()
        return stats

    def assemble_now(self, book_id: str, options: RunOptions) -> dict:
        book = self.load_book(book_id)
        segments = self.db.segments_for_book(book_id)
        return assemble(
            book, segments, self.settings, self.out_dir(book_id),
            make_m4b=options.make_m4b,
        )

    # ------------------------------------------------------------- 端到端

    async def run_all(
        self,
        book_id: str,
        options: RunOptions,
        job: Job | None = None,
        progress: ProgressFn | None = None,
    ) -> dict:
        started = time.monotonic()

        def update(stage: str, message: str = "", **kw) -> None:
            if job:
                self.db.update_job(job.id, stage=stage, message=message, **kw)

        try:
            update("analyze", "解析与标注")
            book, cast = await self.analyze(book_id, options, progress)

            update("plan", "生成分段")
            total = self.plan(book, cast, options)
            if job:
                self.db.update_job(job.id, total=total)

            update("synthesize", "合成音频")
            stats = await self.synthesize(book_id, options, progress)
            if progress:
                await _emit(progress, "合成", stats.ok, stats.total, f"完成 {stats.ok}，失败 {stats.failed}")

            update("assemble", "拼接导出")
            # 拼接要跑多次 ffprobe + concat，耗时且阻塞，放线程池
            manifest = await asyncio.to_thread(self.assemble_now, book_id, options)

            if job:
                self.db.update_job(
                    job.id,
                    status=JobStatus.done,
                    stage="done",
                    message=f"完成，用时 {time.monotonic() - started:.0f}s",
                    done=stats.ok,
                )
            return manifest

        except Exception as exc:  # noqa: BLE001
            log.exception("任务失败")
            if job:
                self.db.update_job(job.id, status=JobStatus.failed, message=str(exc)[:500])
            raise


async def _emit(
    progress: ProgressFn | None, stage: str, done: int, total: int, message: str = ""
) -> None:
    if progress is None:
        return
    result = progress(stage, done, total, message)
    if inspect.isawaitable(result):
        await result


def _as_callback(progress: ProgressFn | None, stage: str):
    if progress is None:
        return None

    async def callback(_stage: str, done: int, total: int) -> None:
        await _emit(progress, stage, done, total, "")

    return callback


def cleanup_book(settings: Settings, db: Database, book_id: str) -> None:
    db.delete_book(book_id)
    directory = settings.books_dir / book_id
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
