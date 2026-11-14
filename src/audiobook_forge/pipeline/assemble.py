"""把合成好的分片拼成整本有声书，并生成章节标记。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import Settings
from ..models import Book, Segment, SegmentStatus
from . import audio as audio_utils

log = logging.getLogger(__name__)


def assemble(
    book: Book,
    segments: list[Segment],
    settings: Settings,
    out_dir: Path,
    *,
    make_m4b: bool = True,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    ready = [
        s for s in segments
        if s.status == SegmentStatus.done and s.audio_path and Path(s.audio_path).is_file()
    ]
    if not ready:
        raise ValueError("没有任何已合成的分片，先把音频合成跑完")

    ready.sort(key=lambda s: s.seq)

    # 每段后面跟的间隔：同章用小间隔，跨章用大间隔
    entries: list[tuple[Path, int]] = []
    chapters_meta: list[dict] = []
    durations: dict[int, int] = {}

    for i, seg in enumerate(ready):
        next_seg = ready[i + 1] if i + 1 < len(ready) else None
        if next_seg is None:
            gap = 0
        elif next_seg.chapter_index != seg.chapter_index:
            gap = settings.chapter_gap_ms
        else:
            gap = settings.block_gap_ms
        entries.append((Path(seg.audio_path), gap))

    # 逐段探测时长，累加成章节区间
    cursor = 0
    by_chapter: dict[int, dict] = {}
    for seg, (path, gap) in zip(ready, entries):
        duration = durations.get(seg.seq)
        if duration is None:
            duration = audio_utils.probe_duration_ms(path, settings.ffmpeg)
            durations[seg.seq] = duration
        info = by_chapter.setdefault(
            seg.chapter_index,
            {"index": seg.chapter_index, "title": seg.chapter_title, "start_ms": cursor, "end_ms": cursor},
        )
        info["end_ms"] = cursor + duration
        cursor += duration + gap

    chapters_meta = [by_chapter[k] for k in sorted(by_chapter)]

    slug = _safe_name(book.title)
    mp3_path = out_dir / f"{slug}.mp3"
    audio_utils.concat(
        entries,
        mp3_path,
        ffmpeg=settings.ffmpeg,
        sample_rate=settings.audio_sample_rate,
        bitrate=settings.audio_bitrate,
        gap_dir=out_dir / "_gaps",
    )

    total_ms = audio_utils.probe_duration_ms(mp3_path, settings.ffmpeg)

    m4b_path: Path | None = None
    if make_m4b:
        try:
            m4b_path = out_dir / f"{slug}.m4b"
            audio_utils.to_m4b(
                mp3_path,
                m4b_path,
                chapters_meta,
                title=book.title,
                author=book.author or "",
                ffmpeg=settings.ffmpeg,
                bitrate=settings.audio_bitrate,
            )
        except audio_utils.FFmpegError as exc:
            log.warning("m4b 生成失败，保留 mp3：%s", exc)
            m4b_path = None

    manifest = {
        "book_id": book.id,
        "title": book.title,
        "author": book.author,
        "mp3": str(mp3_path),
        "m4b": str(m4b_path) if m4b_path else None,
        "duration_ms": total_ms,
        "chapters": chapters_meta,
        "segment_count": len(ready),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _safe_name(text: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    cleaned = "".join("_" if ch in bad else ch for ch in text).strip()
    return (cleaned or "audiobook")[:80]
