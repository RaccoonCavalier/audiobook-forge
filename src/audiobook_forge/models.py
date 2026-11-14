"""跨模块共享的数据模型。"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

BlockType = Literal["narration", "dialogue", "heading", "skip"]
Gender = Literal["male", "female", "child", "unknown"]


class Block(BaseModel):
    """朗读的最小语义单位。"""

    type: BlockType = "narration"
    speaker: str | None = None
    text: str
    # MiMo 支持任意自然语言风格描述，如 "温柔 语速略慢"；其它 provider 会做映射
    style: str | None = None
    voice: str | None = None


class Chapter(BaseModel):
    index: int
    title: str
    blocks: list[Block] = Field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(b.text) for b in self.blocks)


class Book(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str
    author: str | None = None
    language: str = "zh-CN"
    source_format: Literal["epub", "txt", "md"] = "txt"
    source_name: str = ""
    chapters: list[Chapter] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)

    @property
    def char_count(self) -> int:
        return sum(c.char_count for c in self.chapters)

    @property
    def block_count(self) -> int:
        return sum(len(c.blocks) for c in self.chapters)


class VoiceAssignment(BaseModel):
    speaker: str
    voice: str
    gender: Gender = "unknown"
    note: str = ""


class Cast(BaseModel):
    """角色 → 音色 的映射表。"""

    narrator_voice: str
    assignments: list[VoiceAssignment] = Field(default_factory=list)

    def voice_of(self, speaker: str | None, fallback: str) -> str:
        if not speaker:
            return self.narrator_voice or fallback
        for item in self.assignments:
            if item.speaker == speaker:
                return item.voice
        return fallback

    def gender_of(self, speaker: str | None) -> Gender:
        if not speaker:
            return "unknown"
        for item in self.assignments:
            if item.speaker == speaker:
                return item.gender
        return "unknown"


class SegmentStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    skipped = "skipped"


class Segment(BaseModel):
    """一个 TTS 请求单元，落库后可断点续跑。"""

    id: int | None = None
    book_id: str
    chapter_index: int
    chapter_title: str = ""
    seq: int
    speaker: str | None = None
    voice: str
    style: str | None = None
    text: str
    text_hash: str = ""
    status: SegmentStatus = SegmentStatus.pending
    audio_path: str | None = None
    duration_ms: int = 0
    attempts: int = 0
    error: str | None = None

    def model_post_init(self, __context) -> None:  # noqa: D105
        if not self.text_hash:
            self.text_hash = text_hash(self.text)


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    book_id: str
    status: JobStatus = JobStatus.pending
    stage: str = "queued"
    message: str = ""
    total: int = 0
    done: int = 0
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    options: dict = Field(default_factory=dict)

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.done / self.total)


def text_hash(text: str, extra: str = "") -> str:
    payload = f"{extra}\x00{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


_WS = re.compile(r"[ \t\u3000]+")


def normalize_for_hash(text: str) -> str:
    return _WS.sub(" ", text).strip()
