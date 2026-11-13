"""Host live-export channel: virtual-FS rendering and the render endpoint."""

from __future__ import annotations

import json
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import export_pptx_host
import pptd_to_pptx

MANIFEST = """version: v2
title: live export fixture
size: [960, 540]
pages:
  - pages/01.page
  - pages/02.page
"""

PAGE_ONE = """pageType: content
background: {type: solid, color: "#101828"}
elements:
  - elementId: title
    elementType: text
    bounds: [80, 120, 760, 120]
    content:
      align: [left, middle]
      fontFamily: MiSans
      text: |
        <p><span style="font-size:40px;color:#FFFFFF">第一页标题</span></p>
"""

PAGE_TWO = """pageType: content
elements:
  - elementId: body
    elementType: text
    bounds: [80, 200, 760, 200]
    content:
      align: [left, top]
      fontFamily: MiSans
      text: |
        <p><span style="font-size:20px;color:#101828">第二页正文</span></p>
"""


def _files(**overrides: str) -> dict:
    files = {
        "deck.pptd": MANIFEST,
        "pages/01.page": PAGE_ONE,
        "pages/02.page": PAGE_TWO,
    }
    files.update(overrides)
    return files


def _slide_texts(data: bytes) -> list[str]:
    """Collect run text; slide XML may carry xml:space attributes on a:t."""
    texts: list[str] = []
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = sorted(
            name for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
        for name in names:
            xml = archive.read(name).decode("utf-8")
            texts.extend(re.findall(r"<a:t[^>]*>(.*?)</a:t>", xml, re.S))
    return texts


class BuildFromFilesTest(unittest.TestCase):
    def test_renders_every_page_from_memory(self) -> None:
        data, info = pptd_to_pptx.build_from_files(_files())
        self.assertEqual(info["slides"], 2)
        self.assertEqual(len(_slide_texts(data)), 2)
        self.assertIn("第一页标题", _slide_texts(data))
        self.assertIn("第二页正文", _slide_texts(data))

    def test_edited_content_reaches_the_package(self) -> None:
        edited = PAGE_ONE.replace("第一页标题", "改过的标题")
        data, _ = pptd_to_pptx.build_from_files(_files(**{"pages/01.page": edited}))
        self.assertIn("改过的标题", _slide_texts(data))
        self.assertNotIn("第一页标题", _slide_texts(data))

    def test_accepts_the_editor_list_shape(self) -> None:
        as_list = [{"path": path, "content": text} for path, text in _files().items()]
        data, _ = pptd_to_pptx.build_from_files(
            {item["path"]: item["content"] for item in as_list}
        )
        self.assertEqual(len(_slide_texts(data)), 2)

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaises(pptd_to_pptx.ExportError):
            pptd_to_pptx.build_from_files(_files(**{"../escape.page": "x"}))

    def test_requires_the_referenced_page(self) -> None:
        files = _files()
        files.pop("pages/02.page")
        with self.assertRaises(pptd_to_pptx.ExportError):
            pptd_to_pptx.build_from_files(files)

    def test_requires_a_manifest(self) -> None:
        with self.assertRaises(pptd_to_pptx.ExportError):
            pptd_to_pptx.build_from_files({"pages/01.page": PAGE_ONE})

    def test_matches_the_disk_renderer(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "pages").mkdir()
            (root / "deck.pptd").write_text(MANIFEST, encoding="utf-8")
            (root / "pages" / "01.page").write_text(PAGE_ONE, encoding="utf-8")
            (root / "pages" / "02.page").write_text(PAGE_TWO, encoding="utf-8")
            disk, disk_info = pptd_to_pptx.build(root / "deck.pptd")
        memory, memory_info = pptd_to_pptx.build_from_files(_files())
        self.assertEqual(disk_info["slides"], memory_info["slides"])
        self.assertEqual(_slide_texts(disk), _slide_texts(memory))


class RenderEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        (self.root / "index.html").write_text("ok", encoding="utf-8")
        self.state: dict = {}
        self.server, self.thread, url = export_pptx_host.serve_host(self.root, self.state)
        self.base = url.rsplit("/", 1)[0]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._temp.cleanup()

    def _post(self, payload: dict) -> urllib.request.Request:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{export_pptx_host.RENDER_PATH}",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=30)

    def test_renders_posted_virtual_fs(self) -> None:
        with self._post({"files": _files(), "title": "live"}) as response:
            self.assertEqual(response.status, 200)
            payload = response.read()
            self.assertIn("presentationml.presentation", response.headers.get("content-type", ""))
        self.assertIn("第一页标题", _slide_texts(payload))
        self.assertEqual(self.state["slides"], 2)
        self.assertIn("digest", self.state)

    def test_rejects_unknown_paths(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post({"files": {"deck.pptd": MANIFEST}})
        self.assertEqual(caught.exception.code, 400)

    def test_static_files_still_served(self) -> None:
        with urllib.request.urlopen(f"{self.base}/index.html", timeout=30) as response:
            self.assertEqual(response.read(), b"ok")


if __name__ == "__main__":
    unittest.main()
