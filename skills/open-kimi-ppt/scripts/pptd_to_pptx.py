#!/usr/bin/env python3
"""Minimal PPTD -> PPTX reference renderer.

Scope: text elements, page background, per-slide fade transition, page order.
Not a replacement for the official renderer: images, shapes, tables, charts,
animations and font embedding are out of scope on purpose (see README).

Used by export_pptx_host.py --auto-generate so the host-side export channel is
self-contained. Callers that already own a generator should pass --pptx instead.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

EMU_PER_PX = 9525

CT_HEAD = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>"""

MASTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
</p:spTree></p:cSld>
<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
</p:sldMaster>"""

MASTER_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>"""

LAYOUT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">
<p:cSld name="Blank"><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"""

LAYOUT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>"""

THEME = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Probe"><a:themeElements>
<a:clrScheme name="Probe"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
<a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>
<a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2><a:accent3><a:srgbClr val="A5A5A5"/></a:accent3>
<a:accent4><a:srgbClr val="FFC000"/></a:accent4><a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="70AD47"/></a:accent6>
<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme>
<a:fontScheme name="Probe"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>
<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>
<a:fmtScheme name="Probe"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>
<a:lnStyleLst><a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>
<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>
<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme>
</a:themeElements></a:theme>"""


def _escape(text: str) -> str:
    return escape(text, {'"': "&quot;"})


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html or "", flags=re.I)
    text = re.sub(r"</(p|div)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(entity, char)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _text_box(shape_id: int, name: str, bounds: List[float], text: str,
              size_pt: float, color: str, bold: bool) -> str:
    x, y, w, h = [float(v) for v in bounds]
    # NOTE: no backslash escapes inside f-string expressions -- CI runs Python 3.11,
    # where PEP 701 is unavailable; keep the attribute concatenation explicit.
    bold_attr = ' b="1"' if bold else ""
    paragraphs = "".join(
        '<a:p><a:pPr algn="l"/><a:r><a:rPr lang="zh-CN" sz="{}"{} dirty="0">'
        '<a:solidFill><a:srgbClr val="{}"/></a:solidFill></a:rPr><a:t>{}</a:t></a:r></a:p>'.format(
            int(round(size_pt * 100)), bold_attr, color, _escape(line)
        )
        for line in text.split("\n")
    )
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{_escape(name)}"/>'
        f'<p:cNvSpPr txBox="1"><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{int(x * EMU_PER_PX)}" y="{int(y * EMU_PER_PX)}"/>'
        f'<a:ext cx="{int(w * EMU_PER_PX)}" cy="{int(h * EMU_PER_PX)}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
        f'<p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0"><a:normAutofit/></a:bodyPr>'
        f"<a:lstStyle/>{paragraphs}</p:txBody></p:sp>"
    )


def _slide_xml(page: Dict[str, Any]) -> str:
    bg = ""
    if page.get("bgColor"):
        bg = (
            f'<p:bg><p:bgPr><a:solidFill><a:srgbClr val="{page["bgColor"]}"/></a:solidFill>'
            f"<a:effectLst/></p:bgPr></p:bg>"
        )
    shapes = "".join(
        _text_box(i + 2, f"Text {i + 1}", item["bounds"], item["text"],
                  item["sizePt"], item["color"], item["bold"])
        for i, item in enumerate(page["texts"])
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">\n'
        f"<p:cSld>{bg}<p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        f"{shapes}</p:spTree></p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>"
        "<p:transition><p:fade/></p:transition></p:sld>"
    )


def _parse_text_elements(raw: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Accept JSON pages (preferred) and fall back to a narrow YAML scan."""
    page: Optional[Dict[str, Any]] = None
    try:
        page = json.loads(raw)
    except (ValueError, TypeError):
        page = None

    if isinstance(page, dict):
        background = page.get("background") or {}
        bg_color = str(background.get("color") or "").lstrip("#") or None
        texts: List[Dict[str, Any]] = []
        for element in page.get("elements") or []:
            if element.get("elementType") != "text":
                continue
            html = str((element.get("content") or {}).get("text") or "")
            size = re.search(r"font-size:\s*(\d+)px", html)
            color = re.search(r"color:\s*(#[0-9A-Fa-f]{3,8})", html)
            texts.append({
                "text": _strip_html(html),
                "sizePt": round(int(size.group(1)) * 0.75) if size else 18,
                "color": (color.group(1) if color else "#222222").lstrip("#"),
                "bold": bool(re.search(r"font-weight:\s*(700|bold)", html)),
                "bounds": element.get("bounds") or [80, 120, 800, 120],
            })
        return bg_color, texts

    bg_match = re.search(r'background:\s*\{[^}]*color:\s*"?(#[0-9A-Fa-f]{3,8})"?', raw)
    texts = []
    for index, block in enumerate(re.findall(r"text:\s*\|([\s\S]*?)(?=\n\s{0,10}\w+:|\n\s*$)", raw)):
        size = re.search(r"font-size:\s*(\d+)px", block)
        color = re.search(r"color:\s*(#[0-9A-Fa-f]{3,8})", block)
        texts.append({
            "text": _strip_html(block),
            "sizePt": round(int(size.group(1)) * 0.75) if size else 18,
            "color": (color.group(1) if color else "#222222").lstrip("#"),
            "bold": bool(re.search(r"font-weight:\s*(700|bold)", block)),
            "bounds": [80, 120 + index * 120, 800, 110],
        })
    return (bg_match.group(1).lstrip("#") if bg_match else None), texts


def build(manifest_path: Path) -> Tuple[bytes, Dict[str, Any]]:
    """Render a PPTD project into PPTX bytes."""
    manifest_path = Path(manifest_path)
    manifest = manifest_path.read_text(encoding="utf-8")
    root = manifest_path.parent

    size = re.search(r"size:\s*\[\s*(\d+)\s*,\s*(\d+)\s*\]", manifest)
    width, height = (int(size.group(1)), int(size.group(2))) if size else (960, 540)
    page_refs = re.findall(r"^\s{2,}-\s+(\S+\.page)\s*$", manifest, re.M)
    title = re.search(r"^title:\s*(.+)$", manifest, re.M)

    pages = []
    for reference in page_refs:
        path = root / reference
        if not path.is_file():
            raise SystemExit(f"missing page file: {reference}")
        bg_color, texts = _parse_text_elements(path.read_text(encoding="utf-8"))
        pages.append({"bgColor": bg_color, "texts": texts})
    if not pages:
        pages = [{"bgColor": None, "texts": []}]

    count = len(pages)
    entries: List[Tuple[str, str]] = []
    content_types = CT_HEAD
    for index in range(1, count + 1):
        content_types += (
            f'\n<Override PartName="/ppt/slides/slide{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        )
        entries.append((f"ppt/slides/slide{index}.xml", _slide_xml(pages[index - 1])))
        entries.append((
            f"ppt/slides/_rels/slide{index}.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" '
            'Target="../slideLayouts/slideLayout1.xml"/></Relationships>',
        ))
    content_types += "\n</Types>"

    slide_ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 2}"/>' for i in range(count))
    presentation = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">\n'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst>"
        f'<p:sldSz cx="{int(width * EMU_PER_PX)}" cy="{int(height * EMU_PER_PX)}"/>'
        '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
    )
    slide_rels = "".join(
        f'<Relationship Id="rId{i + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
        f'Target="slides/slide{i + 1}.xml"/>'
        for i in range(count)
    )
    presentation_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" '
        'Target="slideMasters/slideMaster1.xml"/>'
        f"{slide_rels}"
        f'<Relationship Id="rId{count + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" '
        'Target="theme/theme1.xml"/></Relationships>'
    )

    entries.extend([
        ("[Content_Types].xml", content_types),
        ("_rels/.rels", RELS),
        ("ppt/presentation.xml", presentation),
        ("ppt/_rels/presentation.xml.rels", presentation_rels),
        ("ppt/slideMasters/slideMaster1.xml", MASTER),
        ("ppt/slideMasters/_rels/slideMaster1.xml.rels", MASTER_RELS),
        ("ppt/slideLayouts/slideLayout1.xml", LAYOUT),
        ("ppt/slideLayouts/_rels/slideLayout1.xml.rels", LAYOUT_RELS),
        ("ppt/theme/theme1.xml", THEME),
    ])

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    summary = {
        "title": (title.group(1).strip().strip("\"'") if title else manifest_path.stem),
        "slides": count,
        "bytes": len(buffer.getvalue()),
        "renderer": "pptd_to_pptx (text/background/fade only)",
    }
    return buffer.getvalue(), summary


if __name__ == "__main__":
    import sys

    manifest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("deck.pptd")
    data, info = build(manifest)
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else manifest.with_suffix(".pptx")
    out.write_bytes(data)
    print(json.dumps({**info, "output": str(out)}, ensure_ascii=False))
