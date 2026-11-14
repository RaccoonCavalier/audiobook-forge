"""把分析后的 Book 拆成可执行的 TTS 分段计划。"""

from __future__ import annotations

from ..config import Settings
from ..models import Book, Cast, Segment
from ..textproc import to_segments


def build_segments(
    book: Book,
    cast: Cast,
    settings: Settings,
    *,
    read_headings: bool = True,
    single_voice: bool = False,
    forced_voice: str | None = None,
) -> list[Segment]:
    """把书拆成分段。

    single_voice=True 时所有内容（旁白、对白、标题）统一用一个音色，
    适合轻小说这类"对话没有明确说话人标记、自动判角色容易判错"的文本。
    """
    narrator = cast.narrator_voice
    unified = (forced_voice or narrator) if single_voice else narrator
    min_chars = settings.segment_min_chars
    max_chars = settings.segment_max_chars
    segments: list[Segment] = []
    seq = 0

    for chapter in book.chapters:
        for block in chapter.blocks:
            if block.type == "skip":
                continue
            if block.type == "heading" and not read_headings:
                continue

            text = block.text.strip()
            if not text:
                continue

            if single_voice:
                voice = unified
            elif block.type == "dialogue":
                voice = block.voice or cast.voice_of(block.speaker, narrator)
            else:
                voice = block.voice or narrator

            style = block.style
            if block.type == "heading" and not style:
                style = "平稳 清晰 稍慢"

            pieces = to_segments(text, min_chars, max_chars)
            for piece in pieces:
                seq += 1
                segments.append(
                    Segment(
                        book_id=book.id,
                        chapter_index=chapter.index,
                        chapter_title=chapter.title,
                        seq=seq,
                        speaker=block.speaker if block.type == "dialogue" else None,
                        voice=voice,
                        style=style,
                        text=piece,
                    )
                )

    return segments


def estimate_chars(segments: list[Segment]) -> int:
    return sum(len(s.text) for s in segments)
