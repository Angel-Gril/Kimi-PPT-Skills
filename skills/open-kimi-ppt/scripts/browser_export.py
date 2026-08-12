#!/usr/bin/env python3
"""Shared Playwright automation for Kimi PPT exports.

Authentication data is read from a dedicated local browser profile at runtime. Tokens
are never printed, returned in summaries, or written into the PPTD project.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import hmac
import importlib
import importlib.metadata
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

KIMI_ORIGIN = "https://www.kimi.com"
EDITOR_URL_HINT = "/neo-ppt/"
DEFAULT_PROFILE_DIR = Path.home() / ".open-kimi-ppt" / "browser-profile"
SIGNATURE_PATH = "/apiv2/utils/v1/signatures"
PROFILE_MARKER = ".open-kimi-ppt-profile.json"
PROFILE_MARKER_SCHEMA = "open-kimi-ppt-export-profile/v2"
PROFILE_OWNER_KEY = Path.home() / ".open-kimi-ppt" / "profile-owner.key"


class BrowserExportError(RuntimeError):
    pass


def _numeric_version(value: str) -> Tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", value)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def ensure_distribution(
    *,
    distribution: str,
    import_name: str,
    minimum: Tuple[int, ...],
    maximum: Tuple[int, ...],
    requirement: str,
    log: Callable[[str], None],
) -> Any:
    """Import a supported dependency, installing the declared range when needed."""
    try:
        installed = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        installed = ""
    parsed = _numeric_version(installed)
    supported = bool(parsed) and parsed >= minimum and parsed < maximum
    if not supported:
        action = "installing" if not installed else f"updating unsupported {installed} to"
        log(f"{distribution} is required; {action} {requirement} with pip --user")
        process = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", requirement],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
        if process.returncode != 0:
            raise BrowserExportError(
                f"failed to install {requirement}:\n{process.stdout[-2000:]}\n"
                f"Install it manually with: python -m pip install --user \"{requirement}\""
            )
        importlib.invalidate_caches()
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise BrowserExportError(
                f"pip completed but {distribution} is still unavailable"
            ) from exc
        parsed = _numeric_version(installed)
        if not parsed or parsed < minimum or parsed >= maximum:
            raise BrowserExportError(
                f"installed {distribution} {installed}, outside supported range {requirement}"
            )
    try:
        return importlib.import_module(import_name)
    except ImportError as exc:
        raise BrowserExportError(
            f"{distribution} is installed but {import_name} could not be imported"
        ) from exc


def ensure_playwright(log: Callable[[str], None]) -> Any:
    ensure_distribution(
        distribution="playwright",
        import_name="playwright.sync_api",
        minimum=(1, 50),
        maximum=(2,),
        requirement="playwright>=1.50,<2",
        log=log,
    )
    from playwright.sync_api import sync_playwright

    return sync_playwright


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def everyday_browser_roots() -> List[Path]:
    """Known browser user-data roots that must never be automated directly."""
    home = Path.home()
    candidates: List[Path] = []
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        candidates.extend(
            [
                local / "Google" / "Chrome" / "User Data",
                local / "Chromium" / "User Data",
                local / "Microsoft" / "Edge" / "User Data",
            ]
        )
    elif sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        candidates.extend(
            [
                support / "Google" / "Chrome",
                support / "Chromium",
                support / "Microsoft Edge",
            ]
        )
    else:
        config = home / ".config"
        candidates.extend(
            [
                config / "google-chrome",
                config / "chromium",
                config / "microsoft-edge",
            ]
        )
    return [candidate.expanduser().resolve() for candidate in candidates]


def _windows_private_acl(path: Path, *, directory: bool) -> None:
    """Restrict a persistent auth path to the current Windows user and SYSTEM."""
    if os.name != "nt":
        return
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    user = user.strip("\\")
    if not user:
        raise BrowserExportError("could not determine the current Windows user for ACLs")
    grant = f"{user}:(OI)(CI)F" if directory else f"{user}:F"
    system_grant = "*S-1-5-18:(OI)(CI)F" if directory else "*S-1-5-18:F"
    process = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", grant, system_grant],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    if process.returncode != 0:
        raise BrowserExportError(
            "could not apply private Windows ACLs to the browser authentication path"
        )


def _assert_windows_profile_location(profile_dir: Path) -> None:
    """Reject persistent authentication under shared Windows profile roots."""
    if os.name != "nt":
        return
    user_profile = Path(os.environ.get("USERPROFILE", str(Path.home()))).resolve()
    if not _is_within(profile_dir.resolve(), user_profile):
        raise BrowserExportError(
            "on Windows, the Kimi export profile must be stored under the current "
            "user profile so private ACLs can be enforced"
        )
    public_value = os.environ.get("PUBLIC")
    if public_value:
        public = Path(public_value).resolve()
        if _is_within(profile_dir.resolve(), public):
            raise BrowserExportError(
                "the Kimi export profile cannot use a public directory"
            )


def validate_profile_dir(profile_dir: Path, forbidden_roots: List[Path]) -> Path:
    """Require an isolated profile outside projects, repositories, and daily profiles."""
    resolved = profile_dir.expanduser().resolve()
    _assert_windows_profile_location(resolved)
    for root in [*forbidden_roots, *everyday_browser_roots()]:
        root = root.expanduser().resolve()
        if _is_within(resolved, root) or _is_within(root, resolved):
            raise BrowserExportError(
                "the Kimi export profile must be isolated from the PPTD project, the "
                "skill repository, and everyday Chrome/Edge profiles. Use the default "
                "~/.open-kimi-ppt/browser-profile or another empty dedicated directory."
            )
    return resolved


def _profile_signature(profile_dir: Path, owner_key: bytes) -> str:
    payload = f"{PROFILE_MARKER_SCHEMA}\0{profile_dir}".encode("utf-8")
    return hmac.new(owner_key, payload, hashlib.sha256).hexdigest()


def _load_or_create_profile_owner_key() -> bytes:
    key_path = PROFILE_OWNER_KEY.expanduser().resolve()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        _windows_private_acl(key_path.parent, directory=True)
    try:
        descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            key = key_path.read_bytes()
        except OSError as exc:
            raise BrowserExportError("could not read the private profile owner key") from exc
        if len(key) != 32:
            raise BrowserExportError("the private profile owner key is invalid")
        return key
    key = secrets.token_bytes(32)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            key_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if os.name != "nt":
        key_path.parent.chmod(0o700)
        key_path.chmod(0o600)
    else:
        _windows_private_acl(key_path, directory=False)
    return key


def secure_profile_permissions(profile_dir: Path) -> None:
    """Create/claim a dedicated profile and reject unowned or forged profiles."""
    profile_dir = profile_dir.expanduser().resolve()
    marker = profile_dir / PROFILE_MARKER
    owner_key = _load_or_create_profile_owner_key()
    expected = {
        "schema": PROFILE_MARKER_SCHEMA,
        "signature": _profile_signature(profile_dir, owner_key),
    }
    if profile_dir.exists() and not profile_dir.is_dir():
        raise BrowserExportError("the Kimi export profile path is not a directory")
    if profile_dir.exists():
        entries = [entry for entry in profile_dir.iterdir() if entry.name != PROFILE_MARKER]
        if entries and not marker.is_file():
            raise BrowserExportError(
                "refusing to use an existing non-empty unmarked browser profile. "
                "Choose a new empty directory dedicated to open-kimi-ppt."
            )
    profile_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        _windows_private_acl(profile_dir, directory=True)
    if marker.exists():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BrowserExportError("the browser profile ownership marker is invalid") from exc
        if not isinstance(data, dict) or data.keys() != expected.keys() or not hmac.compare_digest(
            str(data.get("signature", "")), expected["signature"]
        ) or data.get("schema") != PROFILE_MARKER_SCHEMA:
            raise BrowserExportError("the browser profile ownership marker is not recognized")
    else:
        temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(expected, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, marker)
    if os.name != "nt":
        try:
            profile_dir.chmod(0o700)
            marker.chmod(0o600)
        except OSError as exc:
            raise BrowserExportError(
                "could not apply private permissions to the browser profile"
            ) from exc
    else:
        _windows_private_acl(marker, directory=False)


def redact_url(url: str) -> str:
    """Keep only an origin and allowlisted diagnostic path; remove userinfo and ports."""
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return "<invalid-url>"
        path = SIGNATURE_PATH if parts.path == SIGNATURE_PATH else "/<redacted>"
        return urlunsplit((parts.scheme, parts.hostname, path, "", ""))
    except ValueError:
        return "<invalid-url>"


def jwt_expiry(token: Optional[str]) -> int:
    if not token or token.count(".") < 2:
        return 0
    try:
        segment = token.split(".", 2)[1]
        padding = "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment + padding))
        return int(payload.get("exp") or 0)
    except (ValueError, TypeError, json.JSONDecodeError):
        return 0


def token_bundle(page: Any) -> Dict[str, str]:
    values = page.evaluate(
        """() => ({
          access: localStorage.getItem('access_token') || '',
          refresh: localStorage.getItem('refresh_token') || ''
        })"""
    )
    if not isinstance(values, dict):
        return {"access": "", "refresh": ""}
    return {
        "access": str(values.get("access") or ""),
        "refresh": str(values.get("refresh") or ""),
    }


def token_is_fresh(tokens: Dict[str, str], minimum_seconds: int = 300) -> bool:
    return bool(tokens.get("refresh")) and jwt_expiry(tokens.get("access")) >= (
        int(time.time()) + minimum_seconds
    )


def remaining_timeout_ms(deadline: float, stage: str, cap_ms: Optional[int] = None) -> int:
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise BrowserExportError(f"timed out while {stage}")
    return max(1, min(remaining, cap_ms)) if cap_ms else max(1, remaining)


def atomic_rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename while refusing to replace an existing destination."""
    source = source.resolve()
    destination = destination.resolve()
    if os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError:
            raise
        except OSError as exc:
            if destination.exists():
                raise FileExistsError(str(destination)) from exc
            raise
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise BrowserExportError("atomic no-replace rename is unavailable on this Linux")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            1,
        )
        if result != 0:
            error = ctypes.get_errno()
            if error == 17:
                raise FileExistsError(str(destination))
            raise OSError(error, os.strerror(error), str(destination))
        return
    if sys.platform == "darwin":
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise BrowserExportError("atomic no-replace rename is unavailable on this macOS")
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(os.fsencode(source), os.fsencode(destination), 0x00000004)
        if result != 0:
            error = ctypes.get_errno()
            if error == 17:
                raise FileExistsError(str(destination))
            raise OSError(error, os.strerror(error), str(destination))
        return
    raise BrowserExportError(
        "this platform lacks a supported atomic no-replace rename primitive"
    )


@contextmanager
def exclusive_path_locks(paths: List[Path]):
    """Serialize exporters targeting the same outputs with crash-safe OS locks."""
    acquired: List[Tuple[Path, int]] = []
    lock_paths = sorted(
        {
            path.expanduser().resolve().with_name(
                f".{path.expanduser().resolve().name}.open-kimi-ppt.lock"
            )
            for path in paths
        },
        key=lambda item: str(item).casefold(),
    )
    try:
        for lock_path in lock_paths:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"\0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as exc:
                os.close(descriptor)
                raise BrowserExportError(
                    f"another export is already targeting: {lock_path.name.removeprefix('.').removesuffix('.open-kimi-ppt.lock')}"
                ) from exc
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            acquired.append((lock_path, descriptor))
        yield
    finally:
        for _, descriptor in reversed(acquired):
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                os.close(descriptor)


def launch_persistent_context(
    playwright: Any,
    profile_dir: Path,
    *,
    headless: bool,
    viewport: Tuple[int, int] = (1280, 720),
) -> Any:
    profile_dir = profile_dir.expanduser().resolve()
    secure_profile_permissions(profile_dir)
    errors: List[str] = []
    attempts = [("chrome", "chrome"), ("edge", "msedge"), ("chromium", None)]
    for label, channel in attempts:
        try:
            kwargs: Dict[str, Any] = {
                "headless": headless,
                "accept_downloads": True,
                "viewport": {"width": viewport[0], "height": viewport[1]},
                "args": ["--disable-extensions"],
            }
            if channel:
                kwargs["channel"] = channel
            return playwright.chromium.launch_persistent_context(
                str(profile_dir), **kwargs
            )
        except Exception as exc:  # Playwright uses generated exception classes.
            errors.append(f"{label}: {type(exc).__name__}")
    raise BrowserExportError(
        "could not launch Chrome, Edge, or Playwright Chromium with the dedicated "
        "export profile. Install a supported browser, close any process using the "
        "profile, and retry. Attempts: " + ", ".join(errors)
    )


def launch_ephemeral_browser(playwright: Any, *, headless: bool) -> Tuple[Any, Any]:
    errors: List[str] = []
    attempts = [("chrome", "chrome"), ("edge", "msedge"), ("chromium", None)]
    for label, channel in attempts:
        browser = None
        try:
            kwargs: Dict[str, Any] = {
                "headless": headless,
                "args": ["--disable-extensions"],
            }
            if channel:
                kwargs["channel"] = channel
            browser = playwright.chromium.launch(**kwargs)
            context = browser.new_context(
                accept_downloads=True,
                viewport={"width": 1280, "height": 720},
            )
            return browser, context
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}")
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
    raise BrowserExportError(
        "could not launch Chrome, Edge, or Playwright Chromium for image export. "
        "Attempts: " + ", ".join(errors)
    )


def ensure_kimi_authentication(
    context: Any,
    *,
    timeout_seconds: int,
    headless: bool,
    log: Callable[[str], None],
) -> Dict[str, str]:
    """Return a fresh in-memory token bundle from a top-level Kimi page."""
    page = context.new_page()
    deadline = time.monotonic() + timeout_seconds
    try:
        try:
            page.goto(
                KIMI_ORIGIN,
                wait_until="domcontentloaded",
                timeout=remaining_timeout_ms(deadline, "opening Kimi login", 120_000),
            )
            page.wait_for_timeout(min(3_000, remaining_timeout_ms(deadline, "checking login")))
            tokens = token_bundle(page)
            if token_is_fresh(tokens):
                log("Kimi authentication is available in the dedicated browser profile")
                return tokens

            if headless:
                raise BrowserExportError(
                    "Kimi login is required, but --headless was selected. Run once without "
                    "--headless, sign in in the dedicated browser window, then retry."
                )

            log(
                "Kimi login is required. Complete sign-in in the dedicated browser window; "
                f"waiting up to {timeout_seconds} seconds"
            )
            last_reload = 0.0
            while time.monotonic() < deadline:
                page.wait_for_timeout(min(1_000, remaining_timeout_ms(deadline, "waiting for Kimi login")))
                tokens = token_bundle(page)
                if token_is_fresh(tokens):
                    log("Kimi login confirmed")
                    return tokens
                if tokens.get("refresh") and time.monotonic() - last_reload > 15:
                    last_reload = time.monotonic()
                    page.reload(
                        wait_until="domcontentloaded",
                        timeout=remaining_timeout_ms(deadline, "refreshing Kimi login", 120_000),
                    )
                    page.wait_for_timeout(
                        min(3_000, remaining_timeout_ms(deadline, "checking refreshed login"))
                    )
        except BrowserExportError:
            raise
        except Exception as exc:
            raise BrowserExportError("failed while checking Kimi authentication") from exc
        raise BrowserExportError(
            "timed out waiting for Kimi login. Keep the dedicated login window open "
            "until the exporter confirms authentication."
        )
    finally:
        try:
            page.close()
        except Exception:
            pass


def inject_partitioned_auth(context: Any, tokens: Dict[str, str]) -> None:
    """Seed Kimi localStorage before the cross-site editor iframe initializes."""
    if not token_is_fresh(tokens, minimum_seconds=30):
        raise BrowserExportError("Kimi access token is missing or expired")
    literal = json.dumps(tokens, ensure_ascii=True)
    context.add_init_script(
        script=f"""
          (() => {{
            if (location.hostname !== 'www.kimi.com') return;
            const tokens = {literal};
            localStorage.setItem('access_token', tokens.access);
            localStorage.setItem('refresh_token', tokens.refresh);
          }})();
        """
    )


def save_download_before_deadline(
    download: Any,
    destination: Path,
    *,
    deadline: float,
    stage: str,
) -> Path:
    """Save a Playwright download without allowing artifact transfer to outlive the deadline."""
    timeout_seconds = max(0.001, remaining_timeout_ms(deadline, stage) / 1000)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        impl = getattr(download, "_impl_obj", None)
        sync = getattr(download, "_sync", None)
        if impl is None or sync is None or not hasattr(impl, "save_as"):
            raise BrowserExportError(
                "the installed Playwright version does not expose a cancellable download "
                "artifact; install a supported Playwright release and retry"
            )
        sync(asyncio.wait_for(impl.save_as(destination), timeout=timeout_seconds))
    except BrowserExportError:
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise BrowserExportError(f"timed out while {stage}") from exc
    except Exception as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise BrowserExportError(f"failed while {stage}") from exc
    if not destination.is_file() or destination.stat().st_size == 0:
        raise BrowserExportError(f"download produced no file while {stage}")
    return destination


def collect_http_failure(response: Any, failures: List[Tuple[int, str]]) -> None:
    if response.status >= 400:
        failures.append((int(response.status), redact_url(response.url)))


def signature_unauthorized(failures: List[Tuple[int, str]]) -> bool:
    return any(status == 401 and SIGNATURE_PATH in url for status, url in failures)


def wait_for_editor(page: Any, *, deadline: float) -> Any:
    try:
        page.wait_for_function(
            "['ready', 'error'].includes(document.documentElement.dataset.deckStatus)",
            timeout=remaining_timeout_ms(deadline, "loading the Kimi editor"),
        )
        status = page.evaluate("document.documentElement.dataset.deckStatus")
        if status == "error":
            raise BrowserExportError(
                "Kimi editor reported an initialization error; validate the PPTD and retry"
            )
        for frame in page.frames:
            if EDITOR_URL_HINT in frame.url:
                return frame
    except BrowserExportError:
        raise
    except Exception as exc:
        raise BrowserExportError("failed while waiting for the Kimi editor") from exc
    raise BrowserExportError("Kimi editor iframe did not become available")


def open_export_dialog(frame: Any, format_name: str, *, deadline: float) -> Any:
    try:
        frame.get_by_role("button", name="导出", exact=True).click(
            timeout=remaining_timeout_ms(deadline, "opening the export dialog", 30_000)
        )
        download = frame.get_by_role("button", name="下载", exact=True)
        download.wait_for(
            state="visible",
            timeout=remaining_timeout_ms(deadline, "waiting for the export dialog", 30_000),
        )
        options = frame.locator(".radio-group-item").filter(has_text=format_name)
        if options.count() == 0:
            raise BrowserExportError(
                f"Kimi export dialog has no {format_name!r} option; the editor UI may have changed"
            )
        options.first.click(
            timeout=remaining_timeout_ms(deadline, "selecting the export format", 30_000)
        )
        return download
    except BrowserExportError:
        raise
    except Exception as exc:
        raise BrowserExportError("failed while operating the Kimi export dialog") from exc


def configure_font_embedding(
    frame: Any,
    enabled: bool,
    log: Callable[[str], None],
    *,
    deadline: float,
) -> None:
    try:
        switches = frame.get_by_role("switch")
        if switches.count() == 0:
            if enabled:
                log("warning: Kimi export dialog exposed no font embedding switch")
            return
        switch = switches.first
        checked = switch.get_attribute("aria-checked") == "true"
        disabled = switch.is_disabled()
        if disabled:
            if checked != enabled:
                log("warning: Kimi font embedding switch is disabled for this deck")
            return
        if checked != enabled:
            switch.click(
                timeout=remaining_timeout_ms(deadline, "configuring font embedding", 30_000)
            )
    except BrowserExportError:
        raise
    except Exception as exc:
        raise BrowserExportError("failed while configuring font embedding") from exc
