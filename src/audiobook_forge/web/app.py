"""FastAPI 应用：上传、分析、合成、试听、下载。

长任务都在 asyncio.Task 里跑，前端轮询 job 状态 —— 这样刷新页面也不会丢进度。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..config import Settings, load_settings, save_runtime_overrides
from ..db import Database
from ..discovery import discover
from ..ingest import SUPPORTED, detect_format
from ..models import Job, JobStatus, SegmentStatus
from ..pipeline.runner import Pipeline, RunOptions, cleanup_book
from ..tts.registry import PROVIDER_LABELS, create_tts

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

TASKS: dict[str, asyncio.Task] = {}


_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _repair_filename(name: str) -> str:
    """修掉两种常见文件名编码问题：百分号转义、UTF-8 被当成 latin-1。"""
    name = unquote(name or "")
    if re.search(r"[\u00c0-\u00ff]", name):
        try:
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


def _safe_stem(name: str) -> str:
    """取一个可做文件名的书名，保留中文，去掉路径和非法字符。"""
    name = _repair_filename(name).replace("\\", "/").rsplit("/", 1)[-1]
    stem = _UNSAFE_NAME.sub("_", Path(name).stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem[:80] or "book"


class SettingsUpdate(BaseModel):
    tts_provider: str | None = None
    llm_enabled: bool | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    # OpenAI 兼容 / 本地 TTS
    tts_openai_base_url: str | None = None
    tts_openai_model: str | None = None
    tts_openai_voice: str | None = None
    tts_openai_api_key: str | None = None
    # 停顿（毫秒）
    gap_intra: int | None = None
    gap_block: int | None = None
    gap_chapter: int | None = None


class RunRequest(BaseModel):
    provider: str | None = None
    read_headings: bool = True
    make_m4b: bool = True
    skip_llm: bool = False
    force_reanalyze: bool = False
    # 只合成这些章节（0 基下标）；不传 = 全书
    chapters: list[int] | None = None
    # 单声线：全书统一一个音色（轻小说等难判说话人的场景）
    single_voice: bool = False
    voice: str | None = None


class PreviewRequest(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    voice: str
    style: str | None = None
    provider: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    pipeline = Pipeline(settings, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 上次进程异常退出时，卡在 running 的段要放回 pending，否则永远不重试
        fixed = db.reset_running()
        if fixed:
            log.info("恢复了 %d 个中断的分片", fixed)
        yield
        for task in list(TASKS.values()):
            task.cancel()
        TASKS.clear()

    app = FastAPI(title="Audiobook Forge", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.pipeline = pipeline

    # ------------------------------------------------------------------ 页面

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    async def status() -> dict:
        providers = {}
        for name in ("mock", "mimo", "xfyun", "openai"):
            try:
                tts = create_tts(settings, name)
                ok, message = tts.health()
            except Exception as exc:  # noqa: BLE001
                ok, message = False, str(exc)
            providers[name] = {
                "label": PROVIDER_LABELS.get(name, name),
                "ready": ok,
                "message": message,
            }
        return {
            "version": __version__,
            "provider": settings.tts.provider,
            "providers": providers,
            "llm": {
                "enabled": settings.llm.enabled,
                "ready": settings.llm.usable,
                "model": settings.llm.model,
            },
            "concurrency": settings.tts.concurrency,
            "data_dir": str(settings.data_dir),
        }

    # ------------------------------------------------------------ 设置

    LLM_PRESETS = [
        {
            "id": "siliconflow-deepseek",
            "label": "SiliconFlow · DeepSeek V3.2",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "deepseek-ai/DeepSeek-V3.2",
        },
        {
            "id": "deepseek",
            "label": "DeepSeek 官方",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        },
        {
            "id": "mimo",
            "label": "小米 MiMo",
            "base_url": "https://api.xiaomimimo.com/v1",
            "model": "mimo-v2.6-flash",
        },
        {"id": "custom", "label": "自定义", "base_url": "", "model": ""},
    ]

    def _provider_status() -> list[dict]:
        out = []
        for name in ("mock", "mimo", "xfyun", "openai"):
            try:
                ok, message = create_tts(settings, name).health()
            except Exception as exc:  # noqa: BLE001
                ok, message = False, str(exc)
            out.append(
                {
                    "name": name,
                    "label": PROVIDER_LABELS.get(name, name),
                    "ready": bool(ok),
                    "message": message,
                }
            )
        return out

    def _settings_payload() -> dict:
        return {
            "tts": {
                "provider": settings.tts.provider,
                "concurrency": settings.tts.concurrency,
                "providers": _provider_status(),
                "openai": {
                    "base_url": settings.tts.openai.base_url,
                    "model": settings.tts.openai.model,
                    "voice": settings.tts.openai.voice,
                    "has_key": bool(settings.tts.openai.api_key),
                },
            },
            "llm": {
                "enabled": settings.llm.enabled,
                "base_url": settings.llm.base_url,
                "model": settings.llm.model,
                "usable": settings.llm.usable,
                # 只告诉前端"有没有配 key"，绝不回传密钥本身
                "has_key": bool(settings.llm.api_key),
            },
            "presets": LLM_PRESETS,
            "audio": {
                "intra_block_gap_ms": settings.intra_block_gap_ms,
                "block_gap_ms": settings.block_gap_ms,
                "chapter_gap_ms": settings.chapter_gap_ms,
            },
        }

    @app.get("/api/settings")
    async def get_settings() -> dict:
        return _settings_payload()

    @app.post("/api/settings")
    async def update_settings(req: SettingsUpdate) -> dict:
        if req.tts_provider is not None:
            if req.tts_provider not in {"mock", "mimo", "xfyun", "openai"}:
                raise HTTPException(400, "未知的 TTS 提供商")
            settings.tts.provider = req.tts_provider

        if req.llm_enabled is not None:
            settings.llm.enabled = req.llm_enabled
        if req.llm_base_url is not None and req.llm_base_url.strip():
            settings.llm.base_url = req.llm_base_url.strip()
        if req.llm_model is not None and req.llm_model.strip():
            settings.llm.model = req.llm_model.strip()
        # 留空表示"不改"，避免误把已存的 key 清掉
        if req.llm_api_key is not None and req.llm_api_key.strip():
            settings.llm.api_key = req.llm_api_key.strip()

        if req.tts_openai_base_url is not None and req.tts_openai_base_url.strip():
            settings.tts.openai.base_url = req.tts_openai_base_url.strip()
        if req.tts_openai_model is not None and req.tts_openai_model.strip():
            settings.tts.openai.model = req.tts_openai_model.strip()
        if req.tts_openai_voice is not None and req.tts_openai_voice.strip():
            settings.tts.openai.voice = req.tts_openai_voice.strip()
            settings.tts.openai.narrator_voice = req.tts_openai_voice.strip()
        if req.tts_openai_api_key is not None and req.tts_openai_api_key.strip():
            settings.tts.openai.api_key = req.tts_openai_api_key.strip()

        for field, attr in (
            ("gap_intra", "intra_block_gap_ms"),
            ("gap_block", "block_gap_ms"),
            ("gap_chapter", "chapter_gap_ms"),
        ):
            value = getattr(req, field)
            if value is not None and 0 <= value <= 10000:
                setattr(settings, attr, value)

        path = save_runtime_overrides(settings)
        log.info("设置已更新，写入 %s", path)
        return _settings_payload()

    @app.get("/api/discover")
    async def discover_models(
        kind: str = "llm", base_url: str = "", api_key: str = ""
    ) -> dict:
        """拉取 OpenAI 兼容服务的模型（TTS 还会尝试拉音色）。"""
        if kind not in {"llm", "tts"}:
            raise HTTPException(400, "kind 只能是 llm 或 tts")

        # 没传地址/密钥就用已保存的配置
        if not base_url.strip():
            base_url = (
                settings.llm.base_url if kind == "llm" else settings.tts.openai.base_url
            )
        if not api_key.strip():
            api_key = (
                settings.llm.api_key if kind == "llm" else settings.tts.openai.api_key
            )

        try:
            result = await discover(kind, base_url.strip(), api_key.strip())
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "base_url": base_url}

        return {"ok": True, "base_url": base_url, **result}

    # ------------------------------------------------------------ 书本管理

    @app.post("/api/books")
    async def upload(file: UploadFile) -> dict:
        original = file.filename or "book.txt"
        suffix = Path(_repair_filename(original)).suffix.lower()
        if suffix not in SUPPORTED:
            raise HTTPException(400, f"不支持的文件类型：{suffix or '未知'}（支持 {', '.join(SUPPORTED)}）")

        # 用原标题建子目录存放，解析器会把文件名当书名，避免书名变成一串哈希
        folder = settings.uploads_dir / uuid.uuid4().hex
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / f"{_safe_stem(original)}{suffix}"

        size = 0
        with temp.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    out.close()
                    temp.unlink(missing_ok=True)
                    raise HTTPException(413, "文件超过 200MB")
                out.write(chunk)

        try:
            book = await run_in_threadpool(pipeline.import_file, temp)
        except Exception as exc:  # noqa: BLE001
            temp.unlink(missing_ok=True)
            raise HTTPException(400, f"解析失败：{exc}") from exc

        return {
            "book_id": book.id,
            "title": book.title,
            "author": book.author,
            "chapters": len(book.chapters),
            "blocks": book.block_count,
            "chars": book.char_count,
            "format": book.source_format,
        }

    @app.get("/api/books")
    async def list_books() -> list[dict]:
        books = db.list_books()
        for item in books:
            job = db.latest_job_for_book(item["id"])
            item["job"] = job.model_dump(mode="json") if job else None
            item["has_output"] = pipeline.manifest(item["id"]) is not None
        return books

    @app.get("/api/books/{book_id}")
    async def book_detail(book_id: str) -> dict:
        try:
            book = pipeline.load_book(book_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, "书籍不存在") from exc

        cast = pipeline.load_cast(book_id)
        stats = db.segment_stats(book_id)
        job = db.latest_job_for_book(book_id)
        chapter_stats = db.chapter_segment_stats(book_id)

        return {
            "book": {
                "id": book.id,
                "title": book.title,
                "author": book.author,
                "format": book.source_format,
                "chars": book.char_count,
                "blocks": book.block_count,
            },
            "chapters": [
                {
                    "index": c.index,
                    "title": c.title,
                    "blocks": len(c.blocks),
                    "chars": c.char_count,
                    "dialogues": sum(1 for b in c.blocks if b.type == "dialogue"),
                    # 该章已合成 / 总数，界面用它显示进度并支持按需勾选
                    "seg_done": chapter_stats.get(c.index, {}).get("done", 0),
                    "seg_total": chapter_stats.get(c.index, {}).get("total", 0),
                }
                for c in book.chapters
            ],
            "cast": cast.model_dump(mode="json") if cast else None,
            "segments": stats,
            "job": job.model_dump(mode="json") if job else None,
            "manifest": pipeline.manifest(book_id),
        }

    @app.delete("/api/books/{book_id}")
    async def delete_book(book_id: str) -> dict:
        await run_in_threadpool(cleanup_book, settings, db, book_id)
        return {"ok": True}

    @app.get("/api/books/{book_id}/segments")
    async def segments(
        book_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        status: str | None = None,
    ) -> dict:
        items = db.segments_for_book(
            book_id, SegmentStatus(status) if status else None
        )
        window = items[offset : offset + limit]
        return {
            "total": len(items),
            "offset": offset,
            "items": [
                {
                    "seq": s.seq,
                    "chapter": s.chapter_title,
                    "speaker": s.speaker,
                    "voice": s.voice,
                    "style": s.style,
                    "text": s.text,
                    "status": s.status.value,
                    "duration_ms": s.duration_ms,
                    "error": s.error,
                }
                for s in window
            ],
        }

    # ------------------------------------------------------------ 任务控制

    def _options(req: RunRequest) -> RunOptions:
        return RunOptions(
            provider=req.provider,
            read_headings=req.read_headings,
            make_m4b=req.make_m4b,
            skip_llm=req.skip_llm,
            force_reanalyze=req.force_reanalyze,
            chapters=list(req.chapters) if req.chapters else None,
            single_voice=req.single_voice,
            voice=(req.voice or None) if req.single_voice else None,
        )

    def _start(book_id: str, action: str, req: RunRequest) -> str:
        job = Job(book_id=book_id, stage=action, options=req.model_dump())
        db.create_job(job)
        task = asyncio.create_task(_execute(job, action, _options(req)))
        TASKS[job.id] = task
        task.add_done_callback(lambda _t: TASKS.pop(job.id, None))
        return job.id

    async def _execute(job: Job, action: str, options: RunOptions) -> None:
        db.update_job(job.id, status=JobStatus.running, stage=action, message="启动")

        async def progress(stage: str, done: int, total: int, message: str) -> None:
            db.update_job(job.id, stage=stage, done=done, total=total, message=message)

        try:
            if action == "run":
                await pipeline.run_all(job.book_id, options, job, progress)
            elif action == "analyze":
                await pipeline.analyze(job.book_id, options, progress)
                db.update_job(job.id, status=JobStatus.done, stage="done", message="分析完成")
            elif action == "synthesize":
                # 单独点「合成」时可能既没角色表也没分段，就地补齐，别让用户来回点
                if not db.segments_for_book(job.book_id):
                    cast = pipeline.load_cast(job.book_id)
                    if cast is None:
                        await pipeline.analyze(job.book_id, options, progress)
                        cast = pipeline.load_cast(job.book_id)
                    book = pipeline.load_book(job.book_id)
                    await run_in_threadpool(pipeline.plan, book, cast, options)

                stats = await pipeline.synthesize(job.book_id, options, progress)
                db.update_job(
                    job.id,
                    status=JobStatus.done,
                    stage="done",
                    total=stats.total,
                    done=stats.ok,
                    message=f"合成完成：成功 {stats.ok}，失败 {stats.failed}",
                )
            elif action == "assemble":
                manifest = await run_in_threadpool(
                    pipeline.assemble_now, job.book_id, options
                )
                db.update_job(
                    job.id, status=JobStatus.done, stage="done",
                    message=f"已导出：{Path(manifest['mp3']).name}",
                )
            elif action == "plan":
                book = pipeline.load_book(job.book_id)
                cast = pipeline.load_cast(job.book_id)
                if cast is None:
                    raise ValueError("还没有角色表，请先分析")
                total = await run_in_threadpool(pipeline.plan, book, cast, options)
                db.update_job(job.id, status=JobStatus.done, stage="done", total=total,
                              message=f"生成 {total} 个分片")
            else:
                raise ValueError(f"未知动作：{action}")
        except asyncio.CancelledError:
            db.update_job(job.id, status=JobStatus.cancelled, message="已取消")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("任务 %s 失败", action)
            db.update_job(job.id, status=JobStatus.failed, message=str(exc)[:500])

    @app.post("/api/books/{book_id}/{action}")
    async def start_action(book_id: str, action: str, req: RunRequest | None = None) -> dict:
        if action not in {"run", "analyze", "synthesize", "assemble", "plan"}:
            raise HTTPException(404, "未知动作")
        if not (settings.books_dir / book_id).is_dir():
            raise HTTPException(404, "书籍不存在")
        running = [
            j for j in db.unfinished_jobs() if j.book_id == book_id and j.id in TASKS
        ]
        if running:
            raise HTTPException(409, "该书已有任务在跑，请等待完成")
        job_id = _start(book_id, action, req or RunRequest())
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str) -> dict:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        return job.model_dump(mode="json")

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        if job.status in {JobStatus.done, JobStatus.failed, JobStatus.cancelled}:
            return {"ok": True, "status": job.status.value, "already_finished": True}

        task = TASKS.get(job_id)
        if task is not None and not task.done():
            # 取消信号会让 asyncio.gather 中止后续请求；
            # 已经在跑的 ffmpeg 线程会自然结束，不阻塞前端
            task.cancel()
            return {"ok": True, "status": "cancelling"}

        db.update_job(job_id, status=JobStatus.cancelled, message="已中止")
        return {"ok": True, "status": "cancelled"}

    # ------------------------------------------------------------ 音频访问

    @app.get("/api/books/{book_id}/audio/{seq}")
    async def segment_audio(book_id: str, seq: int) -> FileResponse:
        items = [s for s in db.segments_for_book(book_id) if s.seq == seq]
        if not items or not items[0].audio_path or not Path(items[0].audio_path).is_file():
            raise HTTPException(404, "该分片还没有音频")
        return FileResponse(items[0].audio_path, media_type="audio/mpeg")

    @app.get("/api/books/{book_id}/download")
    async def download(
        book_id: str, fmt: str = "mp3", index: int | None = None
    ) -> FileResponse:
        manifest = pipeline.manifest(book_id)
        if not manifest:
            raise HTTPException(404, "还没有可下载的成品")

        if fmt == "chapter":
            files = manifest.get("chapter_files") or []
            hit = next((f for f in files if f.get("index") == index), None)
            if hit is None:
                raise HTTPException(404, "没有这一章的音频")
            path = hit.get("file")
            media_type = "audio/mpeg"
        else:
            path = manifest.get("m4b") if fmt == "m4b" else manifest.get("mp3")
            media_type = "audio/mp4" if fmt == "m4b" else "audio/mpeg"

        if not path or not Path(path).is_file():
            raise HTTPException(404, "该格式不存在")
        return FileResponse(path, media_type=media_type, filename=Path(path).name)

    @app.get("/api/books/{book_id}/cast")
    async def get_cast(book_id: str) -> dict:
        cast = pipeline.load_cast(book_id)
        return {"cast": cast.model_dump(mode="json") if cast else None}

    @app.post("/api/books/{book_id}/cast")
    async def put_cast(book_id: str, payload: dict) -> dict:
        from ..models import Cast

        try:
            cast = Cast.model_validate(payload.get("cast") or payload)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"角色表格式不正确：{exc}") from exc
        await run_in_threadpool(pipeline.save_cast, book_id, cast)
        return {"ok": True, "cast": cast.model_dump(mode="json")}

    # ------------------------------------------------------------ 试听音色

    @app.get("/api/voices")
    async def voices(provider: str = "") -> dict:
        name = (provider or settings.tts.provider or "mock").lower()
        label = PROVIDER_LABELS.get(name, name)
        # 即使没配 key，也把可选的音色列出来，方便用户先看再决定
        static = {
            "mimo": ["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"],
            "xfyun": [
                "x6_lingxiaoxuan_pro", "x6_lingfeiyi_pro", "x6_pangbainv1_pro",
                "x6_pangbainan1_pro", "x6_lingxiaoyue_pro", "x6_lingyuyan_pro",
            ],
        }
        try:
            tts = create_tts(settings, name)
            ok, message = tts.health()
            items = tts.available_voices or static.get(name, [])
            await tts.aclose()
        except Exception as exc:  # noqa: BLE001
            return {
                "provider": name,
                "label": label,
                "ready": False,
                "message": str(exc),
                "voices": static.get(name, []),
            }
        return {
            "provider": name,
            "label": label,
            "ready": ok,
            "message": message,
            "voices": items,
        }

    # ---------------------------------------------------------------- 试听

    @app.post("/api/preview")
    async def preview(req: PreviewRequest) -> Response:
        from ..tts.base import TTSRequest

        try:
            tts = create_tts(settings, req.provider)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
        ok, message = tts.health()
        if not ok:
            raise HTTPException(400, message)
        try:
            result = await tts.synthesize(
                TTSRequest(text=req.text, voice=req.voice, style=req.style)
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"合成失败：{exc}") from exc
        finally:
            await tts.aclose()
        media = "audio/wav" if result.format == "wav" else "audio/mpeg"
        return Response(content=result.audio, media_type=media)

    # ------------------------------------------------------------ 静态资源

    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    s = load_settings()
    uvicorn.run(
        "audiobook_forge.web:app",
        host=s.host,
        port=s.port,
        log_level=s.log_level.lower(),
    )


if __name__ == "__main__":
    main()
