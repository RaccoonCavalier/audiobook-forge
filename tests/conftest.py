import os
import sys
import tempfile
from pathlib import Path

import pytest

# 必须在 import audiobook_forge 之前把数据目录指到临时区
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="abf-test-"))
os.environ.setdefault("ABF_DATA_DIR", str(_TEST_ROOT / "data"))
os.environ["ABF_TTS_PROVIDER"] = "mock"
os.environ["ABF_LLM_ENABLED"] = "false"


@pytest.fixture(scope="session")
def sample_txt() -> Path:
    path = _TEST_ROOT / "fixtures" / "sample.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """书名：测试小说
作者：测试作者

第一章 开端

夜色渐深，远处的灯火像碎金撒在江面上。

“你来了。”张三说道。

李四点了点头：“我来了，路上堵车。”

第二章 相遇

张三笑了笑，道：“别来无恙？”

李四沉默了片刻，才缓缓开口：“托你的福。”
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="session")
def sample_md() -> Path:
    path = _TEST_ROOT / "fixtures" / "sample.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
title: 测试 markdown
---

# 第一篇 风起

这是正文第一段，用来测试段落提取。

## 小节一

> 引用的一句话不该带着 > 号。

“小明说：我们走吧。”

```python
print("代码块不应该被朗读")
```

- 列表项一
- 列表项二
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="session")
def sample_epub() -> Path:
    path = _TEST_ROOT / "fixtures" / "sample.epub"
    path.parent.mkdir(parents=True, exist_ok=True)
    _build_epub(path)
    return path


def _build_epub(target: Path) -> None:
    import zipfile

    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>测试 EPUB</dc:title>
    <dc:creator>测试作者</dc:creator>
    <dc:language>zh-CN</dc:language>
    <dc:identifier id="id">test-epub-001</dc:identifier>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="c1" href="chap1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="chap2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="c1"/>
    <itemref idref="c2"/>
  </spine>
</package>"""

    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>
    <navPoint id="n1"><navLabel><text>第一章 起点</text></navLabel><content src="chap1.xhtml"/></navPoint>
    <navPoint id="n2"><navLabel><text>第二章 终点</text></navLabel><content src="chap2.xhtml"/></navPoint>
  </navMap>
</ncx>"""

    def xhtml(title: str, paragraphs: list[str]) -> str:
        body = "\n".join(f"<p>{p}</p>" for p in paragraphs)
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title></head>
<body><h1>{title}</h1>{body}</body></html>"""

    with zipfile.ZipFile(target, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        zf.writestr(
            "OEBPS/chap1.xhtml",
            xhtml("第一章 起点", ["天亮了。", "“走吧。”他说。"]),
        )
        zf.writestr(
            "OEBPS/chap2.xhtml",
            xhtml("第二章 终点", ["天又黑了。", "她笑了。"]),
        )
