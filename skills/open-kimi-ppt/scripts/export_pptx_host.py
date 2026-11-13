#!/usr/bin/env python3
"""Export a host-rendered PPTX through Kimi's anonymous SDK channel.

--auto-generate renders the deck the editor currently holds (every autosave is
forwarded to the host), so edits made in the browser are part of the export.
--pptx hands back a fixed artifact instead; edits are then ignored by design.

The reference renderer covers text, solid backgrounds, fade transitions and page
order only. Use export_pptx.py for the official writer, images, shapes, charts
and element animations. The remote editor still needs network access; no account
or saved browser profile is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple, Type

from browser_export import (
    BrowserExportError,
    exclusive_path_locks,
    launch_ephemeral_browser,
    open_export_dialog,
    remaining_timeout_ms,
    save_download_before_deadline,
    wait_for_editor,
)
from export_pptx import (
    ExportError,
    HOST_TEMPLATE,
    assert_safe_output_file,
    atomic_replace_file,
    build_payload,
    find_manifest,
    log,
    validate_pptx_relationship_graph,
)

RENDER_PATH = "/render-pptx"
MAX_RENDER_BODY = 32 * 1024 * 1024


def validate_artifact(path: Path, expected_slides: int) -> None:
    """Check package integrity and page count without claiming official fidelity."""
    try:
        with zipfile.ZipFile(path) as archive:
            validate_pptx_relationship_graph(archive, expected_slides=expected_slides)
            broken = archive.testzip()
            if broken:
                raise ExportError(f"PPTX CRC check failed at: {broken}")
            for name in archive.namelist():
                if name.endswith((".xml", ".rels")):
                    ET.fromstring(archive.read(name))
    except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ExportError(f"not a valid host PPTX: {path}") from exc


class _HostHandler(SimpleHTTPRequestHandler):
    """Static host files plus a POST endpoint that renders the live deck."""

    render_state: Dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        if self.path.split("?")[0] != RENDER_PATH:
            self.send_error(404, "unknown endpoint")
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_RENDER_BODY:
            self.send_error(413, "unsupported render body size")
            return
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400, "render body must be JSON")
            return

        files = request.get("files")
        if isinstance(files, list):
            files = {
                str(item.get("path")): item.get("content", "")
                for item in files
                if isinstance(item, dict) and item.get("path")
            }
        from pptd_to_pptx import build_from_files

        try:
            data, info = build_from_files(files, str(request.get("title") or "presentation"))
        except ExportError as exc:
            self.send_error(400, str(exc))
            return

        self.render_state["digest"] = hashlib.sha256(data).digest()
        self.render_state["slides"] = info["slides"]
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", 'attachment; filename="deck.pptx"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # keep stdout clean
        pass


def serve_host(directory: Path, render_state: Dict[str, Any]) -> Tuple[ThreadingHTTPServer, threading.Thread, str]:
    handler: Type[SimpleHTTPRequestHandler] = type(
        "BoundHostHandler",
        (_HostHandler,),
        {"render_state": render_state},
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(handler, directory=str(directory))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, thread, f"http://{host}:{port}/export_host.html"


def serve(directory: Path, render_state: Optional[Dict[str, Any]] = None):
    """Backwards-compatible entry point; render_state collects live render digests."""
    return serve_host(directory, render_state if render_state is not None else {})


def export_host(args: argparse.Namespace) -> Path:
    manifest = find_manifest(Path(args.project))
    output = Path(args.output).expanduser().resolve()
    assert_safe_output_file(manifest, output)
    if args.timeout <= 0:
        raise ExportError("--timeout must be positive")
    with exclusive_path_locks([output]):
        if output.exists() and not args.force:
            raise ExportError(f"output already exists (pass --force to replace it): {output}")
        payload = build_payload(manifest)
        total_pages = len(payload["pages"])
        render_state: Dict[str, Any] = {}
        with tempfile.TemporaryDirectory(prefix="open-kimi-ppt-host-export-") as temp_name:
            temp_dir = Path(temp_name)
            artifact = temp_dir / "deck.pptx"
            if args.auto_generate:
                from pptd_to_pptx import build

                log("reference renderer: text and solid backgrounds only; images, shapes, charts, "
                    "element animations, rich-text run styling and font embedding are omitted")
                log("live mode: the deck is re-rendered from the editor's latest autosave")
                data, info = build(manifest)
                artifact.write_bytes(data)
                log(f"initial render {info['slides']} slide(s), {info['bytes']} bytes")
            else:
                source = Path(args.pptx).expanduser().resolve()
                if not source.is_file():
                    raise ExportError(f"host artifact not found: {source}")
                shutil.copy2(source, artifact)
                log("fixed artifact mode: edits made in the editor are not exported")
            validate_artifact(artifact, total_pages)

            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise ExportError("playwright is required: pip install -r skills/open-kimi-ppt/requirements.txt") from exc

            shutil.copy2(HOST_TEMPLATE, temp_dir / HOST_TEMPLATE.name)
            (temp_dir / "payload.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            server, thread, url = serve(temp_dir, render_state)
            downloaded = temp_dir / "download.pptx"
            deadline = time.monotonic() + args.timeout
            query = "?hostExport=1&render=1" if args.auto_generate else "?hostExport=1&render=0"
            try:
                with sync_playwright() as playwright:
                    browser, context = launch_ephemeral_browser(playwright, headless=not args.headed)
                    try:
                        page = context.new_page()
                        page.goto(f"{url}{query}", wait_until="domcontentloaded",
                                  timeout=remaining_timeout_ms(deadline, "opening the export host", 60_000))
                        frame = wait_for_editor(page, deadline=deadline)
                        log(f"anonymous editor ready, {total_pages} page(s)")
                        button = open_export_dialog(frame, "PPT", deadline=deadline)
                        try:
                            with page.expect_download(timeout=remaining_timeout_ms(deadline, "downloading host PPTX")) as info:
                                button.click(timeout=remaining_timeout_ms(deadline, "starting host download", 30_000))
                        except Exception as exc:
                            raise ExportError("host download failed; check network access and Kimi SDK compatibility") from exc
                        save_download_before_deadline(info.value, downloaded, deadline=deadline,
                                                      stage="saving the host PPTX")
                    finally:
                        try:
                            context.close()
                        finally:
                            browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            if args.auto_generate:
                digest = render_state.get("digest")
                if not digest:
                    raise ExportError(
                        "the editor never saved before export, so no live deck was rendered; "
                        "open the deck once or use --pptx"
                    )
                if hashlib.sha256(downloaded.read_bytes()).digest() != digest:
                    raise ExportError("downloaded PPTX does not match the live render")
                validate_artifact(downloaded, int(render_state.get("slides") or total_pages))
            else:
                if hashlib.sha256(downloaded.read_bytes()).digest() != hashlib.sha256(artifact.read_bytes()).digest():
                    raise ExportError("downloaded PPTX does not match the host artifact")
                validate_artifact(downloaded, total_pages)
            atomic_replace_file(downloaded, output, force=args.force)
    log(f"saved {output} ({output.stat().st_size} bytes)")
    return output


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export a host-generated PPTX without a Kimi account (network required)")
    parser.add_argument("--project", required=True, help="PPTD manifest or project directory")
    parser.add_argument("-o", "--output", required=True, help="destination .pptx path")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pptx", help="pre-rendered PPTX with the same page count as the project")
    source.add_argument("--auto-generate", action="store_true",
                        help="render the editor's latest content with the limited reference renderer")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--timeout", type=int, default=180, help="total browser timeout in seconds")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    args = parser.parse_args(argv)
    try:
        export_host(args)
    except (ExportError, BrowserExportError, OSError) as exc:
        parser.exit(1, f"host export failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
