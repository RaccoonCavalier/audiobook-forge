"""命令行入口：适合在 NAS 上跑定时任务，或者桌面端批量处理。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import load_settings
from .db import Database
from .models import Job, JobStatus
from .pipeline.runner import Pipeline, RunOptions
from .textproc import format_duration


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def _ctx(env_file: str | None):
    settings = load_settings(env_file)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    return settings, db, Pipeline(settings, db)


def _progress_printer():
    last = {"key": None}

    async def progress(stage: str, done: int, total: int, message: str) -> None:
        key = (stage, done // 10)
        if key == last["key"] and done != total:
            return
        last["key"] = key
        pct = f"{done / total * 100:5.1f}%" if total else "  ..."
        sys.stdout.write(f"\r  {stage:<6} {pct}  {done}/{total}  {message[:40]:<40}")
        sys.stdout.flush()

    return progress


def cmd_serve(args) -> int:
    import uvicorn

    settings, db, _ = _ctx(args.env)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    print(f"有声书工坊已启动： http://{settings.host}:{settings.port}")
    uvicorn.run(
        "audiobook_forge.web:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
    return 0


def cmd_import(args) -> int:
    settings, db, pipeline = _ctx(args.env)
    book = pipeline.import_file(Path(args.file))
    print(f"已导入：《{book.title}》")
    print(f"  ID      {book.id}")
    print(f"  格式    {book.source_format}  章节 {len(book.chapters)}  段落 {book.block_count}  字数 {book.char_count}")
    print(f"  预估时长 约 {format_duration(book.char_count / 5.2)}")
    return 0


def cmd_books(args) -> int:
    settings, db, _ = _ctx(args.env)
    books = db.list_books()
    if not books:
        print("书架是空的")
        return 0
    print(f"{'ID':<14}{'标题':<28}{'章节':>5}{'字数':>9}  分片")
    for item in books:
        stats = db.segment_stats(item["id"])
        done = stats.get("done", 0)
        total = stats.get("total", 0)
        print(
            f"{item['id']:<14}{(item['title'] or '')[:26]:<28}"
            f"{item['chapters']:>5}{item['chars']:>9}  {done}/{total}"
        )
    return 0


async def _run(args, action: str) -> int:
    settings, db, pipeline = _ctx(args.env)
    job = Job(book_id=args.book_id, stage=action)
    db.create_job(job)
    options = RunOptions(
        provider=args.provider,
        read_headings=not args.no_headings,
        make_m4b=not args.no_m4b,
        skip_llm=args.no_llm,
    )
    try:
        if action == "run":
            manifest = await pipeline.run_all(args.book_id, options, job, _progress_printer())
            print()
            print(f"✅ 完成：{manifest['mp3']}")
            if manifest.get("m4b"):
                print(f"   m4b：{manifest['m4b']}")
            print(f"   时长：{format_duration(manifest['duration_ms'] / 1000)}")
        elif action == "analyze":
            book, cast = await pipeline.analyze(args.book_id, options, _progress_printer())
            print()
            print(f"✅ 分析完成：{len(book.chapters)} 章，角色 {len(cast.assignments)} 个")
            for a in cast.assignments:
                print(f"   {a.speaker:<10} → {a.voice}")
        elif action == "synthesize":
            stats = await pipeline.synthesize(args.book_id, options, _progress_printer())
            print()
            print(f"✅ 合成完成：成功 {stats.ok}（缓存 {stats.cached}），失败 {stats.failed}")
            for seq, err in stats.failures[:10]:
                print(f"   ! {seq}: {err[:120]}")
        elif action == "assemble":
            manifest = pipeline.assemble_now(args.book_id, options)
            print(f"✅ 已导出：{manifest['mp3']}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 失败：{exc}", file=sys.stderr)
        return 1


def cmd_build(args) -> int:
    """一步到位：导入 → 分析 → 合成 → 导出。"""
    settings, db, pipeline = _ctx(args.env)
    book = pipeline.import_file(Path(args.file))
    print(f"已导入：《{book.title}》 {book.char_count} 字，约 {format_duration(book.char_count / 5.2)}")
    args.book_id = book.id
    return asyncio.run(_run(args, "run"))


def cmd_status(args) -> int:
    settings, db, _ = _ctx(args.env)
    print(f"数据目录   {settings.data_dir}")
    print(f"TTS 提供商 {settings.tts.provider}（并发 {settings.tts.concurrency}）")
    print(f"LLM        {settings.llm.model} {'可用' if settings.llm.usable else '未配置（将使用规则分拣）'}")

    from .tts.registry import PROVIDER_LABELS, create_tts

    for name in ("mock", "mimo", "xfyun"):
        try:
            tts = create_tts(settings, name)
            ok, message = tts.health()
        except Exception as exc:  # noqa: BLE001
            ok, message = False, str(exc)
        mark = "✅" if ok else "❌"
        print(f"  {mark} {PROVIDER_LABELS.get(name, name):<12} {message}")

    try:
        from .pipeline.audio import probe_duration_ms  # noqa: F401

        import shutil

        print(f"ffmpeg     {shutil.which(settings.ffmpeg) or '未找到'}")
    except Exception as exc:  # noqa: BLE001
        print(f"ffmpeg     异常：{exc}")
    return 0


def cmd_doctor(args) -> int:
    """体检：能不能跑起来。"""
    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "✅" if passed else "❌"
        print(f"  {mark} {name:<18} {detail}")
        ok = ok and passed

    import shutil

    py_ok = sys.version_info >= (3, 10)
    check("Python >= 3.10", py_ok, f"{sys.version.split()[0]}")
    check("ffmpeg", bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or "未安装")
    check("ffprobe", bool(shutil.which("ffprobe")), shutil.which("ffprobe") or "未安装")

    settings, db, _ = _ctx(args.env)
    check("数据目录可写", settings.data_dir.exists(), str(settings.data_dir))

    from .tts.registry import create_tts

    try:
        tts = create_tts(settings, settings.tts.provider)
        healthy, message = tts.health()
        check(f"TTS ({settings.tts.provider})", healthy, message)
    except Exception as exc:  # noqa: BLE001
        check(f"TTS ({settings.tts.provider})", False, str(exc))

    check("LLM（可选）", True, settings.llm.model if settings.llm.usable else "未配置，将使用规则分拣")
    print("\n全部通过，可以开工。" if ok else "\n有项目未通过，请先修复。")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abf",
        description="Audiobook Forge —— 把 EPUB / TXT / Markdown 变成有声书",
    )
    parser.add_argument("--env", help="指定 .env 文件路径", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动 Web 界面")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.set_defaults(func=cmd_serve)

    p_import = sub.add_parser("import", help="导入并解析一本书")
    p_import.add_argument("file")
    p_import.set_defaults(func=cmd_import)

    p_books = sub.add_parser("books", help="列出书架")
    p_books.set_defaults(func=cmd_books)

    p_analyze = sub.add_parser("analyze", help="分析文本（角色/对白/情绪）")
    p_analyze.add_argument("book_id")
    p_analyze.add_argument("--provider", choices=["mock", "mimo", "xfyun"])
    p_analyze.add_argument("--no-llm", action="store_true", help="跳过 LLM，只用规则")
    p_analyze.add_argument("--no-headings", action="store_true", help="不朗读章节标题")
    p_analyze.add_argument("--no-m4b", action="store_true")
    p_analyze.set_defaults(func=lambda a: asyncio.run(_run(a, "analyze")))

    p_synth = sub.add_parser("synthesize", help="合成音频（可反复跑，自动续传）")
    p_synth.add_argument("book_id")
    p_synth.add_argument("--provider", choices=["mock", "mimo", "xfyun"])
    p_synth.add_argument("--no-headings", action="store_true")
    p_synth.add_argument("--no-m4b", action="store_true")
    p_synth.add_argument("--no-llm", action="store_true")
    p_synth.set_defaults(func=lambda a: asyncio.run(_run(a, "synthesize")), no_llm=False)

    p_assemble = sub.add_parser("assemble", help="把已合成的分片拼接导出")
    p_assemble.add_argument("book_id")
    p_assemble.add_argument("--no-m4b", action="store_true")
    p_assemble.add_argument("--provider", default=None)
    p_assemble.add_argument("--no-headings", action="store_true")
    p_assemble.add_argument("--no-llm", action="store_true")
    p_assemble.set_defaults(func=lambda a: asyncio.run(_run(a, "assemble")))

    p_build = sub.add_parser("build", help="一步到位：解析 → 分析 → 合成 → 导出")
    p_build.add_argument("file")
    p_build.add_argument("--provider", choices=["mock", "mimo", "xfyun"])
    p_build.add_argument("--no-llm", action="store_true")
    p_build.add_argument("--no-headings", action="store_true")
    p_build.add_argument("--no-m4b", action="store_true")
    p_build.set_defaults(func=cmd_build)

    p_status = sub.add_parser("status", help="查看配置与提供商状态")
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="环境体检")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging("INFO")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
