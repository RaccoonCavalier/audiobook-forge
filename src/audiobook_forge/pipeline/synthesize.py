"""并发合成：带缓存、重试、失败隔离。

每一段独立成败 —— 一段挂了不影响其它段，最后统一重跑失败段即可。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from ..config import Settings
from ..models import Segment, SegmentStatus
from ..tts.base import BaseTTS, TTSError, TTSRequest
from .audio import transcode_bytes

log = logging.getLogger(__name__)


@dataclass
class SynthStats:
    ok: int = 0
    failed: int = 0
    cached: int = 0
    total: int = 0
    elapsed: float = 0.0
    failures: list[tuple[int, str]] = field(default_factory=list)

    @property
    def audio_ms(self) -> int:
        return 0


SegmentCallback = Callable[[Segment, SegmentStatus, str | None], Awaitable[None] | None]


def cache_key(provider: str, voice: str, style: str | None, text: str) -> str:
    raw = f"{provider}\x00{voice}\x00{style or ''}\x00{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


class Synthesizer:
    def __init__(
        self,
        tts: BaseTTS,
        settings: Settings,
        audio_dir: Path,
        cache_dir: Path,
    ) -> None:
        self.tts = tts
        self.settings = settings
        self.audio_dir = audio_dir
        self.cache_dir = cache_dir
        self.semaphore = asyncio.Semaphore(max(1, settings.tts.concurrency))
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def audio_path(self, segment: Segment) -> Path:
        return self.audio_dir / f"{segment.seq:05d}.mp3"

    async def run(
        self,
        segments: list[Segment],
        on_progress: SegmentCallback | None = None,
    ) -> SynthStats:
        stats = SynthStats(total=len(segments))
        started = time.monotonic()

        await asyncio.gather(
            *(self._one(seg, stats, on_progress) for seg in segments)
        )

        stats.elapsed = time.monotonic() - started
        return stats

    async def _one(
        self,
        segment: Segment,
        stats: SynthStats,
        on_progress: SegmentCallback | None,
    ) -> None:
        async with self.semaphore:
            target = self.audio_path(segment)
            key = cache_key(self.tts.name, segment.voice, segment.style, segment.text)
            cached = self.cache_dir / f"{key}.mp3"

            if cached.is_file() and cached.stat().st_size > 0:
                target.write_bytes(cached.read_bytes())
                stats.ok += 1
                stats.cached += 1
                segment.status = SegmentStatus.done
                segment.audio_path = str(target)
                await _notify(on_progress, segment, SegmentStatus.done, None)
                return

            if target.is_file() and target.stat().st_size > 0:
                # 上次已经合成成功但状态没落库，直接认
                stats.ok += 1
                stats.cached += 1
                segment.status = SegmentStatus.done
                segment.audio_path = str(target)
                await _notify(on_progress, segment, SegmentStatus.done, None)
                return

            error: str | None = None
            for attempt in range(1, self.settings.tts.retries + 1):
                try:
                    result = await self.tts.synthesize(
                        TTSRequest(
                            text=segment.text,
                            voice=segment.voice,
                            style=segment.style,
                        )
                    )
                    suffix = "." + result.format.lower()
                    # ffmpeg 是同步 subprocess，必须丢到线程池，
                    # 否则会把整个事件循环卡住（实测单次可达 1 秒以上）
                    await asyncio.to_thread(
                        transcode_bytes,
                        result.audio,
                        suffix,
                        target,
                        ffmpeg=self.settings.ffmpeg,
                        sample_rate=self.settings.audio_sample_rate,
                        bitrate=self.settings.audio_bitrate,
                    )
                    cached.write_bytes(target.read_bytes())
                    stats.ok += 1
                    segment.status = SegmentStatus.done
                    segment.audio_path = str(target)
                    segment.attempts += 1
                    await _notify(on_progress, segment, SegmentStatus.done, None)
                    return
                except TTSError as exc:
                    error = str(exc)
                    if not exc.retryable:
                        break
                    await asyncio.sleep(min(8.0, 1.0 * attempt))
                except Exception as exc:  # noqa: BLE001 - 单段失败不能拖垮整批
                    error = f"{type(exc).__name__}: {exc}"
                    await asyncio.sleep(min(8.0, 1.0 * attempt))

            stats.failed += 1
            stats.failures.append((segment.seq, error or "未知错误"))
            segment.status = SegmentStatus.failed
            segment.error = error
            segment.attempts += 1
            await _notify(on_progress, segment, SegmentStatus.failed, error)


async def _notify(
    callback: SegmentCallback | None,
    segment: Segment,
    status: SegmentStatus,
    error: str | None,
) -> None:
    if callback is None:
        return
    result = callback(segment, status, error)
    if asyncio.iscoroutine(result):
        await result
