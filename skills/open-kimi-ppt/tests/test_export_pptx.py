#!/usr/bin/env python3
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_pptx.py"
SPEC = importlib.util.spec_from_file_location("export_pptx", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def write_minimal_pptx(
    path: Path,
    *,
    slide_xml: str,
    slide_count: int = 1,
    relationship_targets=None,
) -> None:
    presentation_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    relationship_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_relationship_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    slide_ids = "".join(
        f'<p:sldId id="{256 + index}" r:id="rId{index}"/>'
        for index in range(1, slide_count + 1)
    )
    targets = relationship_targets or [
        f"slides/slide{index}.xml" for index in range(1, slide_count + 1)
    ]
    relationships = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
        f'Target="{targets[index - 1]}"/>'
        for index in range(1, slide_count + 1)
    )
    overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for index in range(1, slide_count + 1)
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/ppt/presentation.xml" '
            f'ContentType="{MODULE.PPTX_CONTENT_TYPE}"/>{overrides}</Types>',
        )
        archive.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{package_relationship_ns}">'
            '<Relationship Id="rIdOffice" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="ppt/presentation.xml"/></Relationships>',
        )
        archive.writestr(
            "ppt/presentation.xml",
            f'<p:presentation xmlns:p="{presentation_ns}" xmlns:r="{relationship_ns}">'
            f'<p:sldIdLst>{slide_ids}</p:sldIdLst></p:presentation>',
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            f'<Relationships xmlns="{package_relationship_ns}">{relationships}</Relationships>',
        )
        for index in range(1, slide_count + 1):
            archive.writestr(f"ppt/slides/slide{index}.xml", slide_xml)


class ExportPptxTests(unittest.TestCase):
    def test_fade_is_inserted_before_timing(self):
        source = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<p:sld xmlns:p="urn:test"><p:cSld><p:spTree><p:extLst/>'
            b'</p:spTree></p:cSld><p:clrMapOvr/><p:timing/><p:extLst/></p:sld>'
        )
        result_bytes = MODULE.replace_transition(source, "fade")
        result = result_bytes.decode("utf-8")
        self.assertIn("<p:transition", result)
        self.assertIn("<p:fade/>", result)
        self.assertGreater(result.index("<p:transition"), result.index("<p:clrMapOvr"))
        self.assertLess(result.index("<p:transition"), result.index("<p:timing"))
        MODULE.validate_transition_order(result_bytes, "fade")

    def test_existing_transition_is_replaced_or_removed(self):
        source = (
            b'<p:sld xmlns:p="urn:test"><p:cSld/>'
            b'<p:transition><p:wipe/></p:transition><p:extLst/></p:sld>'
        )
        faded = MODULE.replace_transition(source, "fade").decode("utf-8")
        self.assertNotIn("p:wipe", faded)
        self.assertEqual(faded.count("<p:transition"), 1)
        MODULE.validate_transition_order(faded.encode("utf-8"), "fade")
        cleared = MODULE.replace_transition(source, "none").decode("utf-8")
        self.assertNotIn("p:transition", cleared)
        MODULE.validate_transition_order(cleared.encode("utf-8"), "none")

    def test_nested_transition_is_relocated_to_slide_root(self):
        source = (
            b'<p:sld xmlns:p="urn:test"><p:cSld><p:spTree>'
            b'<p:transition><p:fade/></p:transition><p:extLst/>'
            b'</p:spTree></p:cSld><p:clrMapOvr/><p:extLst/></p:sld>'
        )
        result = MODULE.replace_transition(source, "fade")
        MODULE.validate_transition_order(result, "fade")
        self.assertEqual(MODULE.root_child_names(result), [
            "cSld", "clrMapOvr", "transition", "extLst"
        ])

    def test_patch_transitions_preserves_a_valid_zip(self):
        with tempfile.TemporaryDirectory() as name:
            deck = Path(name) / "test.pptx"
            write_minimal_pptx(
                deck,
                slide_xml='<p:sld xmlns:p="urn:test"><p:cSld/></p:sld>',
            )
            self.assertEqual(MODULE.patch_transitions(deck, "fade"), 1)
            with zipfile.ZipFile(deck) as archive:
                self.assertIsNone(archive.testzip())
                slide = archive.read("ppt/slides/slide1.xml")
                self.assertIn(b"<p:fade/>", slide)
    def test_verify_editability_accepts_independent_shapes(self):
        with tempfile.TemporaryDirectory() as name:
            deck = Path(name) / "editable.pptx"
            write_minimal_pptx(
                deck,
                slide_xml=(
                    '<p:sld xmlns:p="urn:test" xmlns:a="urn:a"><p:cSld><p:spTree>'
                    '<p:sp><p:txBody><a:p><a:r><a:t>Editable</a:t></a:r></a:p>'
                    '</p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
                ),
            )
            counts = MODULE.verify_editability(deck)
            self.assertEqual(counts["shapes"], 1)
            self.assertEqual(counts["textObjects"], 1)

    def test_verify_editability_rejects_image_only_deck(self):
        with tempfile.TemporaryDirectory() as name:
            deck = Path(name) / "image-only.pptx"
            write_minimal_pptx(
                deck,
                slide_xml=(
                    '<p:sld xmlns:p="urn:test"><p:cSld><p:spTree>'
                    '<p:pic/></p:spTree></p:cSld></p:sld>'
                ),
            )
            with self.assertRaisesRegex(MODULE.ExportError, "image-only fallback"):
                MODULE.verify_editability(deck)
            counts = MODULE.verify_editability(deck, allow_image_only=True)
            self.assertEqual(counts["pictures"], 1)

    def test_browser_profile_defaults_outside_project(self):
        args = MODULE.parse_args(["deck.pptd"])
        self.assertEqual(args.profile_dir, MODULE.DEFAULT_PROFILE_DIR)
        self.assertNotIn("deck", str(args.profile_dir).lower())

    def test_build_payload_embeds_only_referenced_local_images(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pages").mkdir()
            (root / "media").mkdir()
            (root / "deck.pptd").write_text(
                "version: v2\ntitle: Safe\nsize: LAYOUT_WIDE\npages:\n  - pages/001.yaml\n",
                encoding="utf-8",
            )
            (root / "pages" / "001.yaml").write_text(
                "elements:\n  - type: image\n    src: media/used.png\n",
                encoding="utf-8",
            )
            (root / "media" / "used.png").write_bytes(b"used")
            (root / "media" / "private.png").write_bytes(b"private")
            payload = MODULE.build_payload(root / "deck.pptd")
            self.assertIn("media/used.png", payload["imageMap"])
            self.assertNotIn("media/private.png", payload["imageMap"])
            serialized = str(payload["imageMap"])
            self.assertNotIn("cHJpdmF0ZQ", serialized)

    def test_verify_relationship_graph_rejects_missing_and_orphaned_slides(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            missing = root / "missing-slide.pptx"
            write_minimal_pptx(
                missing,
                slide_xml='<p:sld xmlns:p="urn:test"/>',
                relationship_targets=["slides/does-not-exist.xml"],
            )
            with zipfile.ZipFile(missing) as archive:
                with self.assertRaisesRegex(MODULE.ExportError, "broken PPTX relationship"):
                    MODULE.validate_pptx_relationship_graph(archive)

            orphaned = root / "orphaned-slide.pptx"
            write_minimal_pptx(
                orphaned,
                slide_xml='<p:sld xmlns:p="urn:test"/>',
            )
            with zipfile.ZipFile(orphaned, "a", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("ppt/slides/slide99.xml", '<p:sld xmlns:p="urn:test"/>')
            with zipfile.ZipFile(orphaned) as archive:
                with self.assertRaisesRegex(MODULE.ExportError, "orphaned slide parts"):
                    MODULE.validate_pptx_relationship_graph(archive)

    def test_verify_relationship_graph_rejects_invalid_opc_metadata(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            slide_xml = '<p:sld xmlns:p="urn:test"/>'

            missing_root = root / "missing-root.pptx"
            write_minimal_pptx(missing_root, slide_xml=slide_xml)
            rewritten = root / "rewritten.pptx"
            with zipfile.ZipFile(missing_root) as source, zipfile.ZipFile(
                rewritten, "w", zipfile.ZIP_DEFLATED
            ) as target:
                for info in source.infolist():
                    if info.filename != "_rels/.rels":
                        target.writestr(info, source.read(info.filename))
            self.assertFalse(MODULE.is_pptx(rewritten))

            wrong_type = root / "wrong-type.pptx"
            write_minimal_pptx(wrong_type, slide_xml=slide_xml)
            mutated = root / "wrong-type-mutated.pptx"
            with zipfile.ZipFile(wrong_type) as source, zipfile.ZipFile(
                mutated, "w", zipfile.ZIP_DEFLATED
            ) as target:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename == "ppt/_rels/presentation.xml.rels":
                        data = data.replace(
                            b"/relationships/slide\"",
                            b"/relationships/image\"",
                        )
                    target.writestr(info, data)
            self.assertFalse(MODULE.is_pptx(mutated))

            bad_types = root / "bad-types.pptx"
            write_minimal_pptx(bad_types, slide_xml=slide_xml)
            malformed = root / "bad-types-mutated.pptx"
            with zipfile.ZipFile(bad_types) as source, zipfile.ZipFile(
                malformed, "w", zipfile.ZIP_DEFLATED
            ) as target:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename == "[Content_Types].xml":
                        data = b"not xml " + MODULE.PPTX_CONTENT_TYPE.encode("utf-8")
                    target.writestr(info, data)
            self.assertFalse(MODULE.is_pptx(malformed))

    def test_pptx_zip_limits_reject_suspicious_compression_ratio(self):
        with tempfile.TemporaryDirectory() as name:
            archive_path = Path(name) / "compressed.zip"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as writer:
                writer.writestr("huge.xml", b"A" * (2 * 1024 * 1024))
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaisesRegex(MODULE.ExportError, "suspiciously compressed"):
                    MODULE.validate_zip_limits(
                        archive,
                        max_entries=10,
                        max_total_bytes=4 * 1024 * 1024,
                        max_part_bytes=4 * 1024 * 1024,
                        max_compression_ratio=200,
                        label="test",
                    )

    def test_rollback_preserves_output_replaced_by_external_writer(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            first_source = root / "first-source.pptx"
            second_source = root / "second-source.pptx"
            first_output = root / "first.pptx"
            second_output = root / "second.pptx"
            first_source.write_bytes(b"first")
            second_source.write_bytes(b"second")
            real_rename = MODULE.atomic_rename_no_replace
            calls = 0

            def race(source, destination):
                nonlocal calls
                calls += 1
                if calls == 1:
                    real_rename(source, destination)
                    Path(destination).unlink()
                    Path(destination).write_bytes(b"concurrent-owner")
                    return
                raise FileExistsError(destination)

            with mock.patch.object(MODULE, "atomic_rename_no_replace", side_effect=race):
                with self.assertRaisesRegex(MODULE.ExportError, "preserved externally replaced"):
                    MODULE.commit_output_files(
                        [(first_source, first_output), (second_source, second_output)],
                        force=False,
                    )
            self.assertEqual(first_output.read_bytes(), b"concurrent-owner")

    def test_non_force_transaction_never_overwrites_racing_output(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            staged = root / "staged.pptx"
            output = root / "output.pptx"
            staged.write_bytes(b"new")

            def race(source, destination):
                Path(destination).write_bytes(b"racer")
                raise FileExistsError(destination)

            with mock.patch.object(MODULE, "atomic_rename_no_replace", side_effect=race):
                with self.assertRaisesRegex(MODULE.ExportError, "output appeared"):
                    MODULE.commit_output_files([(staged, output)], force=False)
            self.assertEqual(output.read_bytes(), b"racer")
            self.assertEqual(staged.read_bytes(), b"new")

    def test_force_transaction_refuses_racing_directory(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            staged = root / "staged.pptx"
            output = root / "output.pptx"
            staged.write_bytes(b"new")
            output.mkdir()
            valuable = output / "valuable.txt"
            valuable.write_text("valuable", encoding="utf-8")

            with self.assertRaisesRegex(MODULE.ExportError, "non-regular output"):
                MODULE.commit_output_files([(staged, output)], force=True)
            self.assertTrue(output.is_dir())
            self.assertEqual(valuable.read_text(encoding="utf-8"), "valuable")
            self.assertEqual(staged.read_bytes(), b"new")

    def test_assert_safe_output_rejects_project_and_non_pptx(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manifest = root / "deck.pptd"
            manifest.write_text("version: 2", encoding="utf-8")
            with self.assertRaises(MODULE.ExportError):
                MODULE.assert_safe_output_file(manifest, root)
            with self.assertRaises(MODULE.ExportError):
                MODULE.assert_safe_output_file(manifest, root / "notes.txt")

    def test_verify_output_rejects_slide_count_mismatch(self):
        with tempfile.TemporaryDirectory() as name:
            deck = Path(name) / "count.pptx"
            write_minimal_pptx(
                deck,
                slide_xml='<p:sld xmlns:p="urn:test"/>',
            )
            with self.assertRaisesRegex(MODULE.ExportError, "slide count mismatch"):
                MODULE.verify_output(
                    deck,
                    "none",
                    False,
                    allow_image_only=True,
                    expected_slides=2,
                )

    def test_atomic_replace_preserves_existing_output_on_copy_failure(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "source.pptx"
            output = root / "output.pptx"
            source.write_bytes(b"new")
            output.write_bytes(b"old")
            with mock.patch.object(MODULE.shutil, "copy2", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    MODULE.atomic_replace_file(source, output)
            self.assertEqual(output.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
