"""把一段文本切成 TTS 友好的小段。

为什么要切：
1. 多数 TTS 接口单次请求有长度上限（讯飞 64K 字节、MiMo 也有上下文限制）；
2. 段小了才能断点续跑、并发合成、失败只重试一小段；
3. 段太长韵律会飘，太短又会让每个请求的固定开销占比过高。
"""

from __future__ import annotations

from ..models import Block

# 句末标点
_ENDERS = "。！？!?；;…"
# 句末标点后可能紧跟的收尾引号/括号
_CLOSERS = "”’』」）)]】》"
# 长句二次切分用的停顿标点
_SOFT = "，、,：:—"


def split_sentences(text: str) -> list[str]:
    """按句末标点切句，并把紧跟在标点后的收尾引号归入前一句。"""
    sentences: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)

    while i < n:
        ch = text[i]
        buf.append(ch)
        if ch in _ENDERS:
            j = i + 1
            while j < n and text[j] in _ENDERS:       # 连续的 ！！！ 或 …… 
                buf.append(text[j])
                j += 1
            while j < n and text[j] in _CLOSERS:      # “……。” 的收尾引号
                buf.append(text[j])
                j += 1
            sentence = "".join(buf).strip()
            if sentence:
                sentences.append(sentence)
            buf = []
            i = j
            continue
        i += 1

    tail = "".join(buf).strip()
    if tail:
        sentences.append(tail)
    return sentences


def _split_long(sentence: str, max_chars: int) -> list[str]:
    """超长句：先按逗号等停顿切，再不行就硬切。"""
    if len(sentence) <= max_chars:
        return [sentence]

    parts: list[str] = []
    buf = ""
    for ch in sentence:
        buf += ch
        if ch in _SOFT and len(buf) >= max_chars * 0.55:
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)

    result: list[str] = []
    for part in parts:
        if len(part) <= max_chars:
            result.append(part)
        else:
            result.extend(part[k : k + max_chars] for k in range(0, len(part), max_chars))
    return [p for p in result if p.strip()]


def to_segments(text: str, min_chars: int = 12, max_chars: int = 120) -> list[str]:
    """文本 → 若干段，每段长度尽量落在 [min_chars, max_chars]。"""
    sentences = split_sentences(text)
    if not sentences:
        return []

    chunks: list[str] = []
    buf = ""

    for sentence in sentences:
        if len(sentence) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_split_long(sentence, max_chars))
            continue

        if not buf:
            buf = sentence
        elif len(buf) + len(sentence) <= max_chars:
            buf += sentence
        else:
            chunks.append(buf)
            buf = sentence

        if len(buf) >= max_chars:
            chunks.append(buf)
            buf = ""

    if buf:
        chunks.append(buf)

    return _merge_tiny(chunks, min_chars, max_chars)


def _merge_tiny(chunks: list[str], min_chars: int, max_chars: int) -> list[str]:
    """把过短的尾巴（如单独一个 "好。"）并到邻居，避免频繁短请求。"""
    if len(chunks) <= 1:
        return chunks

    merged: list[str] = []
    for chunk in chunks:
        if (
            merged
            and len(chunk) < min_chars
            and len(merged[-1]) + len(chunk) <= max_chars
        ):
            merged[-1] = merged[-1] + chunk
        else:
            merged.append(chunk)

    # 末尾仍然极短就并入前一段
    if len(merged) >= 2 and len(merged[-1]) < min_chars // 2:
        tail = merged.pop()
        merged[-1] = merged[-1] + tail

    return merged


def blocks_to_texts(block: Block, min_chars: int, max_chars: int) -> list[str]:
    return to_segments(block.text, min_chars, max_chars)
