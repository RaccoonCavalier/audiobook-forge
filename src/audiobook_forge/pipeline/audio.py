"""ffmpeg 封装：转码统一、按间隔拼接、加章节生成 m4b。"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    pass


def _resolve_ffmpeg(ffmpeg: str) -> str:
    path = shutil.which(ffmpeg)
    if not path:
        raise FFmpegError(
            f"找不到 ffmpeg（{ffmpeg}）。请安装并加入 PATH，或用 ABF_FFMPEG 指定完整路径。"
        )
    return path


def ffprobe_path(ffmpeg: str) -> str:
    probe = Path(_resolve_ffmpeg(ffmpeg)).with_name("ffprobe.exe")
    if probe.is_file():
        return str(probe)
    found = shutil.which("ffprobe")
    if not found:
        raise FFmpegError("找不到 ffprobe，请确认与 ffmpeg 同目录")
    return found


def run(args: list[str], *, timeout: float = 300.0) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"命令失败：{' '.join(args[:3])}...\n{proc.stderr[-800:]}")
    return proc


def transcode(
    source: Path,
    target: Path,
    *,
    ffmpeg: str = "ffmpeg",
    sample_rate: int = 44100,
    bitrate: str = "64k",
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            _resolve_ffmpeg(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-ac", "1", "-ar", str(sample_rate),
            "-c:a", "libmp3lame", "-b:a", bitrate,
            str(target),
        ]
    )


def transcode_bytes(
    data: bytes,
    source_suffix: str,
    target: Path,
    *,
    ffmpeg: str = "ffmpeg",
    sample_rate: int = 44100,
    bitrate: str = "64k",
) -> None:
    with tempfile.NamedTemporaryFile(suffix=source_suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        transcode(
            tmp_path, target,
            ffmpeg=ffmpeg, sample_rate=sample_rate, bitrate=bitrate,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def probe_duration_ms(path: Path, ffmpeg: str = "ffmpeg") -> int:
    probe = ffprobe_path(ffmpeg)
    proc = run(
        [
            probe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        timeout=60,
    )
    try:
        return int(float(proc.stdout.strip()) * 1000)
    except (ValueError, AttributeError):
        return 0


def make_silence(
    duration_ms: int,
    target: Path,
    *,
    ffmpeg: str = "ffmpeg",
    sample_rate: int = 44100,
    bitrate: str = "64k",
) -> Path:
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            _resolve_ffmpeg(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"anullsrc=r={sample_rate}:cl=mono",
            "-t", f"{duration_ms / 1000:.3f}",
            "-c:a", "libmp3lame", "-b:a", bitrate,
            str(target),
        ]
    )
    return target


def concat(
    entries: list[tuple[Path, int]],
    target: Path,
    *,
    ffmpeg: str = "ffmpeg",
    sample_rate: int = 44100,
    bitrate: str = "64k",
    gap_dir: Path | None = None,
) -> dict:
    """entries: [(音频文件, 其后的间隔毫秒)]。返回章节起止信息。"""
    if not entries:
        raise FFmpegError("没有可拼接的音频")

    gap_dir = gap_dir or target.parent / "_gaps"
    list_file = target.parent / "_concat.txt"
    target.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for path, gap_ms in entries:
        if not path.is_file():
            raise FFmpegError(f"缺少分片：{path}")
        lines.append(f"file '{path.as_posix()}'")
        if gap_ms > 0:
            silence = make_silence(
                gap_ms,
                gap_dir / f"sil_{gap_ms}.mp3",
                ffmpeg=ffmpeg, sample_rate=sample_rate, bitrate=bitrate,
            )
            lines.append(f"file '{silence.as_posix()}'")

    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    run(
        [
            _resolve_ffmpeg(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:a", "libmp3lame", "-b:a", bitrate,
            str(target),
        ],
        timeout=1800,
    )
    list_file.unlink(missing_ok=True)
    return {"duration_ms": probe_duration_ms(target, ffmpeg)}


def to_m4b(
    source_mp3: Path,
    target: Path,
    chapters: list[dict],
    *,
    title: str = "",
    author: str = "",
    ffmpeg: str = "ffmpeg",
    bitrate: str = "64k",
) -> None:
    """转成带章节标记的 m4b（Apple Books / 播客客户端友好）。"""
    meta_file = target.parent / "_meta.txt"
    lines = [";FFMETADATA1"]
    if title:
        lines.append(f"title={_escape_meta(title)}")
    if author:
        lines.append(f"artist={_escape_meta(author)}")
    for chapter in chapters:
        start = int(chapter.get("start_ms", 0))
        end = int(chapter.get("end_ms", start))
        if end <= start:
            continue
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start}",
            f"END={end}",
            f"title={_escape_meta(chapter.get('title', ''))}",
        ]
    meta_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    run(
        [
            _resolve_ffmpeg(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source_mp3),
            "-i", str(meta_file),
            "-map_metadata", "1",
            "-c:a", "aac", "-b:a", bitrate,
            "-f", "mp4",
            str(target),
        ],
        timeout=1800,
    )
    meta_file.unlink(missing_ok=True)


def _escape_meta(text: str) -> str:
    return text.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace(
        "#", "\\#"
    ).replace("\n", " ")
