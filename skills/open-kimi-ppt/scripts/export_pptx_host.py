#!/usr/bin/env python3
"""Export a frozen host-generated PPTX through Kimi's anonymous SDK channel.

--auto-generate uses a limited text/solid-background/fade reference renderer.
Use export_pptx.py for the official writer, images, shapes and element animations.
The remote editor still requires network access; no account or saved profile is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Sequence

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
    serve,
    validate_pptx_relationship_graph,
)


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
        with tempfile.TemporaryDirectory(prefix="open-kimi-ppt-host-export-") as temp_name:
            temp_dir = Path(temp_name)
            artifact = temp_dir / "deck.pptx"
            if args.auto_generate:
                from pptd_to_pptx import build

                log("reference renderer: text and solid backgrounds only; images, shapes, charts, "
                    "element animations, rich-text run styling and font embedding are omitted")
                data, info = build(manifest)
                artifact.write_bytes(data)
                log(f"rendered {info['slides']} slide(s), {info['bytes']} bytes")
            else:
                source = Path(args.pptx).expanduser().resolve()
                if not source.is_file():
                    raise ExportError(f"host artifact not found: {source}")
                shutil.copy2(source, artifact)
            validate_artifact(artifact, total_pages)
            expected_digest = hashlib.sha256(artifact.read_bytes()).digest()

            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise ExportError("playwright is required: pip install -r skills/open-kimi-ppt/requirements.txt") from exc

            shutil.copy2(HOST_TEMPLATE, temp_dir / HOST_TEMPLATE.name)
            (temp_dir / "payload.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            server, thread, url = serve(temp_dir)
            downloaded = temp_dir / "download.pptx"
            deadline = time.monotonic() + args.timeout
            try:
                with sync_playwright() as playwright:
                    browser, context = launch_ephemeral_browser(playwright, headless=not args.headed)
                    try:
                        page = context.new_page()
                        page.goto(f"{url}?hostExport=1", wait_until="domcontentloaded",
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
            if hashlib.sha256(downloaded.read_bytes()).digest() != expected_digest:
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
    source.add_argument("--auto-generate", action="store_true", help="use the limited reference renderer")
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
