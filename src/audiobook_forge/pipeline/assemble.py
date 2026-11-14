"""把合成好的分片拼成整本有声书，并生成章节标记。"""

from __future__ import annotations

import json
import logging
import os
import re
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

    # 按章分组（分片本身是按 seq 排好序的）
    groups: list[tuple[int, str, list[Segment]]] = []
    for seg in ready:
        if groups and groups[-1][0] == seg.chapter_index:
            groups[-1][2].append(seg)
        else:
            groups.append((seg.chapter_index, seg.chapter_title, [seg]))

    def chapter_entries(segs: list[Segment]) -> list[tuple[Path, int]]:
        return [
            (Path(s.audio_path), settings.block_gap_ms if i < len(segs) - 1 else 0)
            for i, s in enumerate(segs)
        ]

    # 整本：同章小间隔，跨章大间隔，最后一段不留尾巴
    entries: list[tuple[Path, int]] = []
    for gi, (_idx, _title, segs) in enumerate(groups):
        for i, seg in enumerate(segs):
            if i < len(segs) - 1:
                gap = settings.block_gap_ms
            elif gi + 1 < len(groups):
                gap = settings.chapter_gap_ms
            else:
                gap = 0
            entries.append((Path(seg.audio_path), gap))

    # 逐段探测时长，累加成章节区间
    cursor = 0
    by_chapter: dict[int, dict] = {}
    durations: dict[int, int] = {}
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
    # 先写临时文件再原子替换：中途取消或崩溃都不会留下半截损坏的成品
    mp3_tmp = out_dir / f".{slug}.part.mp3"
    audio_utils.concat(
        entries,
        mp3_tmp,
        ffmpeg=settings.ffmpeg,
        sample_rate=settings.audio_sample_rate,
        bitrate=settings.audio_bitrate,
        gap_dir=settings.cache_dir / "gaps",
    )
    os.replace(mp3_tmp, mp3_path)

    total_ms = audio_utils.probe_duration_ms(mp3_path, settings.ffmpeg)

    # 每章一个独立音频：书名-第01章-章节名.mp3
    chapter_files: list[dict] = []
    for idx, title, segs in groups:
        suffix = _chapter_suffix(idx, title)
        name = f"{slug}-{suffix}.mp3"
        target = out_dir / name
        part = out_dir / f".{slug}-{suffix}.part.mp3"
        try:
            audio_utils.concat(
                chapter_entries(segs),
                part,
                ffmpeg=settings.ffmpeg,
                sample_rate=settings.audio_sample_rate,
                bitrate=settings.audio_bitrate,
                gap_dir=settings.cache_dir / "gaps",
            )
            os.replace(part, target)
            chapter_files.append(
                {
                    "index": idx,
                    "title": title,
                    "file": str(target),
                    "duration_ms": audio_utils.probe_duration_ms(target, settings.ffmpeg),
                }
            )
        except audio_utils.FFmpegError as exc:
            log.warning("第 %s 章单独导出失败：%s", idx + 1, exc)
            part.unlink(missing_ok=True)

    m4b_path: Path | None = None
    if make_m4b:
        try:
            m4b_path = out_dir / f"{slug}.m4b"
            m4b_tmp = out_dir / f".{slug}.part.m4b"
            audio_utils.to_m4b(
                mp3_path,
                m4b_tmp,
                chapters_meta,
                title=book.title,
                author=book.author or "",
                ffmpeg=settings.ffmpeg,
                bitrate=settings.audio_bitrate,
            )
            os.replace(m4b_tmp, m4b_path)
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
        "chapter_files": chapter_files,
        "output_dir": str(out_dir),
        "segment_count": len(ready),
        # 只合成了部分章节时为 True
        "partial": len(ready) < len(segments),
        "total_segments": len(segments),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _safe_name(text: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    cleaned = "".join("_" if ch in bad else ch for ch in text).strip()
    return (cleaned.strip(" .") or "audiobook")[:80]


_CHAPTER_NUM_PREFIX = re.compile(
    r"^\s*第\s*[0-9零〇一二三四五六七八九十百千两]+\s*[章回节卷部篇集][\s:：、.．\-—]*"
)
_SPECIAL_TITLES = ("前言", "序章", "序言", "楔子", "引子", "尾声", "后记", "终章", "结局", "正文")


def _chapter_suffix(index: int, title: str) -> str:
    """章节文件名后缀：'第01章-雨夜'；前言/番外这类不加章号。"""
    cleaned = _CHAPTER_NUM_PREFIX.sub("", title or "").strip()
    cleaned = _safe_name(cleaned or (title or ""))
    if (
        cleaned in _SPECIAL_TITLES
        or cleaned.startswith("番外")
        or cleaned.startswith("序")
    ):
        return cleaned
    return f"第{index + 1:02d}章-{cleaned}"
