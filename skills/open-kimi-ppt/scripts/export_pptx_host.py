#!/usr/bin/env python3
"""Host-side PPTX export for open-kimi-ppt (no Kimi account required).

The Kimi neo-ppt editor delegates PPTX export to its embedding host whenever the
iframe is opened with ``sdkExportMode=external`` and the host implements
``handleExport``. This script drives that channel:

    build payload.json -> serve it next to export_host.html -> open the host page
    with ?hostExport=1 -> click "导出 / PPT / 下载" -> save the downloaded file

No login, no dedicated browser profile, no /apiv2 signature round-trip: the PPTX
is whatever the host hands back (``--pptx`` or ``--auto-generate``).

Usage:
    python export_pptx_host.py --project dir/to/deck --auto-generate -o deck.pptx
    python export_pptx_host.py --project dir/to/deck --pptx frozen.pptx -o deck.pptx

Caveat: --auto-generate uses the bundled reference renderer (text, background,
fade). For official-grade output (embedded fonts, shapes, charts, animations)
use export_pptx.py with a signed-in profile instead.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
HOST_TEMPLATE = SCRIPT_DIR / "export_host.html"
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}


def log(message: str) -> None:
    print(f"[host-export] {message}", flush=True)


def find_manifest(project: Path) -> Path:
    candidates = sorted(p for p in project.glob("*.pptd") if p.is_file())
    if not candidates:
        raise SystemExit(f"no .pptd manifest found in {project}")
    if len(candidates) > 1:
        raise SystemExit(
            f"multiple .pptd manifests found in {project}: "
            + ", ".join(p.name for p in candidates)
        )
    return candidates[0]


def build_payload(manifest: Path) -> Dict[str, Any]:
    manifest_text = manifest.read_text(encoding="utf-8")
    root = manifest.parent
    pages: List[Dict[str, str]] = []
    for reference in re.findall(r"^\s{2,}-\s+(\S+\.page)\s*$", manifest_text, re.M):
        page_path = root / reference
        if not page_path.is_file():
            raise SystemExit(f"missing page file: {reference}")
        pages.append({"path": reference, "content": page_path.read_text(encoding="utf-8")})
    if not pages:
        raise SystemExit("PPTD manifest must list at least one page")

    image_map: Dict[str, str] = {}
    for candidate in sorted(root.rglob("*")):
        if candidate.is_file() and candidate.suffix.lower() in MEDIA_SUFFIXES:
            mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            encoded = base64.b64encode(candidate.read_bytes()).decode("ascii")
            image_map[candidate.relative_to(root).as_posix()] = f"data:{mime};base64,{encoded}"

    title = re.search(r"^title:\s*(.+)$", manifest_text, re.M)
    return {
        "id": f"host-export-{int(time.time() * 1000)}",
        "title": title.group(1).strip().strip("\"'") if title else manifest.stem,
        "manifestPath": manifest.name,
        "manifestContent": manifest_text,
        "pages": pages,
        "imageMap": image_map,
    }


def serve(directory: Path) -> tuple[ThreadingHTTPServer, str]:
    class Quiet(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, *args):  # silence per-request logging
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/export_host.html"


def launch(playwright: Any, headless: bool) -> Any:
    errors = []
    for channel in ("chrome", "msedge", None):
        try:
            kwargs: Dict[str, Any] = {"headless": headless, "args": ["--disable-extensions"]}
            if channel:
                kwargs["channel"] = channel
            browser = playwright.chromium.launch(**kwargs)
            return browser, browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 900})
        except Exception as exc:  # generated Playwright error types
            errors.append(f"{channel or 'chromium'}: {type(exc).__name__}")
    raise SystemExit("could not launch a Chromium build; tried " + ", ".join(errors))


def wait_ready(page: Any, timeout_ms: int) -> None:
    page.wait_for_function(
        "['ready','error'].includes(document.documentElement.dataset.deckStatus)",
        timeout=timeout_ms,
    )
    status = page.evaluate("document.documentElement.dataset.deckStatus")
    if status == "error":
        detail = page.evaluate("window.exportHostError || 'unknown error'")
        raise SystemExit(f"host page failed to initialise: {detail}")


def editor_frame(page: Any, timeout_ms: int) -> Any:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for frame in page.frames:
            if "/neo-ppt/" in frame.url:
                return frame
        page.wait_for_timeout(250)
    raise SystemExit("Kimi editor iframe did not appear")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export PPTX through the host-side channel (no login)")
    parser.add_argument("--project", required=True, help="PPTD project directory")
    parser.add_argument("-o", "--output", required=True, help="destination .pptx path")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pptx", help="pre-rendered PPTX handed back by the host")
    source.add_argument("--auto-generate", action="store_true", help="render with the bundled reference renderer")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--timeout", type=int, default=180, help="seconds to wait for the editor")
    args = parser.parse_args()

    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise SystemExit(f"project directory not found: {project}")
    manifest = find_manifest(project)
    payload = build_payload(manifest)
    total_pages = len(payload["pages"])

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("playwright is required: python -m pip install --user 'playwright>=1.50,<2'")

    with tempfile.TemporaryDirectory(prefix="open-kimi-ppt-host-export-") as temp_name:
        temp_dir = Path(temp_name)
        shutil.copy2(HOST_TEMPLATE, temp_dir / HOST_TEMPLATE.name)
        (temp_dir / "payload.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        if args.auto_generate:
            sys.path.insert(0, str(SCRIPT_DIR))
            import pptd_to_pptx  # noqa: WPS433 (local reference renderer)

            data, info = pptd_to_pptx.build(manifest)
            (temp_dir / "deck.pptx").write_bytes(data)
            log(f"rendered {info['slides']} slide(s) with {info['renderer']} ({info['bytes']} bytes)")
        else:
            shutil.copy2(Path(args.pptx).expanduser().resolve(), temp_dir / "deck.pptx")
            log(f"using host artifact: {args.pptx}")

        server, url = serve(temp_dir)
        try:
            with sync_playwright() as playwright:
                browser, context = launch(playwright, headless=not args.headed)
                page = context.new_page()
                page.goto(f"{url}?hostExport=1", wait_until="domcontentloaded", timeout=60_000)
                wait_ready(page, args.timeout * 1000)
                log(f"editor ready (anonymous), {total_pages} page(s) loaded")

                frame = editor_frame(page, args.timeout * 1000)
                frame.get_by_role("button", name="导出", exact=True).click(timeout=30_000)
                option = frame.locator(".radio-group-item").filter(has_text="PPT")
                option.first.wait_for(state="visible", timeout=30_000)
                option.first.click(timeout=30_000)
                button = frame.get_by_role("button", name="下载", exact=True)
                button.wait_for(state="visible", timeout=30_000)

                output = Path(args.output).expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                with page.expect_download(timeout=args.timeout * 1000) as info:
                    button.click(timeout=30_000)
                info.value.save_as(output)
                log(f"saved {output} ({output.stat().st_size} bytes)")
                context.close()
                browser.close()
        finally:
            server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
