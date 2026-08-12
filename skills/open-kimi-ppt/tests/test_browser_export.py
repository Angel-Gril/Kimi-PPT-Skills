#!/usr/bin/env python3
import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("browser_export", SCRIPTS / "browser_export.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class BrowserExportTests(unittest.TestCase):
    def test_redact_url_removes_query_fragment_userinfo_and_port(self):
        value = MODULE.redact_url(
            "https://user:password@www.kimi.com:8443/apiv2/utils/v1/signatures?token=secret#fragment"
        )
        self.assertEqual(value, "https://www.kimi.com/apiv2/utils/v1/signatures")
        self.assertNotIn("secret", value)
        self.assertNotIn("password", value)
        other = MODULE.redact_url("https://api.example.com/private/account?id=42")
        self.assertEqual(other, "https://api.example.com/<redacted>")

    def test_jwt_expiry_parses_without_logging_token(self):
        import base64
        import json

        payload = base64.urlsafe_b64encode(json.dumps({"exp": 424242}).encode()).decode().rstrip("=")
        token = f"header.{payload}.signature"
        self.assertEqual(MODULE.jwt_expiry(token), 424242)

    def test_signature_unauthorized_detects_exact_failure(self):
        failures = [
            (404, "https://www.kimi.com/other"),
            (401, "https://www.kimi.com/apiv2/utils/v1/signatures"),
        ]
        self.assertTrue(MODULE.signature_unauthorized(failures))
        self.assertFalse(MODULE.signature_unauthorized([(401, "https://example.com/")]))

    def test_default_profile_is_not_in_repository(self):
        profile = MODULE.DEFAULT_PROFILE_DIR.expanduser().resolve()
        repository = Path(__file__).resolve().parents[3]
        self.assertFalse(profile == repository or repository in profile.parents)

    def test_validate_profile_rejects_project_and_daily_browser(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            project = root / "project"
            project.mkdir()
            with self.assertRaises(MODULE.BrowserExportError):
                MODULE.validate_profile_dir(project / "profile", [project])
            daily = root / "Chrome" / "User Data"
            daily.mkdir(parents=True)
            with mock.patch.object(MODULE, "everyday_browser_roots", return_value=[daily]):
                with self.assertRaises(MODULE.BrowserExportError):
                    MODULE.validate_profile_dir(daily / "Default", [])

    def test_profile_marker_is_path_bound_and_unmarked_profiles_are_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            owner_key = root / "owner.key"
            first = root / "first-profile"
            second = root / "second-profile"
            with mock.patch.object(MODULE, "PROFILE_OWNER_KEY", owner_key):
                MODULE.secure_profile_permissions(first)
                marker = first / MODULE.PROFILE_MARKER
                self.assertTrue(marker.is_file())

                second.mkdir()
                (second / MODULE.PROFILE_MARKER).write_bytes(marker.read_bytes())
                with self.assertRaisesRegex(
                    MODULE.BrowserExportError, "ownership marker"
                ):
                    MODULE.secure_profile_permissions(second)

                unmarked = root / "ordinary-profile"
                unmarked.mkdir()
                (unmarked / "Local State").write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(MODULE.BrowserExportError, "unmarked"):
                    MODULE.secure_profile_permissions(unmarked)

    def test_output_lock_ignores_stale_file_and_rejects_active_holder(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / "deck.pptx"
            lock_path = output.with_name(".deck.pptx.open-kimi-ppt.lock")
            lock_path.write_text("pid=999999\n", encoding="ascii")
            with MODULE.exclusive_path_locks([output]):
                with self.assertRaisesRegex(
                    MODULE.BrowserExportError, "another export"
                ):
                    with MODULE.exclusive_path_locks([output]):
                        pass
            with MODULE.exclusive_path_locks([output]):
                pass

    def test_remaining_timeout_uses_global_deadline(self):
        deadline = time.monotonic() + 0.2
        first = MODULE.remaining_timeout_ms(deadline, "test")
        time.sleep(0.05)
        second = MODULE.remaining_timeout_ms(deadline, "test")
        self.assertLess(second, first)
        with self.assertRaisesRegex(MODULE.BrowserExportError, "timed out"):
            MODULE.remaining_timeout_ms(time.monotonic() - 1, "expired")

    def test_save_download_obeys_deadline_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "download.tmp"

            class Impl:
                async def save_as(self, path):
                    Path(path).write_bytes(b"partial")
                    await __import__("asyncio").sleep(10)

            class Download:
                _impl_obj = Impl()

                @staticmethod
                def _sync(coro):
                    return __import__("asyncio").run(coro)

            started = time.monotonic()
            with self.assertRaisesRegex(MODULE.BrowserExportError, "timed out"):
                MODULE.save_download_before_deadline(
                    Download(),
                    destination,
                    deadline=time.monotonic() + 0.05,
                    stage="saving test download",
                )
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(destination.exists())

    def test_wait_for_editor_fails_immediately_on_error_status(self):
        page = mock.Mock()
        page.evaluate.return_value = "error"
        with self.assertRaisesRegex(MODULE.BrowserExportError, "initialization error"):
            MODULE.wait_for_editor(page, deadline=time.monotonic() + 5)
        page.wait_for_function.assert_called_once()


if __name__ == "__main__":
    unittest.main()
