#!/usr/bin/env python3
import importlib.util
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

SPEC = importlib.util.spec_from_file_location("export_images", SCRIPTS_DIR / "export_images.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def make_images_zip(path: Path, names=("1.jpeg", "10.jpeg", "2.jpeg")) -> None:
    # 1x1 white JPEG, the smallest valid payload Pillow can open.
    import base64

    pixel = base64.b64decode(
        "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////"
        "////////////////////////////////////////////2wBDAf//////////////////"
        "////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB"
        "/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMB"
        "AAIQAxAAAAGf/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAA"
        "AAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgB"
        "AgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAA"
        "AAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABCf/8QAFBEBAAAAAAAAAAAA"
        "AAAAAAAAAP/aAAgBAwEPEBB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEPEBB/"
        "/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxB//9k="
    )
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, pixel)


class ExportImagesTests(unittest.TestCase):
    def test_page_sort_key_orders_numeric_stems(self):
        paths = [Path("10.jpeg"), Path("2.jpeg"), Path("cover.jpeg"), Path("1.jpeg")]
        ordered = sorted(paths, key=MODULE.page_sort_key)
        self.assertEqual(
            [path.name for path in ordered],
            ["1.jpeg", "2.jpeg", "10.jpeg", "cover.jpeg"],
        )

    def test_is_image_zip_accepts_image_entries_only(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            good = root / "images.zip"
            make_images_zip(good)
            self.assertTrue(MODULE.is_image_zip(good))

            bad = root / "text.zip"
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr("readme.txt", "hello")
            self.assertFalse(MODULE.is_image_zip(bad))
            self.assertFalse(MODULE.is_image_zip(root / "missing.zip"))

    def test_validate_image_zip_rejects_suspicious_compression_ratio(self):
        with tempfile.TemporaryDirectory() as name:
            archive_path = Path(name) / "bomb.zip"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("1.png", b"\0" * 1_000_000)
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaisesRegex(MODULE.ExportError, "compression ratio"):
                    MODULE.validate_image_zip(archive)

    def test_unzip_images_flattens_sorts_and_checks_count(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive_path = root / "images.zip"
            make_images_zip(archive_path, names=("1.jpeg", "10.jpeg", "2.jpeg", "note.txt"))
            images = MODULE.unzip_images(
                archive_path, root / "pages", expected_pages=3
            )
            self.assertEqual(
                [path.name for path in images], ["1.jpeg", "2.jpeg", "10.jpeg"]
            )
            with self.assertRaisesRegex(MODULE.ExportError, "image count mismatch"):
                MODULE.unzip_images(
                    archive_path, root / "other-pages", expected_pages=2
                )

    def test_assert_safe_output_rejects_project_ancestor_and_project_itself(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            project = root / "project"
            project.mkdir()
            manifest = project / "deck.pptd"
            manifest.write_text("version: 2", encoding="utf-8")
            for unsafe in (project, root, project / ".qa-images", project / "pages"):
                with self.assertRaises(MODULE.ExportError):
                    MODULE.assert_safe_output_directory(manifest, unsafe)
            MODULE.assert_safe_output_directory(
                manifest, project.with_name(f"{project.name}.qa-images")
            )

    def test_atomic_directory_replace_preserves_old_output_on_copy_failure(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            staged = root / "staged"
            output = root / "output"
            staged.mkdir()
            output.mkdir()
            (staged / "new.txt").write_text("new", encoding="utf-8")
            (output / "old.txt").write_text("old", encoding="utf-8")
            with mock.patch.object(MODULE.shutil, "copytree", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    MODULE.atomic_replace_directory(staged, output, force=True)
            self.assertEqual((output / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertFalse(any(root.glob(".*.incoming")))

    def test_atomic_directory_replace_preserves_backup_if_rollback_fails(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            staged = root / "staged"
            output = root / "output"
            staged.mkdir()
            output.mkdir()
            (staged / "new.txt").write_text("new", encoding="utf-8")
            (output / "old.txt").write_text("old", encoding="utf-8")
            real_replace = MODULE.os.replace
            calls = []

            def fail_install_and_restore(source, destination):
                calls.append((Path(source), Path(destination)))
                if len(calls) == 1:
                    return real_replace(source, destination)
                raise OSError(f"replace failure {len(calls)}")

            with mock.patch.object(MODULE.os, "replace", side_effect=fail_install_and_restore):
                with self.assertRaisesRegex(MODULE.ExportError, "rollback also failed") as caught:
                    MODULE.atomic_replace_directory(staged, output, force=True)
            backups = list(root.glob(".output.*.backup"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertIn(str(backups[0]), str(caught.exception))

    def test_stitch_overview_grid(self):
        try:
            image_cls, draw_cls, image_font = MODULE.ensure_pillow()
        except MODULE.ExportError:
            self.skipTest("Pillow is not available")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            images = []
            for index in range(1, 5):
                path = root / f"{index}.jpeg"
                image = image_cls.new("RGB", (320, 180), (index * 40 % 255, 30, 60))
                image.save(path, "JPEG")
                images.append(path)
            overview = MODULE.stitch_overview(
                images, root / "overview.jpg", image_cls, draw_cls, image_font
            )
            self.assertTrue(overview.is_file())
            with image_cls.open(overview) as result:
                self.assertEqual(
                    result.width,
                    3 * MODULE.OVERVIEW_THUMB_WIDTH + 4 * MODULE.OVERVIEW_GAP,
                )
                rows = 2
                cell = MODULE.OVERVIEW_LABEL_HEIGHT + 360
                self.assertEqual(result.height, rows * cell + (rows + 1) * MODULE.OVERVIEW_GAP)


if __name__ == "__main__":
    unittest.main()
