import io
import json
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from argparse import Namespace
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import export_pptx_host as host
import pptd_to_pptx as renderer
from export_pptx import ExportError

NS = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'p': 'http://schemas.openxmlformats.org/presentationml/2006/main'}
FIXTURE = Path(__file__).parent / 'fixtures/minimal/minimal.pptd'


class HostExportTests(unittest.TestCase):
    def test_reference_preserves_fixture_geometry_units_and_text(self):
        data, info = renderer.build(FIXTURE)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIsNone(archive.testzip())
            for name in archive.namelist():
                if name.endswith('.xml') or name.endswith('.rels'):
                    ET.fromstring(archive.read(name))
            slide = ET.fromstring(archive.read('ppt/slides/slide1.xml'))
            offset = slide.find('.//p:spPr/a:xfrm/a:off', NS)
            self.assertEqual(offset.attrib, {'x': '1270000', 'y': '2286000'})
            self.assertEqual(slide.find('.//a:rPr', NS).get('sz'), '4200')
            self.assertEqual(slide.find('.//a:pPr', NS).get('algn'), 'ctr')
            self.assertEqual(slide.find('.//a:bodyPr', NS).get('anchor'), 'ctr')
            self.assertEqual(slide.find('.//a:t', NS).text, '\u672c\u5730\u5bfc\u51fa\u9a8c\u8bc1')
            self.assertEqual(info['slides'], 1)

    def test_yaml_and_json_preserve_scalars_theme_and_order(self):
        page = {'background': {'type': 'solid', 'color': '$bg'}, 'elements': [
            {'elementType': 'text', 'bounds': [10, 20, 30, 40],
             'content': {'style': '$title', 'text': 'Scalar &amp; &#34;quoted&#34;'}},
            {'elementType': 'text', 'bounds': [50, 60, 70, 80],
             'content': {'fontSize': 12.5, 'color': '#abc8', 'text': '<p>First</p><p>Second</p>'}},
        ]}
        theme = {'colors': {'bg': '#123', 'fg': '#ABCDEF'},
                 'textStyles': {'title': {'color': '$fg', 'fontSize': 24, 'bold': True}}}
        import yaml
        for raw in (json.dumps(page), yaml.safe_dump(page)):
            background, texts = renderer._parse_text_elements(raw, theme)
            self.assertEqual(background, '112233')
            self.assertEqual([item['text'] for item in texts], ['Scalar & "quoted"', 'First\nSecond'])
            self.assertEqual(texts[0]['bounds'], [10, 20, 30, 40])
            self.assertEqual(texts[0]['sizePt'], 24)
            self.assertEqual(texts[0]['color'], 'ABCDEF')
            self.assertTrue(texts[0]['bold'])
            self.assertEqual(texts[1]['sizePt'], 12.5)
            self.assertEqual(texts[1]['color'], 'AABBCC88')

    def test_repository_example_preserves_every_text_element(self):
        import yaml
        manifest = SCRIPTS.parents[2] / 'example/dji-pocket4/dji-pocket4.pptd'
        if not manifest.is_file():
            self.skipTest('repository examples are not installed')
        document = yaml.safe_load(manifest.read_text(encoding='utf-8'))
        data, info = renderer.build(manifest)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(info['slides'], len(document['pages']))
            for index, page_path in enumerate(document['pages'], 1):
                page = yaml.safe_load((manifest.parent / page_path).read_text(encoding='utf-8'))
                expected = [e for e in page['elements'] if e['elementType'] == 'text']
                slide = ET.fromstring(archive.read(f'ppt/slides/slide{index}.xml'))
                self.assertEqual(len(slide.findall('.//p:sp', NS)), len(expected))
                self.assertFalse(any('elementId:' in (node.text or '') for node in slide.findall('.//a:t', NS)))

    def test_rejects_page_escape_in_both_entries(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            project = root / 'project'
            project.mkdir()
            (root / 'private.page').write_text('elements: []', encoding='utf-8')
            manifest = project / 'deck.pptd'
            manifest.write_text('version: v2\nsize: [960, 540]\npages: [../private.page]', encoding='utf-8')
            for entry in (host.build_payload, renderer.build):
                with self.assertRaisesRegex(ExportError, 'escapes'):
                    entry(manifest)

    def test_payload_only_includes_referenced_images(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'deck.pptd').write_text('version: v2\npages: ["a page.page"]', encoding='utf-8')
            (root / 'a page.page').write_text('elements:\n  - elementType: image\n    src: used.png', encoding='utf-8')
            (root / 'used.png').write_bytes(b'used')
            (root / 'private.png').write_bytes(b'private')
            payload = host.build_payload(root / 'deck.pptd')
            self.assertEqual(set(payload['imageMap']), {'used.png'})
            self.assertEqual(payload['pages'][0]['path'], 'a page.page')

    def test_invalid_artifact_and_wrong_page_count_are_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'deck.pptx'
            path.write_bytes(b'not a pptx')
            with self.assertRaises(ExportError):
                host.validate_artifact(path, 1)
            path.write_bytes(renderer.build(FIXTURE)[0])
            host.validate_artifact(path, 1)
            with self.assertRaisesRegex(ExportError, 'slide count mismatch'):
                host.validate_artifact(path, 2)

    def test_malformed_slide_xml_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'broken.pptx'
            data, _ = renderer.build(FIXTURE)
            with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as broken:
                for entry in original.namelist():
                    broken.writestr(entry, b'<broken' if entry == 'ppt/slides/slide1.xml' else original.read(entry))
            with self.assertRaisesRegex(ExportError, 'not a valid host PPTX'):
                host.validate_artifact(path, 1)

    def test_invalid_manifest_does_not_create_blank_success(self):
        with tempfile.TemporaryDirectory() as name:
            manifest = Path(name) / 'deck.pptd'
            for pages in ('[]', 'null'):
                manifest.write_text(f'version: v2\npages: {pages}', encoding='utf-8')
                with self.assertRaisesRegex(ExportError, 'non-empty pages'):
                    renderer.build(manifest)

    def test_reference_cli_preserves_existing_output_and_source(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / 'deck.pptx'
            output.write_bytes(b'keep me')
            with self.assertRaises(SystemExit):
                renderer.main([str(FIXTURE), str(output)])
            self.assertEqual(output.read_bytes(), b'keep me')
            renderer.main([str(FIXTURE), str(output), '--force'])
            host.validate_artifact(output, 1)
            with self.assertRaises(SystemExit):
                renderer.main([str(FIXTURE), str(FIXTURE), '--force'])

    def test_host_refuses_existing_output_before_browser_start(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / 'existing.pptx'
            output.write_bytes(b'keep me')
            args = Namespace(project=str(FIXTURE), output=str(output), auto_generate=True,
                             pptx=None, force=False, timeout=30, headed=False)
            with mock.patch.object(host, 'launch_ephemeral_browser') as launch:
                with self.assertRaisesRegex(ExportError, 'already exists'):
                    host.export_host(args)
                launch.assert_not_called()
            self.assertEqual(output.read_bytes(), b'keep me')

    def test_failed_or_tampered_download_preserves_output_and_closes_resources(self):
        for fail_download in (False, True):
            with self.subTest(fail_download=fail_download), tempfile.TemporaryDirectory() as name:
                output = Path(name) / 'existing.pptx'
                output.write_bytes(b'previous valid output')
                args = Namespace(project=str(FIXTURE), output=str(output), auto_generate=True,
                                 pptx=None, force=True, timeout=30, headed=False)
                browser, context, server, thread = (mock.MagicMock() for _ in range(4))

                def download(_download, path, **kwargs):
                    if fail_download:
                        raise host.BrowserExportError('download failed')
                    path.write_bytes(b'tampered')

                with mock.patch('playwright.sync_api.sync_playwright'), \
                     mock.patch.object(host, 'serve', return_value=(server, thread, 'http://127.0.0.1/export_host.html')), \
                     mock.patch.object(host, 'launch_ephemeral_browser', return_value=(browser, context)), \
                     mock.patch.object(host, 'wait_for_editor'), \
                     mock.patch.object(host, 'open_export_dialog'), \
                     mock.patch.object(host, 'save_download_before_deadline', side_effect=download):
                    with self.assertRaises((ExportError, host.BrowserExportError)):
                        host.export_host(args)
                self.assertEqual(output.read_bytes(), b'previous valid output')
                context.close.assert_called_once()
                browser.close.assert_called_once()
                server.shutdown.assert_called_once()
                server.server_close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
