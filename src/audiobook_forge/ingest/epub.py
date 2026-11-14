"""EPUB：读 OPF spine 定顺序，按 XHTML 文档切章，标题取自 NCX/nav 或文档内标题。"""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from urllib.parse import unquote

from lxml import etree, html

from ..models import Block, Book, Chapter

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xhtml": "http://www.w3.org/1999/xhtml",
}

_TEXT_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "dd", "dt")
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP_TAGS = {"script", "style", "nav", "head", "title"}


def parse_epub(path: Path) -> Book:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        opf_path = _find_opf(zf, names)
        if not opf_path:
            raise ValueError("EPUB 缺少 OPF：不是合法的 epub 文件")

        opf = etree.fromstring(zf.read(opf_path))
        base = posixpath.dirname(opf_path)

        title = _first_text(opf, ".//dc:title") or path.stem
        author = _first_text(opf, ".//dc:creator")
        language = _first_text(opf, ".//dc:language") or "zh-CN"

        manifest: dict[str, tuple[str, str]] = {}
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            manifest[item.get("id")] = (
                item.get("href", ""),
                item.get("media-type", ""),
            )

        spine_ids = [ref.get("idref") for ref in opf.findall(".//opf:spine/opf:itemref", NS)]
        spine_ids = [i for i in spine_ids if i in manifest]

        labels = _read_ncx_labels(zf, opf, base, manifest)
        labels.update(_read_nav_labels(zf, opf, base, manifest))

        chapters: list[Chapter] = []
        for idref in spine_ids:
            href, media_type = manifest[idref]
            if "html" not in media_type and not href.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            full = _resolve(base, href)
            if full not in names:
                continue
            try:
                data = zf.read(full)
            except KeyError:
                continue

            nodes = _extract_nodes(data)
            if not nodes:
                continue

            doc_title = labels.get(_strip_fragment(href)) or labels.get(full) or ""
            chapters.extend(_nodes_to_chapters(nodes, doc_title))

    chapters = _renumber([c for c in chapters if c.blocks])
    if not chapters:
        raise ValueError("EPUB 中没有提取到正文")

    return Book(
        title=title,
        author=author,
        language=language,
        source_format="epub",
        source_name=path.name,
        chapters=chapters,
    )


def _find_opf(zf: zipfile.ZipFile, names: set[str]) -> str | None:
    if "META-INF/container.xml" in names:
        try:
            container = etree.fromstring(zf.read("META-INF/container.xml"))
            node = container.find(".//c:rootfile", NS)
            if node is not None and node.get("full-path"):
                return node.get("full-path")
        except etree.XMLSyntaxError:
            pass
    for name in names:
        if name.lower().endswith(".opf"):
            return name
    return None


def _first_text(node, xpath: str) -> str | None:
    found = node.find(xpath, NS)
    if found is None or not found.text:
        return None
    return found.text.strip() or None


def _resolve(base: str, href: str) -> str:
    target = unquote(href)
    return posixpath.normpath(posixpath.join(base, target)) if base else posixpath.normpath(target)


def _strip_fragment(href: str) -> str:
    return unquote(href.split("#", 1)[0])


def _read_ncx_labels(zf, opf, base: str, manifest: dict) -> dict[str, str]:
    labels: dict[str, str] = {}
    ncx_href = ""
    for href, media_type in manifest.values():
        if media_type == "application/x-dtbncx+xml" or href.lower().endswith(".ncx"):
            ncx_href = href
            break
    if not ncx_href:
        return labels
    full = _resolve(base, ncx_href)
    try:
        ncx = etree.fromstring(zf.read(full))
    except (KeyError, etree.XMLSyntaxError):
        return labels
    for nav_point in ncx.findall(".//ncx:navPoint", NS):
        label_node = nav_point.find("ncx:navLabel/ncx:text", NS)
        content = nav_point.find("ncx:content", NS)
        if label_node is None or content is None:
            continue
        src = content.get("src", "")
        text = (label_node.text or "").strip()
        if src and text:
            labels.setdefault(_strip_fragment(src), text)
            labels.setdefault(_resolve(base, src), text)
    return labels


def _read_nav_labels(zf, opf, base: str, manifest: dict) -> dict[str, str]:
    labels: dict[str, str] = {}
    nav_href = ""
    for href, media_type in manifest.values():
        if "nav" in (media_type or "") or href.lower().endswith("nav.xhtml"):
            nav_href = href
            break
    if not nav_href:
        return labels
    full = _resolve(base, nav_href)
    try:
        doc = html.fromstring(zf.read(full))
    except (KeyError, etree.ParserError):
        return labels
    for a in doc.xpath("//nav//a[@href]"):
        text = " ".join((a.text_content() or "").split())
        href = a.get("href", "")
        if text and href:
            labels.setdefault(_strip_fragment(href), text)
    return labels


def _extract_nodes(data: bytes) -> list[tuple[str, str]]:
    try:
        doc = html.fromstring(data)
    except etree.ParserError:
        return []

    for bad in doc.xpath("//script | //style | //head"):
        bad.getparent().remove(bad)

    nodes: list[tuple[str, str]] = []
    body = doc.find(".//body")
    root = body if body is not None else doc

    for el in root.iter():
        tag = el.tag
        if not isinstance(tag, str):
            continue
        tag = tag.split("}")[-1].lower()
        if tag not in _TEXT_TAGS:
            continue
        text = " ".join((el.text_content() or "").split())
        if not text:
            continue
        nodes.append((tag, text))
    return nodes


def _nodes_to_chapters(nodes: list[tuple[str, str]], doc_title: str) -> list[Chapter]:
    total = sum(len(t) for _, t in nodes)
    heading_count = sum(1 for tag, _ in nodes if tag in {"h1", "h2"})

    # 一个大文档塞了多个章节：按 h1/h2 再切
    if heading_count >= 2 and total > 1500:
        chapters: list[Chapter] = []
        current: Chapter | None = None
        for tag, text in nodes:
            if tag in {"h1", "h2"}:
                if current and current.blocks:
                    chapters.append(current)
                current = Chapter(index=len(chapters), title=text, blocks=[])
                continue
            if current is None:
                current = Chapter(index=len(chapters), title=doc_title or "正文", blocks=[])
            current.blocks.append(
                Block(type="heading" if tag in _HEADING_TAGS else "narration", text=text)
            )
        if current and current.blocks:
            chapters.append(current)
        return chapters

    # 常规：一个文档 = 一章
    title = doc_title
    blocks: list[Block] = []
    for tag, text in nodes:
        if not title and tag in _HEADING_TAGS:
            title = text
            continue
        if tag in _HEADING_TAGS and title == text:
            continue
        blocks.append(Block(type="heading" if tag in _HEADING_TAGS else "narration", text=text))

    if not blocks and title:
        blocks = [Block(type="heading", text=title)]
    return [Chapter(index=0, title=title or doc_title or "正文", blocks=blocks)]


def _renumber(chapters: list[Chapter]) -> list[Chapter]:
    for i, chapter in enumerate(chapters):
        chapter.index = i
    return chapters
