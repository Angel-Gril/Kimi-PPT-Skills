#!/usr/bin/env python3
"""Render PPTD pages through Kimi for image-based visual QA."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from browser_export import (
    atomic_rename_no_replace,
    BrowserExportError,
    collect_http_failure,
    ensure_distribution,
    ensure_playwright,
    exclusive_path_locks,
    launch_ephemeral_browser,
    open_export_dialog,
    remaining_timeout_ms,
    save_download_before_deadline,
    wait_for_editor,
)
from export_pptx import (
    HOST_TEMPLATE,
    ExportError,
    build_payload,
    find_manifest,
    log,
    serve,
)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
OVERVIEW_COLUMNS = 3
OVERVIEW_THUMB_WIDTH = 640
OVERVIEW_LABEL_HEIGHT = 32
OVERVIEW_GAP = 12
MAX_IMAGE_ZIP_ENTRIES = 2_000
MAX_IMAGE_FILE_BYTES = 80 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 800 * 1024 * 1024
MAX_IMAGE_COMPRESSION_RATIO = 250
MAX_IMAGE_PIXELS = 80_000_000


def ensure_pillow() -> Tuple[Any, Any, Any]:
    ensure_distribution(
        distribution="Pillow",
        import_name="PIL",
        minimum=(10,),
        maximum=(13,),
        requirement="Pillow>=10,<13",
        log=log,
    )
    from PIL import Image, ImageDraw, ImageFont

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    return Image, ImageDraw, ImageFont


def validate_image_zip(archive: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_IMAGE_ZIP_ENTRIES:
        raise ExportError("page-image ZIP contains too many entries")
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise ExportError("page-image ZIP contains duplicate entry names")
    selected: List[zipfile.ZipInfo] = []
    total = 0
    for info in infos:
        normalized = Path(info.filename.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ExportError("page-image ZIP contains an unsafe entry path")
        if info.is_dir() or normalized.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if info.file_size > MAX_IMAGE_FILE_BYTES:
            raise ExportError("page-image ZIP contains an oversized image")
        total += info.file_size
        if total > MAX_IMAGE_TOTAL_BYTES:
            raise ExportError("page-image ZIP exceeds the uncompressed size limit")
        compressed = max(1, info.compress_size)
        if info.file_size / compressed > MAX_IMAGE_COMPRESSION_RATIO:
            raise ExportError("page-image ZIP contains a suspicious compression ratio")
        selected.append(info)
    if not selected:
        raise ExportError("page-image ZIP contains no supported images")
    return selected


def is_image_zip(path: Path) -> bool:
    if not path.is_file() or path.name.endswith(".crdownload"):
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            validate_image_zip(archive)
            return True
    except (OSError, ExportError, zipfile.BadZipFile):
        return False


def page_sort_key(path: Path) -> Tuple[int, str]:
    match = re.match(r"(\d+)", path.stem)
    return (int(match.group(1)) if match else sys.maxsize, path.name)


def unzip_images(
    archive_path: Path, pages_dir: Path, *, expected_pages: Optional[int] = None
) -> List[Path]:
    pages_dir.mkdir(parents=True, exist_ok=True)
    images: List[Path] = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in validate_image_zip(archive):
            name = Path(info.filename).name
            if not name:
                continue
            target = pages_dir / name
            if target.exists():
                raise ExportError(f"duplicate page image name in ZIP: {name}")
            with archive.open(info) as source, target.open("wb") as out:
                shutil.copyfileobj(source, out)
            images.append(target)
    images.sort(key=page_sort_key)
    if not images:
        raise ExportError(f"no page images found in: {archive_path}")
    if expected_pages is not None and len(images) != expected_pages:
        raise ExportError(
            f"exported image count mismatch: expected {expected_pages}, "
            f"received {len(images)}"
        )
    return images


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def assert_safe_output_directory(manifest: Path, output: Path) -> None:
    project_root = manifest.parent.resolve()
    output = output.expanduser().resolve()
    if (
        output == project_root
        or _is_within(project_root, output)
        or _is_within(output, project_root)
    ):
        raise ExportError(
            "image QA output must be outside the entire PPTD project tree and must "
            "not contain the project"
        )
    if output.is_file():
        raise ExportError("image QA output path is an existing file")


def atomic_replace_directory(staged: Path, output: Path, *, force: bool) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    incoming = output.with_name(f".{output.name}.{uuid.uuid4().hex}.incoming")
    backup = output.with_name(f".{output.name}.{uuid.uuid4().hex}.backup")
    moved_old = False
    installed = False
    try:
        shutil.copytree(staged, incoming)
        if not force:
            try:
                atomic_rename_no_replace(incoming, output)
            except FileExistsError as exc:
                raise ExportError(
                    f"output appeared while exporting; refusing to overwrite: {output}"
                ) from exc
            installed = True
            return
        if output.exists():
            os.replace(output, backup)
            moved_old = True
        try:
            os.replace(incoming, output)
            installed = True
        except Exception as install_error:
            if moved_old and backup.exists() and not output.exists():
                try:
                    os.replace(backup, output)
                    moved_old = False
                except Exception as restore_error:
                    raise ExportError(
                        "image output replacement failed and rollback also failed; "
                        f"the previous output is preserved at: {backup}; "
                        f"replacement error: {install_error}; rollback error: {restore_error}"
                    ) from restore_error
            raise
        if moved_old and backup.exists():
            shutil.rmtree(backup)
            moved_old = False
    finally:
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
        if backup.exists() and not moved_old:
            shutil.rmtree(backup, ignore_errors=True)
        if installed and moved_old:
            log(f"warning: previous image output backup remains at: {backup}")


def label_font(image_font: Any) -> Any:
    try:
        return image_font.load_default(size=18)
    except TypeError:
        return image_font.load_default()


def stitch_overview(
    images: Sequence[Path],
    output: Path,
    image_cls: Any,
    draw_cls: Any,
    image_font: Any,
) -> Path:
    thumbs: List[Tuple[str, Any]] = []
    for index, path in enumerate(images, start=1):
        with image_cls.open(path) as opened:
            frame = opened.convert("RGB")
            ratio = OVERVIEW_THUMB_WIDTH / frame.width
            thumb = frame.resize(
                (OVERVIEW_THUMB_WIDTH, max(1, round(frame.height * ratio)))
            )
        thumbs.append((f"P{index}", thumb))

    columns = OVERVIEW_COLUMNS
    rows = math.ceil(len(thumbs) / columns)
    cell_height = OVERVIEW_LABEL_HEIGHT + max(thumb.height for _, thumb in thumbs)
    width = columns * OVERVIEW_THUMB_WIDTH + (columns + 1) * OVERVIEW_GAP
    height = rows * cell_height + (rows + 1) * OVERVIEW_GAP
    overview = image_cls.new("RGB", (width, height), "#e5e7eb")
    draw = draw_cls.Draw(overview)
    font = label_font(image_font)
    for position, (label, thumb) in enumerate(thumbs):
        column = position % columns
        row = position // columns
        x = OVERVIEW_GAP + column * (OVERVIEW_THUMB_WIDTH + OVERVIEW_GAP)
        y = OVERVIEW_GAP + row * (cell_height + OVERVIEW_GAP)
        draw.rectangle(
            (x, y, x + OVERVIEW_THUMB_WIDTH, y + OVERVIEW_LABEL_HEIGHT - 4),
            fill="#111827",
        )
        draw.text((x + 8, y + 5), label, fill="#ffffff", font=font)
        overview.paste(thumb, (x, y + OVERVIEW_LABEL_HEIGHT))
    overview.save(output, "JPEG", quality=88)
    return output


def _export_images_locked(
    source: Path,
    output: Path,
    keep_download: bool = False,
    force: bool = False,
    export_timeout: int = 900,
    headless: bool = True,
) -> Dict[str, Any]:
    manifest = find_manifest(source)
    payload = build_payload(manifest)
    expected_pages = len(payload["pages"])
    output = output.expanduser().resolve()
    assert_safe_output_directory(manifest, output)
    if output.exists() and not force:
        raise ExportError(
            f"output directory already exists (pass --force to replace it): {output}"
        )
    image_cls, draw_cls, image_font = ensure_pillow()
    sync_playwright = ensure_playwright(log)

    log(f"manifest: {manifest}")
    with tempfile.TemporaryDirectory(prefix="open-kimi-ppt-images-") as temp_name:
        temp_dir = Path(temp_name)
        raw_download = temp_dir / "browser-output.zip"
        shutil.copy2(HOST_TEMPLATE, temp_dir / HOST_TEMPLATE.name)
        (temp_dir / "payload.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        server, thread, url = serve(temp_dir)
        failures: List[Tuple[int, str]] = []
        deadline = time.monotonic() + export_timeout
        try:
            with sync_playwright() as playwright:
                browser, context = launch_ephemeral_browser(
                    playwright, headless=headless
                )
                page = context.new_page()
                page.on(
                    "response",
                    lambda response: collect_http_failure(response, failures),
                )
                try:
                    log("opening the Kimi slide editor for visual QA")
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=remaining_timeout_ms(
                            deadline, "opening the local image export host", 120_000
                        ),
                    )
                    frame = wait_for_editor(page, deadline=deadline)
                    download_button = open_export_dialog(
                        frame, "图片", deadline=deadline
                    )
                    log("rendering page images in the browser")
                    try:
                        with page.expect_download(
                            timeout=remaining_timeout_ms(
                                deadline, "rendering page images"
                            )
                        ) as download_info:
                            download_button.click(
                                timeout=remaining_timeout_ms(
                                    deadline, "starting image download", 30_000
                                )
                            )
                        save_download_before_deadline(
                            download_info.value,
                            raw_download,
                            deadline=deadline,
                            stage="saving the page image ZIP",
                        )
                    except Exception as exc:
                        recent = ", ".join(
                            f"HTTP {status} {failed_url}"
                            for status, failed_url in failures[-8:]
                        ) or "none"
                        raise ExportError(
                            "timed out waiting for the page image ZIP. "
                            f"Recent browser failures: {recent}"
                        ) from exc
                finally:
                    page.close()
                    context.close()
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        if not is_image_zip(raw_download):
            raise ExportError("Kimi download is not a valid page-image ZIP")
        staged_output = temp_dir / "verified-output"
        staged_output.mkdir()
        images = unzip_images(
            raw_download,
            staged_output / "pages",
            expected_pages=expected_pages,
        )
        if keep_download:
            shutil.copy2(raw_download, staged_output / "browser-raw.zip")
        overview = stitch_overview(
            images,
            staged_output / "overview.jpg",
            image_cls,
            draw_cls,
            image_font,
        )
        page_paths = [entry["path"] for entry in payload["pages"]]
        mapping = [
            {
                "index": index,
                "image": f"pages/{path.name}",
                "page": page_paths[index - 1],
            }
            for index, path in enumerate(images, start=1)
        ]
        committed_images = [output / "pages" / path.name for path in images]
        atomic_replace_directory(staged_output, output, force=force)

    return {
        "pages": len(committed_images),
        "overview": str(output / "overview.jpg"),
        "output": str(output),
        "images": mapping,
    }


def export_images(
    source: Path,
    output: Path,
    keep_download: bool = False,
    force: bool = False,
    export_timeout: int = 900,
    headless: bool = True,
) -> Dict[str, Any]:
    resolved_output = output.expanduser().resolve()
    with exclusive_path_locks([resolved_output]):
        return _export_images_locked(
            source,
            resolved_output,
            keep_download=keep_download,
            force=force,
            export_timeout=export_timeout,
            headless=headless,
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export PPTD pages and stitch an overview for visual QA."
    )
    parser.add_argument("input", type=Path, help=".pptd manifest or project directory")
    parser.add_argument(
        "--output", "-o", type=Path,
        help="output directory (default: sibling <project-name>.qa-images)",
    )
    parser.add_argument(
        "--keep-browser-raw",
        action="store_true",
        help="also keep the downloaded images ZIP beside the overview",
    )
    parser.add_argument(
        "--export-timeout",
        type=int,
        default=900,
        help="seconds to wait for editor loading and image rendering (default: 900)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the temporary browser used for image QA",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an existing output directory"
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        manifest = find_manifest(args.input)
        output = args.output or manifest.parent.with_name(
            f"{manifest.parent.name}.qa-images"
        )
        summary = export_images(
            args.input,
            output,
            args.keep_browser_raw,
            args.force,
            args.export_timeout,
            not args.headed,
        )
    except (
        ExportError,
        BrowserExportError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"open-kimi-ppt image export failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
