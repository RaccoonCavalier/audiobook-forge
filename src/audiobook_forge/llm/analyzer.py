"""分拣编排：选角（cast）→ 逐章标注（type/speaker/style）→ 无 LLM 时的规则兜底。"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..config import Settings
from ..models import Block, Book, Cast, Chapter, VoiceAssignment
from ..textproc import clean_block_text, is_speakable
from .base import BaseLLM, LLMError
from .prompts import SYSTEM_ANNOTATE, SYSTEM_CAST, USER_ANNOTATE, USER_CAST

log = logging.getLogger(__name__)

# 每批送给模型的字数上限；太小会让模型丢上下文，太大容易超上下文窗口
BATCH_CHARS = 2600
SAMPLE_CHARS = 1400

# provider 的可用音色池（按性别轮转分配）
VOICE_POOL: dict[str, dict[str, list[str]]] = {
    "mimo": {
        "female": ["冰糖", "茉莉", "Mia", "Chloe"],
        "male": ["苏打", "白桦", "Milo", "Dean"],
        "child": ["茉莉", "Mia"],
        "unknown": ["冰糖", "苏打", "茉莉", "白桦"],
    },
    "xfyun": {
        "female": ["x6_lingxiaoxuan_pro", "x6_pangbainv1_pro", "x6_lingxiaoyue_pro", "x6_lingyuyan_pro"],
        "male": ["x6_lingfeiyi_pro", "x6_pangbainan1_pro", "x6_lingfeizhe_pro", "x6_lingfeihan_pro"],
        "child": ["x6_lingyouyou_pro", "x6_dongmanshaonv_pro"],
        "unknown": ["x6_lingxiaoxuan_pro", "x6_lingfeiyi_pro"],
    },
    "mock": {
        "female": ["mock-female-a", "mock-female-b"],
        "male": ["mock-male-a", "mock-male-b"],
        "child": ["mock-child"],
        "unknown": ["mock-female-a"],
    },
}

ProgressCallback = Callable[[str, int, int], Awaitable[None] | None]


@dataclass
class AnalysisStats:
    chapters: int = 0
    blocks: int = 0
    dialogues: int = 0
    llm_calls: int = 0
    llm_tokens: int = 0
    fallback_blocks: int = 0


class Analyzer:
    def __init__(self, llm: BaseLLM | None, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings
        self.stats = AnalysisStats()
        self._semaphore = asyncio.Semaphore(max(1, settings.llm.concurrency))
        self._cast_assignments: list[VoiceAssignment] = []

    # ------------------------------------------------------------- 选角

    async def discover_cast(self, book: Book) -> Cast:
        provider = self.settings.tts.provider
        narrator = self._narrator_voice(provider)

        if not self.llm:
            return self._rule_cast(book, provider, narrator)

        samples = self._build_samples(book)
        if not samples.strip():
            return self._rule_cast(book, provider, narrator)

        user = USER_CAST.format(
            title=book.title,
            author=book.author or "未知",
            samples=samples,
        )
        try:
            response = await self.llm.chat_json(SYSTEM_CAST, user)
            self.stats.llm_calls += 1
            self.stats.llm_tokens += response.usage.total
            characters = response.data.get("characters") or []
        except LLMError as exc:
            log.warning("选角失败，退回规则分配：%s", exc)
            return self._rule_cast(book, provider, narrator)

        assignments: list[VoiceAssignment] = []
        for index, item in enumerate(characters):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name or name in {"旁白", "narrator"}:
                continue
            gender = str(item.get("gender", "unknown")).lower()
            if gender not in {"male", "female", "child", "unknown"}:
                gender = "unknown"
            voice = self._pick_voice(provider, gender, index)
            hint = str(item.get("voice_hint", "")).strip()
            assignments.append(
                VoiceAssignment(
                    speaker=name,
                    voice=voice,
                    gender=gender,  # type: ignore[arg-type]
                    note=hint,
                )
            )

        if not assignments:
            return self._rule_cast(book, provider, narrator)

        return Cast(narrator_voice=narrator, assignments=assignments)

    # ------------------------------------------------------------- 标注

    async def analyze_book(
        self,
        book: Book,
        cast: Cast,
        progress: ProgressCallback | None = None,
    ) -> Book:
        provider = self.settings.tts.provider
        narrator_voice = cast.narrator_voice or self._narrator_voice(provider)
        cast_names = [a.speaker for a in cast.assignments]
        self._cast_assignments = list(cast.assignments)

        total = len(book.chapters)
        tasks = [
            self._analyze_chapter(chapter, book.title, cast_names, narrator_voice)
            for chapter in book.chapters
        ]

        results: list[Chapter] = []
        for done, coro in enumerate(asyncio.as_completed(tasks), start=1):
            chapter = await coro
            results.append(chapter)
            if progress:
                maybe = progress("标注", done, total)
                if asyncio.iscoroutine(maybe):
                    await maybe

        results.sort(key=lambda c: c.index)
        for chapter in results:
            for block in chapter.blocks:
                if block.type == "dialogue":
                    self.stats.dialogues += 1
        self.stats.chapters = len(results)
        self.stats.blocks = sum(len(c.blocks) for c in results)
        return book.model_copy(update={"chapters": results})

    async def _analyze_chapter(
        self,
        chapter: Chapter,
        book_title: str,
        cast_names: list[str],
        narrator_voice: str,
    ) -> Chapter:
        blocks = [b for b in chapter.blocks]
        if not blocks:
            return chapter

        if not self.llm:
            annotated = self._rule_annotate(blocks, cast_names)
        else:
            annotated = await self._llm_annotate(blocks, book_title, chapter.title, cast_names)

        out: list[Block] = []
        cast_map = {a.speaker: a.voice for a in self._cast_assignments}
        for block, (btype, speaker, style) in zip(blocks, annotated):
            text = block.text
            if btype != "skip":
                cleaned = clean_block_text(text)
                if cleaned is None:
                    btype = "skip"
                    text = text
                else:
                    text = cleaned
            voice = None
            if btype == "dialogue" and speaker:
                voice = cast_map.get(speaker) or narrator_voice
            out.append(
                Block(
                    type=btype,  # type: ignore[arg-type]
                    speaker=speaker if btype == "dialogue" else None,
                    text=text,
                    style=style,
                    voice=voice,
                )
            )
        return Chapter(index=chapter.index, title=chapter.title, blocks=out)

    async def _llm_annotate(
        self,
        blocks: list[Block],
        book_title: str,
        chapter_title: str,
        cast_names: list[str],
    ) -> list[tuple[str, str | None, str | None]]:
        batches = _batch_indices(blocks)
        results: list[tuple[str, str | None, str | None]] = [
            ("narration", None, None) for _ in blocks
        ]

        async def run_batch(indices: list[int]) -> None:
            listing = "\n".join(f"{i}: {blocks[i].text}" for i in indices)
            user = USER_ANNOTATE.format(
                title=book_title,
                chapter_title=chapter_title,
                cast="、".join(cast_names) if cast_names else "（暂无角色表）",
                blocks=listing,
            )
            async with self._semaphore:
                try:
                    response = await self.llm.chat_json(SYSTEM_ANNOTATE, user)  # type: ignore[union-attr]
                except LLMError as exc:
                    log.warning("章节《%s》标注失败，该批退回规则：%s", chapter_title, exc)
                    self.stats.fallback_blocks += len(indices)
                    for i in indices:
                        results[i] = _rule_one_block(blocks[i], cast_names)
                    return

            self.stats.llm_calls += 1
            self.stats.llm_tokens += response.usage.total
            items = response.data.get("blocks") or []
            seen: set[int] = set()
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("i"))
                except (TypeError, ValueError):
                    continue
                if idx not in indices or idx in seen:
                    continue
                seen.add(idx)
                results[idx] = _sanitize_annotation(item, cast_names)

            # 模型漏掉的编号，用规则补齐，保证长度对齐
            for i in indices:
                if i not in seen:
                    self.stats.fallback_blocks += 1
                    results[i] = _rule_one_block(blocks[i], cast_names)

        await asyncio.gather(*(run_batch(b) for b in batches))
        return results

    # ------------------------------------------------------------- 规则兜底

    def _rule_annotate(
        self, blocks: list[Block], cast_names: list[str]
    ) -> list[tuple[str, str | None, str | None]]:
        return [_rule_one_block(b, cast_names) for b in blocks]

    def _rule_cast(self, book: Book, provider: str, narrator: str) -> Cast:
        """没有 LLM 时：靠 "X说" 正则粗抽角色。"""
        found: dict[str, str] = {}
        for chapter in book.chapters:
            for block in chapter.blocks:
                # 说话人可能出现在句首（"张三说道："）也可能在引号之后（"……"张三说道。）
                for match in _SPEECH_ANY.finditer(block.text):
                    found.setdefault(match.group(1), "unknown")
                # 只有动作没有言语动词的，如 "苏晚点了点头：……"
                gesture = _GESTURE_SPEAKER.match(block.text)
                if gesture:
                    found.setdefault(gesture.group(1), "unknown")
        assignments = [
            VoiceAssignment(
                speaker=name,
                voice=self._pick_voice(provider, "unknown", i),
                gender="unknown",
            )
            for i, name in enumerate(list(found)[:12])
        ]
        return Cast(narrator_voice=narrator, assignments=assignments)

    def _narrator_voice(self, provider: str) -> str:
        if provider == "mimo":
            return self.settings.tts.mimo.narrator_voice
        if provider == "xfyun":
            return self.settings.tts.xfyun.narrator_voice
        return "mock-narrator"

    def _pick_voice(self, provider: str, gender: str, index: int) -> str:
        pool = VOICE_POOL.get(provider, VOICE_POOL["mock"])
        if gender in {"male", "female", "child"}:
            voices = pool[gender]
        else:
            # 性别未知时把男女声混着轮转，保证不同角色听起来不一样
            voices = pool["female"] + pool["male"]
        return voices[index % len(voices)]

    def _build_samples(self, book: Book) -> str:
        parts: list[str] = []
        budget = SAMPLE_CHARS * 3
        for chapter in book.chapters:
            if budget <= 0:
                break
            text = "\n".join(b.text for b in chapter.blocks)[:SAMPLE_CHARS]
            if text.strip():
                parts.append(f"【{chapter.title}】\n{text}")
                budget -= len(text)
        return "\n\n".join(parts)


_SPEECH_VERBS = r"(?:说道|笑道|问道|答道|低声说|沉声道|说|道|问|答|喊|叫)"
# 说话人必须出现在句首或句末标点/引号之后，且名字用非贪婪匹配，
# 否则 "沈砚说道" 会被贪婪地拆成 "沈砚说" + "道"。
_SPEECH_TAG = re.compile(rf"^([\u4e00-\u9fff]{{2,4}}?){_SPEECH_VERBS}")
_SPEECH_ANY = re.compile(
    rf"(?:^|(?<=[。！？…\u201d\u2019\u300d\u300f]))([\u4e00-\u9fff]{{2,4}}?){_SPEECH_VERBS}"
)
# "苏晚点了点头：“……”" 这种没有言语动词的引导方式
_GESTURE_SPEAKER = re.compile(
    r"^([\u4e00-\u9fff]{2,4}?)(?:点了点头|摇了摇头|笑了笑|叹了口气|愣了一下|皱了皱眉|冷笑|苦笑)(?:[，,：:]|$)"
)
_QUOTE_OPEN = ("“", "「", "『", "\"")
_VALID_TYPES = {"narration", "dialogue", "heading", "skip"}


def _rule_one_block(block: Block, cast_names: list[str]) -> tuple[str, str | None, str | None]:
    """单块的规则判断：有引号 → 对白；标题块 → heading；否则旁白。"""
    text = block.text.strip()
    if block.type == "heading":
        return "heading", None, None
    if not text or not is_speakable(text):
        return "skip", None, None

    starts_quote = text.startswith(_QUOTE_OPEN)
    has_quote = any(q in text for q in _QUOTE_OPEN)
    if not (starts_quote or has_quote):
        return "narration", None, None

    speaker: str | None = None
    for match in _SPEECH_ANY.finditer(text):
        if match.group(1) in cast_names:
            speaker = match.group(1)
            break
    if speaker is None:
        # 没有"说/道"时，靠 "苏晚点了点头：“……”" 这类动作引导语判断
        gesture = _GESTURE_SPEAKER.match(text)
        if gesture and gesture.group(1) in cast_names:
            speaker = gesture.group(1)
    if speaker is None:
        for name in cast_names:
            if text.startswith(name + "："):
                speaker = name
                break
    return "dialogue", speaker or "未知角色", None


def _sanitize_annotation(
    item: dict, cast_names: list[str]
) -> tuple[str, str | None, str | None]:
    btype = str(item.get("type", "narration")).strip().lower()
    if btype not in _VALID_TYPES:
        btype = "narration"

    speaker = item.get("speaker")
    speaker = str(speaker).strip() if speaker else None
    if btype != "dialogue":
        speaker = None
    elif speaker and cast_names and speaker not in cast_names:
        speaker = "未知角色"

    style = item.get("style")
    style = str(style).strip() if style else None
    if style and len(style) > 24:
        style = style[:24]
    if style in {"正常", "平静", "普通", "normal"}:
        style = None
    return btype, speaker, style


def _batch_indices(blocks: list[Block]) -> list[list[int]]:
    """按字数预算把块编号分批，尽量保证批次不切断上下文。"""
    batches: list[list[int]] = []
    current: list[int] = []
    size = 0
    for i, block in enumerate(blocks):
        length = len(block.text)
        if current and size + length > BATCH_CHARS:
            batches.append(current)
            current, size = [], 0
        current.append(i)
        size += length
    if current:
        batches.append(current)
    return batches
