"""Host live-export channel: virtual-FS rendering and the render endpoint."""

from __future__ import annotations

import json
import hashlib
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
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
        data, _ = pptd_to_pptx.build_from_files(as_list)
        self.assertEqual(len(_slide_texts(data)), 2)

    def test_browser_saved_colors_remain_visible_on_dark_background(self) -> None:
        ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
        for css, expected, alpha in [
            ('rgb(255, 255, 255)', 'FFFFFF', None),
            ('rgba(255, 255, 255, 0.5)', 'FFFFFF', '50196'),
            ('rgb(100% 100% 100% / 50%)', 'FFFFFF', '50196'),
            ('rgb(23, 25, 35)', '171923', None),
        ]:
            with self.subTest(css=css):
                # This nesting and rgb() syntax are emitted by the real editor.
                edited = PAGE_ONE.replace(
                    '<span style="font-size:40px;color:#FFFFFF">第一页标题</span>',
                    f'<span style="font-size: 40px;"><span style="color: {css};">改过的标题</span></span>',
                )
                data, _ = pptd_to_pptx.build_from_files(_files(**{'pages/01.page': edited}))
                with zipfile.ZipFile(BytesIO(data)) as archive:
                    slide = ET.fromstring(archive.read('ppt/slides/slide1.xml'))
                color = slide.find('.//a:rPr/a:solidFill/a:srgbClr', ns)
                self.assertEqual(color.get('val'), expected)
                opacity = color.find('a:alpha', ns)
                self.assertEqual(opacity.get('val') if opacity is not None else None, alpha)
                self.assertEqual(slide.find('.//a:t', ns).text, '改过的标题')

    def test_invalid_css_colors_are_rejected(self) -> None:
        for color in ('rgb(256, 0, 0)', 'rgba(255, 0, 0, 2)', 'rgb(NaN, 0, 0)', 'rgb(1, 2)'):
            with self.subTest(color=color), self.assertRaises(pptd_to_pptx.ExportError):
                pptd_to_pptx._color(color, {})

    def test_saved_font_size_change_reaches_the_package(self) -> None:
        edited = PAGE_ONE.replace('font-size:40px', 'font-size: 60px;')
        data, _ = pptd_to_pptx.build_from_files(_files(**{'pages/01.page': edited}))
        with zipfile.ZipFile(BytesIO(data)) as archive:
            slide = ET.fromstring(archive.read('ppt/slides/slide1.xml'))
        ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
        self.assertEqual(slide.find('.//a:rPr', ns).get('sz'), '6000')

    def test_rejects_path_traversal(self) -> None:
        invalid_paths = ['../escape.page', '/deck.pptd', chr(67) + ':/outside/a.page',
                         '//server/share/a.page', 'pages/..', 'pages/a/../b.page', 123, 'bad\x00name']
        for path in invalid_paths:
            with self.subTest(path=path), self.assertRaises(pptd_to_pptx.ExportError):
                pptd_to_pptx.build_from_files({**_files(), path: 'x'})

    def test_rejects_duplicate_paths_and_invalid_file_entries(self) -> None:
        as_list = [{'path': path, 'content': content} for path, content in _files().items()]
        for extra in [
            {'path': './pages/01.page', 'content': PAGE_TWO},
            {'path': 'pages//01.page', 'content': PAGE_TWO},
            {'path': 'pages/01.page', 'content': PAGE_TWO},
            {'path': 'pages/new.page'},
            {'path': 'pages/new.page', 'content': {}},
            42,
        ]:
            with self.subTest(extra=extra), self.assertRaises(pptd_to_pptx.ExportError):
                pptd_to_pptx.build_from_files([*as_list, extra])

    def test_rejects_ambiguous_manifests(self) -> None:
        with self.assertRaisesRegex(pptd_to_pptx.ExportError, 'exactly one'):
            pptd_to_pptx.build_from_files({**_files(), 'another.pptd': MANIFEST})

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

    def _post(self, payload, headers=None):
        return self._post_raw(json.dumps(payload).encode('utf-8'), headers)

    def _post_raw(self, body, headers=None):
        request_headers = {
            'Content-Type': 'application/json',
            'Origin': self.base,
            'X-Render-Token': self.state['token'],
        }
        request_headers.update(headers or {})
        request = urllib.request.Request(
            f"{self.base}{export_pptx_host.RENDER_PATH}",
            data=body,
            headers=request_headers,
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=30)

    def _assert_rejected(self, payload, code=400, headers=None):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post(payload, headers)
        self.assertEqual(caught.exception.code, code)
        caught.exception.close()

    def test_renders_posted_virtual_fs(self) -> None:
        with self._post({"files": _files(), "title": "live"}) as response:
            self.assertEqual(response.status, 200)
            payload = response.read()
            self.assertIn("presentationml.presentation", response.headers.get("content-type", ""))
            self.assertEqual(response.headers.get('X-Render-Pages'), '2')
        self.assertIn("第一页标题", _slide_texts(payload))
        self.assertEqual(self.state['renders'][hashlib.sha256(payload).hexdigest()], 2)

    def test_browser_file_list_is_rendered_and_changes_page_count(self) -> None:
        files = _files()
        files['deck.pptd'] = MANIFEST.replace('  - pages/02.page\n', '')
        with self._post({'files': [{'path': p, 'content': c} for p, c in files.items()]}) as response:
            data = response.read()
            self.assertEqual(response.headers.get('X-Render-Pages'), '1')
        self.assertEqual(self.state['renders'][hashlib.sha256(data).hexdigest()], 1)

    def test_other_origins_missing_session_and_non_json_requests_are_rejected(self) -> None:
        body = {'files': _files()}
        for headers, code in [
            ({'Origin': 'http://127.0.0.1:9999'}, 403),
            ({'Origin': 'null'}, 403),
            ({'X-Render-Token': ''}, 403),
            ({'X-Render-Token': 'wrong-session'}, 403),
            ({'Content-Type': 'text/plain'}, 415),
        ]:
            with self.subTest(headers=headers):
                self._assert_rejected(body, code, headers)
        self.assertEqual(self.state['renders'], {})

    def test_malformed_request_structures_return_400_and_server_stays_usable(self) -> None:
        for payload in ([], None, 'text', 12, {}, {'files': []}, {'files': _files(), 'title': []},
                        {'files': [{'path': 'deck.pptd', 'content': {}}]},
                        {'files': _files(**{'pages/01.page': 'elements: [{elementType: text, content: 42}]'})}):
            with self.subTest(payload=payload):
                self._assert_rejected(payload)
        for body in (b'{', b'{"files": {}, "files": {}}', b'\xff'):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as caught:
                self._post_raw(body)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        self.assertEqual(self.state['renders'], {})
        with self._post({'files': _files()}) as response:
            self.assertEqual(response.status, 200)

    def test_interleaved_render_results_each_keep_their_own_digest_and_pages(self) -> None:
        first = _files()
        second = _files()
        second['deck.pptd'] = MANIFEST.replace('  - pages/02.page\n', '')
        second['pages/01.page'] = PAGE_ONE.replace('第一页标题', '第二次请求')

        def render(files):
            with self._post({'files': files}) as response:
                return response.read()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(render, [first, second]))
        self.assertEqual(len(self.state['renders']), 2)
        for index, (data, count) in enumerate(zip(results, [2, 1])):
            self.assertEqual(self.state['renders'][hashlib.sha256(data).hexdigest()], count)
            path = self.root / f'result-{index}.pptx'
            path.write_bytes(data)
            export_pptx_host.validate_live_download(path, self.state)
        (self.root / 'tampered.pptx').write_bytes(b'tampered')
        with self.assertRaises(export_pptx_host.ExportError):
            export_pptx_host.validate_live_download(self.root / 'tampered.pptx', self.state)
        with self.assertRaises(export_pptx_host.ExportError):
            export_pptx_host.validate_live_download(self.root / 'result-0.pptx', {'renders': {}})

    def test_rejects_unknown_paths(self) -> None:
        self._assert_rejected({"files": {"deck.pptd": MANIFEST}})

    def test_static_files_still_served(self) -> None:
        with urllib.request.urlopen(f"{self.base}/index.html", timeout=30) as response:
            self.assertEqual(response.read(), b"ok")


if __name__ == "__main__":
    unittest.main()
