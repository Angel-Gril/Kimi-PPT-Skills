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
import argparse
import json
import math
import re
import zipfile
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

import yaml

from browser_export import BrowserExportError, exclusive_path_locks
from export_pptx import (
    ExportError, assert_safe_output_file, atomic_replace_file, find_manifest,
    read_yaml_mapping, safe_project_path,
)

# PPTD defines 1px = 1pt, and OOXML uses 12700 EMU per point.
EMU_PER_PX = 12700

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
    if re.search(r"[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]", text):
        raise ExportError("text contains characters that XML cannot represent")
    return escape(text, {'"': "&quot;"})


def _strip_html(html: str) -> str:
    class PlainText(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_starttag(self, tag, attrs):
            if tag == "br" or (tag in {"p", "div"} and self.parts and not self.parts[-1].endswith("\n")):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in {"p", "div"}:
                self.parts.append("\n")

        def handle_data(self, data):
            self.parts.append(data)

    parser = PlainText()
    parser.feed(html)
    parser.close()
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


def _color(value: str, colors: Dict[str, str]) -> str:
    seen = set()
    while isinstance(value, str) and value.startswith("$"):
        if value in seen or value[1:] not in colors:
            raise ExportError(f"unknown or cyclic theme color: {value}")
        seen.add(value)
        value = colors[value[1:]]
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3})?|#[0-9A-Fa-f]{4}(?:[0-9A-Fa-f]{4})?", value):
        raise ExportError(f"unsupported reference-renderer color: {value}")
    value = value[1:]
    if len(value) in (3, 4):
        value = "".join(char * 2 for char in value)
    return value.upper()


def _solid_fill(color: str) -> str:
    alpha = f'<a:alpha val="{round(int(color[6:], 16) / 255 * 100000)}"/>' if len(color) == 8 else ""
    return f'<a:solidFill><a:srgbClr val="{color[:6]}">{alpha}</a:srgbClr></a:solidFill>'


def _numbers(value: Any, count: int, label: str) -> List[float]:
    if not isinstance(value, (list, tuple)) or len(value) != count:
        raise ExportError(f"{label} must contain {count} numbers")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise ExportError(f"{label} must contain finite numbers")
    return [float(v) for v in value]


def _text_box(shape_id: int, name: str, bounds: List[float], text: str,
              size_pt: float, color: str, bold: bool, align: List[str], font: str) -> str:
    x, y, w, h = [float(v) for v in bounds]
    # NOTE: no backslash escapes inside f-string expressions -- CI runs Python 3.11,
    # where PEP 701 is unavailable; keep the attribute concatenation explicit.
    bold_attr = ' b="1"' if bold else ""
    paragraphs = "".join(
        '<a:p><a:pPr algn="{}"/><a:r><a:rPr lang="zh-CN" sz="{}"{} dirty="0">'
        '{}<a:latin typeface="{}"/><a:ea typeface="{}"/></a:rPr><a:t xml:space="preserve">{}</a:t></a:r></a:p>'.format(
            {"left": "l", "center": "ctr", "right": "r", "justify": "just"}.get(align[0], "l"),
            int(round(size_pt * 100)), bold_attr, _solid_fill(color), _escape(font), _escape(font), _escape(line)
        )
        for line in text.split("\n")
    )
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{_escape(name)}"/>'
        f'<p:cNvSpPr txBox="1"><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{int(x * EMU_PER_PX)}" y="{int(y * EMU_PER_PX)}"/>'
        f'<a:ext cx="{int(w * EMU_PER_PX)}" cy="{int(h * EMU_PER_PX)}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
        f'<p:txBody><a:bodyPr wrap="square" anchor="{ {"top": "t", "middle": "ctr", "bottom": "b"}.get(align[1], "t") }" lIns="0" tIns="0" rIns="0" bIns="0"><a:normAutofit/></a:bodyPr>'
        f"<a:lstStyle/>{paragraphs}</p:txBody></p:sp>"
    )


def _slide_xml(page: Dict[str, Any]) -> str:
    bg = ""
    if page.get("bgColor"):
        bg = (
            f'<p:bg><p:bgPr>{_solid_fill(page["bgColor"])}'
            f"<a:effectLst/></p:bgPr></p:bg>"
        )
    shapes = "".join(
        _text_box(i + 2, f"Text {i + 1}", item["bounds"], item["text"],
                  item["sizePt"], item["color"], item["bold"], item["align"], item["font"])
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


def _parse_text_elements(raw: str, theme: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Read both YAML and JSON pages without losing scalar text or geometry."""
    try:
        page = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ExportError(f"invalid page YAML: {exc}") from exc
    if not isinstance(page, dict) or not isinstance(page.get("elements"), list):
        raise ExportError("page must be a mapping with an elements array")
    theme = theme or {}
    colors = theme.get("colors") or {}
    background = page.get("background") or {}
    bg_color = _color(background.get("color", "#FFFFFF"), colors) if background.get("type", "solid") == "solid" else "FFFFFF"
    texts = []
    for element in page["elements"]:
        if not isinstance(element, dict):
            raise ExportError("page elements must be mappings")
        if element.get("elementType") != "text":
            continue
        content = element.get("content") or {}
        style_name = str(content.get("style") or "").lstrip("$")
        style = {**(theme.get("textStyles", {}).get(style_name) or {}), **content}
        html = str(content.get("text") or "")
        size = re.search(r"font-size:\s*(\d+(?:\.\d+)?)(?:px|pt)", html, re.I)
        color = re.search(r"(?:[;\"']|^)\s*color:\s*(#[0-9A-Fa-f]{3,8}|\$[\w-]+)", html)
        bounds = _numbers(element.get("bounds"), 4, "text bounds")
        if bounds[2] <= 0 or bounds[3] <= 0:
            raise ExportError("text bounds width and height must be positive")
        size_pt = float(size.group(1)) if size else float(style.get("fontSize", 18))
        if not math.isfinite(size_pt) or not 1 <= size_pt <= 4000:
            raise ExportError("text fontSize must be between 1 and 4000")
        align = style.get("align", ["left", "top"])
        if not isinstance(align, list) or len(align) != 2:
            raise ExportError("text align must contain horizontal and vertical alignment")
        font = style.get("fontFamily", "MiSans")
        if isinstance(font, dict):
            font = font.get("ea") or font.get("latin") or "MiSans"
        texts.append({
            "text": _strip_html(html),
            "sizePt": size_pt,
            "color": _color(color.group(1) if color else style.get("color", "#000000"), colors),
            "bold": bool(style.get("bold") or re.search(r"font-weight:\s*(?:[6-9]00|bold)|<(?:strong|b)(?:\s|>)", html, re.I)),
            "bounds": bounds,
            "align": align,
            "font": str(font),
        })
    return bg_color, texts


def build(manifest_path: Path) -> Tuple[bytes, Dict[str, Any]]:
    """Render a PPTD project into PPTX bytes."""
    manifest_path = Path(manifest_path).resolve()
    _, manifest = read_yaml_mapping(manifest_path)
    root = manifest_path.parent
    if manifest.get("version") != "v2":
        raise ExportError("reference rendering requires PPTD version: v2")
    width, height = _numbers(manifest.get("size", [960, 540]), 2, "slide size")
    if width <= 0 or height <= 0:
        raise ExportError("slide size must be positive")
    page_refs = manifest.get("pages")
    if not isinstance(page_refs, list) or not page_refs:
        raise ExportError("PPTD manifest must contain a non-empty pages list")

    pages = []
    for reference in page_refs:
        path = safe_project_path(root, reference)
        if not path.is_file():
            raise SystemExit(f"missing page file: {reference}")
        bg_color, texts = _parse_text_elements(path.read_text(encoding="utf-8"), manifest.get("theme"))
        pages.append({"bgColor": bg_color, "texts": texts})

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
        "title": str(manifest.get("title") or manifest_path.stem),
        "slides": count,
        "bytes": len(buffer.getvalue()),
        "renderer": "pptd_to_pptx (text/background/fade only)",
    }
    return buffer.getvalue(), summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--force", action="store_true", help="replace an existing PPTX")
    args = parser.parse_args(argv)
    try:
        manifest = find_manifest(args.manifest)
        out = (args.output or manifest.with_suffix(".pptx")).expanduser().resolve()
        assert_safe_output_file(manifest, out)
        with exclusive_path_locks([out]):
            if out.exists() and not args.force:
                raise ExportError(f"output already exists (pass --force to replace it): {out}")
            data, info = build(manifest)
            with tempfile.TemporaryDirectory(prefix="pptd-reference-") as name:
                stage = Path(name) / "render.pptx"
                stage.write_bytes(data)
                atomic_replace_file(stage, out, force=args.force)
        print(json.dumps({**info, "output": str(out)}, ensure_ascii=False))
    except (ExportError, BrowserExportError, OSError) as exc:
        parser.exit(1, f"reference render failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
