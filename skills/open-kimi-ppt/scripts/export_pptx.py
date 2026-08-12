#!/usr/bin/env python3
"""Export an editable PPTX through Kimi's browser-side writer.

The exporter uses a dedicated persistent Chrome/Edge profile. Authentication stays
inside that local browser profile and process memory; tokens are never logged or
written into the PPTD project. Referenced remote resources may still be fetched by
the official editor. Local image files are supplied to the iframe as data URLs.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from browser_export import (
    atomic_rename_no_replace,
    BrowserExportError,
    DEFAULT_PROFILE_DIR,
    collect_http_failure,
    configure_font_embedding,
    ensure_kimi_authentication,
    ensure_playwright,
    exclusive_path_locks,
    inject_partitioned_auth,
    launch_persistent_context,
    open_export_dialog,
    remaining_timeout_ms,
    save_download_before_deadline,
    signature_unauthorized,
    validate_profile_dir,
    wait_for_editor,
)

SKILL_DIR = Path(__file__).resolve().parent.parent
HOST_TEMPLATE = Path(__file__).with_name("export_host.html")
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_EMBEDDED_MEDIA_BYTES = 200 * 1024 * 1024
MAX_PPTX_ENTRIES = 20_000
MAX_PPTX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_PPTX_PART_BYTES = 64 * 1024 * 1024
MAX_PPTX_COMPRESSION_RATIO = 200
PPTX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
)
PPTX_SLIDE_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
)
OFFICE_DOCUMENT_REL_TYPES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument",
}
SLIDE_REL_TYPES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/slide",
}
CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
FADE_TRANSITION_XML = (
    '<p:transition spd="fast" advClick="1"><p:fade/></p:transition>'
)


class ExportError(RuntimeError):
    pass


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: Any) -> None:
        return


def log(message: str) -> None:
    print(f"[open-kimi-ppt] {message}", file=sys.stderr, flush=True)


def ensure_pyyaml() -> Any:
    try:
        import yaml
    except ImportError:
        log("PyYAML is required; installing pyyaml with pip --user")
        process = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "pyyaml"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
        if process.returncode != 0:
            raise ExportError(
                "failed to install PyYAML with pip --user:\n"
                f"{process.stdout[-2000:]}\n"
                "Install it manually with: python3 -m pip install --user pyyaml"
            )
        import yaml
    return yaml


yaml = ensure_pyyaml()


def find_manifest(source: Path) -> Path:
    source = source.expanduser().resolve()
    if source.is_file():
        if source.suffix.lower() != ".pptd":
            raise ExportError(f"input must be a .pptd file or project directory: {source}")
        return source
    if not source.is_dir():
        raise ExportError(f"input does not exist: {source}")
    manifests = sorted(source.rglob("*.pptd"))
    if not manifests:
        raise ExportError(f"no .pptd manifest found under: {source}")
    if len(manifests) > 1:
        choices = "\n  ".join(str(path) for path in manifests[:20])
        raise ExportError(
            "multiple .pptd manifests found; pass one manifest explicitly:\n  " + choices
        )
    return manifests[0]


def read_yaml_mapping(path: Path) -> Tuple[str, Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ExportError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExportError(f"expected a YAML mapping in {path}")
    return text, value


def safe_project_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise ExportError("page path must be a non-empty string")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ExportError(f"project path escapes the PPTD directory: {relative}") from exc
    return candidate


def iter_local_image_sources(value: Any) -> Sequence[str]:
    """Yield local image src values from parsed manifest/page structures."""
    found: List[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "src" and isinstance(child, str):
                    source = child.strip()
                    suffix = Path(source.split("?", 1)[0]).suffix.lower()
                    if suffix in IMAGE_MIME and not re.match(
                        r"^(?:[a-z][a-z0-9+.-]*:|//)", source, re.IGNORECASE
                    ):
                        found.append(source)
                else:
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def build_image_map(root: Path, documents: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    image_map: Dict[str, str] = {}
    total = 0
    references = sorted(
        {source for document in documents for source in iter_local_image_sources(document)}
    )
    for reference in references:
        path = safe_project_path(root, reference)
        if not path.is_file():
            raise ExportError(f"missing referenced local image: {reference}")
        suffix = path.suffix.lower()
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise ExportError(f"referenced local image exceeds 20 MiB: {reference}")
        if total + size > MAX_EMBEDDED_MEDIA_BYTES:
            raise ExportError(
                "referenced local image payload exceeds 200 MiB; reduce media size "
                "or use remote URLs"
            )
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        normalized = path.relative_to(root).as_posix()
        data_url = f"data:{IMAGE_MIME[suffix]};base64,{data}"
        image_map[reference.replace("\\", "/")] = data_url
        image_map[normalized] = data_url
        total += size
    if references:
        log(f"prepared {len(references)} referenced local image resource(s), {total} bytes")
    return image_map


def build_payload(manifest: Path) -> Dict[str, Any]:
    manifest_text, manifest_data = read_yaml_mapping(manifest)
    if manifest_data.get("version") != "v2":
        raise ExportError("local PPTX export currently requires PPTD version: v2")
    page_paths = manifest_data.get("pages")
    if not isinstance(page_paths, list) or not page_paths:
        raise ExportError("PPTD manifest must contain a non-empty pages list")

    root = manifest.parent.resolve()
    pages: List[Dict[str, str]] = []
    page_documents: List[Dict[str, Any]] = []
    for entry in page_paths:
        page_path = safe_project_path(root, entry)
        if not page_path.is_file():
            raise ExportError(f"missing page file: {entry}")
        page_text, page_data = read_yaml_mapping(page_path)
        if not isinstance(page_data.get("elements"), list):
            raise ExportError(f"page elements must be an array: {entry}")
        pages.append({"path": str(entry), "content": page_text})
        page_documents.append(page_data)

    title = str(manifest_data.get("title") or manifest.stem)
    return {
        "id": f"local-export-{uuid.uuid4().hex}",
        "title": title,
        "manifestPath": manifest.name,
        "manifestContent": manifest_text,
        "pages": pages,
        "imageMap": build_image_map(root, [manifest_data, *page_documents]),
    }


def validate_zip_limits(
    archive: zipfile.ZipFile,
    *,
    max_entries: int,
    max_total_bytes: int,
    max_part_bytes: int,
    max_compression_ratio: int,
    label: str,
) -> None:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(infos) > max_entries:
        raise ExportError(f"{label} contains too many ZIP entries")
    if len(names) != len(set(names)):
        raise ExportError(f"{label} contains duplicate ZIP entry names")
    total = 0
    for info in infos:
        normalized = posixpath.normpath(info.filename.replace("\\", "/"))
        if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
            raise ExportError(f"{label} contains an unsafe ZIP entry path")
        if info.file_size > max_part_bytes:
            raise ExportError(f"{label} contains an oversized ZIP entry")
        if info.file_size:
            if info.compress_size <= 0:
                raise ExportError(f"{label} contains an invalid compressed ZIP entry")
            if info.file_size / info.compress_size > max_compression_ratio:
                raise ExportError(f"{label} contains a suspiciously compressed ZIP entry")
        total += info.file_size
        if total > max_total_bytes:
            raise ExportError(f"{label} exceeds the uncompressed size limit")


def _relationship_part_name(source_part: str) -> str:
    directory, filename = posixpath.split(source_part)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _resolve_relationship_target(source_part: str, target: str) -> str:
    if not target or target.startswith("/"):
        resolved = target.lstrip("/")
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))
    if not resolved or resolved == ".." or resolved.startswith("../"):
        raise ExportError("PPTX relationship escapes the package root")
    return resolved


def validate_pptx_relationship_graph(
    archive: zipfile.ZipFile,
    *,
    expected_slides: Optional[int] = None,
) -> List[str]:
    """Return presentation-order slide parts after validating all internal relationships."""
    validate_zip_limits(
        archive,
        max_entries=MAX_PPTX_ENTRIES,
        max_total_bytes=MAX_PPTX_UNCOMPRESSED_BYTES,
        max_part_bytes=MAX_PPTX_PART_BYTES,
        max_compression_ratio=MAX_PPTX_COMPRESSION_RATIO,
        label="PPTX",
    )
    names = set(archive.namelist())
    required = {
        "[Content_Types].xml",
        "_rels/.rels",
        "ppt/presentation.xml",
        "ppt/_rels/presentation.xml.rels",
    }
    missing = sorted(required - names)
    if missing:
        raise ExportError(f"PPTX is missing required part(s): {', '.join(missing)}")

    try:
        content_types_root = ET.fromstring(archive.read("[Content_Types].xml"))
    except ET.ParseError as exc:
        raise ExportError("invalid [Content_Types].xml") from exc
    expected_types_tag = f"{{{CONTENT_TYPES_NAMESPACE}}}Types"
    if content_types_root.tag != expected_types_tag:
        raise ExportError("invalid [Content_Types].xml root element")
    overrides: Dict[str, str] = {}
    for element in content_types_root:
        if element.tag != f"{{{CONTENT_TYPES_NAMESPACE}}}Override":
            continue
        part_name = element.attrib.get("PartName", "")
        content_type = element.attrib.get("ContentType", "")
        if not part_name.startswith("/") or not content_type:
            raise ExportError("invalid content-type override")
        normalized = posixpath.normpath(part_name.lstrip("/"))
        if normalized in overrides:
            raise ExportError("duplicate content-type override")
        overrides[normalized] = content_type
    if overrides.get("ppt/presentation.xml") != PPTX_CONTENT_TYPE:
        raise ExportError("PPTX presentation content type is missing or invalid")

    relationships_by_part: Dict[str, Dict[str, str]] = {}
    relationship_types_by_part: Dict[str, Dict[str, str]] = {}
    for rels_name in sorted(name for name in names if name.endswith(".rels")):
        if "/_rels/" in rels_name:
            prefix, rel_file = rels_name.rsplit("/_rels/", 1)
            source_part = posixpath.join(prefix, rel_file[:-5])
        elif rels_name == "_rels/.rels":
            source_part = ""
        else:
            continue
        try:
            root = ET.fromstring(archive.read(rels_name))
        except ET.ParseError as exc:
            raise ExportError(f"invalid relationship XML: {rels_name}") from exc
        ids: Dict[str, str] = {}
        types: Dict[str, str] = {}
        for relationship in root:
            if relationship.tag.rsplit("}", 1)[-1] != "Relationship":
                continue
            relationship_id = relationship.attrib.get("Id", "")
            relationship_type = relationship.attrib.get("Type", "")
            if not relationship_id or relationship_id in ids or not relationship_type:
                raise ExportError(
                    f"duplicate, missing, or untyped relationship in: {rels_name}"
                )
            types[relationship_id] = relationship_type
            if relationship.attrib.get("TargetMode") == "External":
                ids[relationship_id] = ""
                continue
            target = _resolve_relationship_target(
                source_part, relationship.attrib.get("Target", "")
            )
            if target not in names:
                raise ExportError(
                    f"broken PPTX relationship target: {rels_name} -> {target}"
                )
            ids[relationship_id] = target
        relationships_by_part[source_part] = ids
        relationship_types_by_part[source_part] = types

    root_relationships = relationships_by_part.get("", {})
    root_relationship_types = relationship_types_by_part.get("", {})
    office_document_ids = [
        relationship_id
        for relationship_id, relationship_type in root_relationship_types.items()
        if relationship_type in OFFICE_DOCUMENT_REL_TYPES
    ]
    if len(office_document_ids) != 1:
        raise ExportError("PPTX must contain exactly one package office-document relationship")
    if root_relationships.get(office_document_ids[0]) != "ppt/presentation.xml":
        raise ExportError("package office-document relationship targets the wrong part")

    presentation_rels = relationships_by_part.get("ppt/presentation.xml", {})
    presentation_rel_types = relationship_types_by_part.get(
        "ppt/presentation.xml", {}
    )
    try:
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
    except ET.ParseError as exc:
        raise ExportError("invalid ppt/presentation.xml") from exc
    slide_parts: List[str] = []
    relationship_namespace = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    numeric_ids = set()
    for element in presentation.iter():
        if element.tag.rsplit("}", 1)[-1] != "sldId":
            continue
        numeric_id = element.attrib.get("id", "")
        relationship_id = element.attrib.get(f"{{{relationship_namespace}}}id", "")
        if not numeric_id or numeric_id in numeric_ids:
            raise ExportError("presentation contains duplicate or missing slide IDs")
        numeric_ids.add(numeric_id)
        target = presentation_rels.get(relationship_id)
        relationship_type = presentation_rel_types.get(relationship_id)
        if (
            not relationship_id
            or not target
            or relationship_type not in SLIDE_REL_TYPES
            or not re.fullmatch(r"ppt/slides/slide\d+\.xml", target)
        ):
            raise ExportError("presentation contains a missing or invalid slide relationship")
        if overrides.get(target) != PPTX_SLIDE_CONTENT_TYPE:
            raise ExportError(f"slide content type is missing or invalid: {target}")
        slide_parts.append(target)
    if not slide_parts or len(slide_parts) != len(set(slide_parts)):
        raise ExportError("presentation contains no slides or duplicate slide targets")
    packaged_slide_parts = {
        name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
    }
    if set(slide_parts) != packaged_slide_parts:
        raise ExportError("PPTX contains missing or orphaned slide parts")
    if expected_slides is not None and len(slide_parts) != expected_slides:
        raise ExportError(
            f"exported slide count mismatch: expected {expected_slides}, "
            f"received {len(slide_parts)}"
        )
    return slide_parts


def is_pptx(path: Path) -> bool:
    if not path.is_file() or path.name.endswith(".crdownload"):
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            validate_pptx_relationship_graph(archive)
            return True
    except (OSError, KeyError, ET.ParseError, ExportError, zipfile.BadZipFile):
        return False


def replace_transition(slide_xml: bytes, transition: str) -> bytes:
    text = slide_xml.decode("utf-8")
    pattern = re.compile(
        r"<p:transition\b[^>]*(?:/>|>.*?</p:transition>)", re.DOTALL
    )
    text = pattern.sub("", text)
    if transition == "none":
        return text.encode("utf-8")

    # CT_Slide requires transition as a direct child after cSld/clrMapOvr and
    # before timing/extLst. Searching for the first p:extLst is incorrect:
    # shapes may contain their own nested extLst inside cSld, causing Office to
    # ignore a transition inserted there.
    color_map = re.search(
        r"<p:clrMapOvr\b[^>]*(?:/>|>.*?</p:clrMapOvr>)", text, re.DOTALL
    )
    common_slide = re.search(
        r"<p:cSld\b[^>]*(?:/>|>.*?</p:cSld>)", text, re.DOTALL
    )
    anchor = color_map or common_slide
    if anchor is None:
        raise ExportError("slide XML has no cSld/clrMapOvr insertion anchor")
    position = anchor.end()
    return (text[:position] + FADE_TRANSITION_XML + text[position:]).encode("utf-8")


def root_child_names(slide_xml: bytes) -> List[str]:
    try:
        root = ET.fromstring(slide_xml)
    except ET.ParseError as exc:
        raise ExportError(f"invalid slide XML: {exc}") from exc
    return [child.tag.rsplit("}", 1)[-1] for child in root]


def has_direct_fade_transition(slide_xml: bytes) -> bool:
    try:
        root = ET.fromstring(slide_xml)
    except ET.ParseError as exc:
        raise ExportError(f"invalid slide XML: {exc}") from exc
    transition = next(
        (child for child in root if child.tag.rsplit("}", 1)[-1] == "transition"),
        None,
    )
    if transition is None:
        return False
    return any(child.tag.rsplit("}", 1)[-1] == "fade" for child in transition)


def validate_transition_order(slide_xml: bytes, transition: str) -> None:
    names = root_child_names(slide_xml)
    transition_indexes = [index for index, name in enumerate(names) if name == "transition"]
    if transition == "none":
        if transition_indexes:
            raise ExportError("transition=none left a root-level transition")
        return
    if len(transition_indexes) != 1 or not has_direct_fade_transition(slide_xml):
        raise ExportError("slide does not contain exactly one root-level fade transition")
    transition_index = transition_indexes[0]
    for required_before in ("cSld", "clrMapOvr"):
        if required_before in names and names.index(required_before) > transition_index:
            raise ExportError(f"{required_before} appears after transition")
    for required_after in ("timing", "extLst"):
        if required_after in names and names.index(required_after) < transition_index:
            raise ExportError(f"{required_after} appears before transition")


def patch_transitions(pptx: Path, transition: str) -> int:
    temporary = pptx.with_name(f".{pptx.name}.{uuid.uuid4().hex}.tmp")
    slide_count = 0
    try:
        with zipfile.ZipFile(pptx, "r") as source, zipfile.ZipFile(temporary, "w") as target:
            slide_parts = set(validate_pptx_relationship_graph(source))
            target.comment = source.comment
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename in slide_parts:
                    data = replace_transition(data, transition)
                    slide_count += 1
                target.writestr(info, data, compress_type=info.compress_type)
        if slide_count == 0:
            raise ExportError("exported PPTX contains no slide XML")
        temporary.replace(pptx)
    finally:
        temporary.unlink(missing_ok=True)
    return slide_count


def inspect_editability(pptx: Path) -> Dict[str, int]:
    """Count independently editable slide objects without exposing their content."""
    counts = {
        "shapes": 0,
        "textObjects": 0,
        "pictures": 0,
        "charts": 0,
        "tables": 0,
    }
    with zipfile.ZipFile(pptx) as archive:
        slide_names = validate_pptx_relationship_graph(archive)
        for name in slide_names:
            root = ET.fromstring(archive.read(name))
            for element in root.iter():
                local = element.tag.rsplit("}", 1)[-1]
                if local in {"sp", "cxnSp", "graphicFrame"}:
                    counts["shapes"] += 1
                    if any(
                        child.tag.rsplit("}", 1)[-1] == "t"
                        and bool((child.text or "").strip())
                        for child in element.iter()
                    ):
                        counts["textObjects"] += 1
                elif local == "pic":
                    counts["pictures"] += 1
                elif local == "chart":
                    counts["charts"] += 1
                elif local == "tbl":
                    counts["tables"] += 1
    return counts


def verify_editability(pptx: Path, allow_image_only: bool = False) -> Dict[str, int]:
    counts = inspect_editability(pptx)
    editable = counts["shapes"] + counts["charts"] + counts["tables"]
    if not allow_image_only and editable == 0:
        raise ExportError(
            "the exported PPTX contains no editable shapes, text objects, charts, or "
            "tables. It appears to be an image-only fallback; pass --allow-image-only "
            "only when that output is intentional."
        )
    return counts


def assert_safe_output_file(manifest: Path, output: Path) -> None:
    root = manifest.parent.resolve()
    output = output.expanduser().resolve()
    if output == manifest.resolve() or output.suffix.lower() != ".pptx":
        raise ExportError("PPTX output must be a distinct .pptx file")
    if output.is_dir():
        raise ExportError("PPTX output path is an existing directory")
    if output == root:
        raise ExportError("PPTX output must not replace the PPTD project directory")


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _path_identity(path: Path) -> Tuple[int, int, int, int]:
    """Return a replacement-sensitive identity for a regular output file."""
    stat = path.stat(follow_symlinks=False)
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def commit_output_files(
    sources: List[Tuple[Path, Path]],
    *,
    force: bool,
) -> None:
    """Commit one or more files with no-overwrite semantics and force-mode rollback."""
    if not sources:
        return
    destinations = [destination.expanduser().resolve() for _, destination in sources]
    if len(destinations) != len(set(destinations)):
        raise ExportError("output destinations must be distinct")
    staged: List[Tuple[Path, Path]] = []
    backups: Dict[Path, Path] = {}
    committed: Dict[Path, Tuple[int, int, int, int]] = {}
    try:
        for source, destination in zip((item[0] for item in sources), destinations):
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            shutil.copy2(source, stage)
            staged.append((stage, destination))

        if not force:
            for stage, destination in staged:
                expected_identity = _path_identity(stage)
                try:
                    atomic_rename_no_replace(stage, destination)
                except FileExistsError as exc:
                    raise ExportError(
                        f"output appeared while exporting; refusing to overwrite: {destination}"
                    ) from exc
                except OSError as exc:
                    raise ExportError(
                        "could not atomically create the output without overwrite; "
                        "retry with --force only if replacement is intentional"
                    ) from exc
                committed[destination] = expected_identity
            return

        for _, destination in staged:
            if os.path.lexists(destination):
                if destination.is_symlink() or not destination.is_file():
                    raise ExportError(
                        "refusing to replace a non-regular output path, even with "
                        f"--force: {destination}"
                    )
                backup = destination.with_name(
                    f".{destination.name}.open-kimi-ppt-backup.{uuid.uuid4().hex}"
                )
                os.replace(destination, backup)
                backups[destination] = backup
        for stage, destination in staged:
            expected_identity = _path_identity(stage)
            os.replace(stage, destination)
            committed[destination] = expected_identity
    except Exception as exc:
        recovery_errors: List[str] = []
        externally_replaced: set[Path] = set()
        for destination, identity in reversed(list(committed.items())):
            try:
                if os.path.lexists(destination) and (
                    destination.is_symlink()
                    or not destination.is_file()
                    or _path_identity(destination) != identity
                ):
                    externally_replaced.add(destination)
                    recovery_errors.append(
                        f"preserved externally replaced output: {destination}"
                    )
                    continue
                _remove_path(destination)
            except OSError as recovery_exc:
                recovery_errors.append(f"remove {destination}: {recovery_exc}")
        for destination, backup in reversed(list(backups.items())):
            if not backup.exists():
                continue
            if destination in externally_replaced:
                recovery_errors.append(
                    f"preserved previous backup after concurrent replacement: {backup}"
                )
                continue
            try:
                if os.path.lexists(destination):
                    recovery_errors.append(
                        f"refused to overwrite path created during rollback: {destination}; "
                        f"previous backup preserved at {backup}"
                    )
                    continue
                os.replace(backup, destination)
            except OSError as recovery_exc:
                recovery_errors.append(
                    f"restore {destination} from preserved backup {backup}: {recovery_exc}"
                )
        if recovery_errors:
            raise ExportError(
                f"output commit failed: {exc}. Recovery also failed: "
                + "; ".join(recovery_errors)
            ) from exc
        raise
    else:
        cleanup_failures: List[str] = []
        for destination, backup in list(backups.items()):
            try:
                _remove_path(backup)
                backups.pop(destination, None)
            except OSError as cleanup_exc:
                cleanup_failures.append(f"{backup}: {cleanup_exc}")
        if cleanup_failures:
            log(
                "warning: output committed, but previous backup cleanup failed; "
                "preserved backup(s): " + "; ".join(cleanup_failures)
            )
    finally:
        for stage, _ in staged:
            stage.unlink(missing_ok=True)


def atomic_replace_file(source: Path, output: Path, *, force: bool = True) -> None:
    commit_output_files([(source, output)], force=force)


def verify_output(
    pptx: Path,
    transition: str,
    expect_fonts: bool,
    allow_image_only: bool = False,
    expected_slides: Optional[int] = None,
) -> Dict[str, Any]:
    if not is_pptx(pptx):
        raise ExportError(f"output is not a valid PPTX ZIP: {pptx}")
    with zipfile.ZipFile(pptx) as archive:
        broken = archive.testzip()
        if broken:
            raise ExportError(f"PPTX CRC check failed at: {broken}")
        slide_names = validate_pptx_relationship_graph(
            archive, expected_slides=expected_slides
        )
        slide_xml = {name: archive.read(name) for name in slide_names}
        for data in slide_xml.values():
            validate_transition_order(data, transition)
        transition_hits = sum(has_direct_fade_transition(data) for data in slide_xml.values())
        if transition == "fade" and transition_hits != len(slide_names):
            raise ExportError("fade transition was not written to every slide")
        fonts = [
            name
            for name in archive.namelist()
            if name.startswith("ppt/fonts/") and not name.endswith("/")
        ]
        if expect_fonts and not fonts:
            log(
                "warning: embed-fonts was enabled, but the official writer produced no font part"
            )
        summary = {
            "slides": len(slide_names),
            "fadeTransitions": transition_hits,
            "fontParts": len(fonts),
            "bytes": pptx.stat().st_size,
        }
    summary.update(verify_editability(pptx, allow_image_only))
    return summary


def serve(directory: Path) -> Tuple[ThreadingHTTPServer, threading.Thread, str]:
    handler = lambda *args, **kwargs: QuietHandler(  # noqa: E731
        *args, directory=str(directory), **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}/export_host.html"


def _export_pptx_locked(
    source: Path,
    output: Path,
    transition: str,
    embed_fonts: bool,
    keep_download: bool = False,
    force: bool = False,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    login_timeout: int = 600,
    export_timeout: int = 900,
    headless: bool = False,
    allow_image_only: bool = False,
) -> Dict[str, Any]:
    manifest = find_manifest(source)
    payload = build_payload(manifest)
    expected_slides = len(payload["pages"])
    output = output.expanduser().resolve()
    assert_safe_output_file(manifest, output)
    if output.exists() and not force:
        raise ExportError(f"output already exists (pass --force to replace it): {output}")
    debug_copy = output.with_name(f"{output.stem}.browser-raw.pptx")
    if keep_download and debug_copy.exists() and not force:
        raise ExportError(f"raw debug output already exists (pass --force): {debug_copy}")

    log(f"manifest: {manifest}")
    log(
        f"defaults: transition={transition}, embed_fonts={'on' if embed_fonts else 'off'}"
    )
    sync_playwright = ensure_playwright(log)
    skill_repository = SKILL_DIR.parents[1]
    profile_dir = validate_profile_dir(
        profile_dir,
        [manifest.parent.resolve(), skill_repository.resolve()],
    )

    with tempfile.TemporaryDirectory(prefix="open-kimi-ppt-export-") as temp_name:
        temp_dir = Path(temp_name)
        raw_download = temp_dir / "browser-output.pptx"
        shutil.copy2(HOST_TEMPLATE, temp_dir / HOST_TEMPLATE.name)
        (temp_dir / "payload.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        server, thread, url = serve(temp_dir)
        failures: List[Tuple[int, str]] = []
        try:
            with sync_playwright() as playwright:
                context = launch_persistent_context(
                    playwright,
                    profile_dir,
                    headless=headless,
                )
                try:
                    tokens = ensure_kimi_authentication(
                        context,
                        timeout_seconds=login_timeout,
                        headless=headless,
                        log=log,
                    )
                    inject_partitioned_auth(context, tokens)
                    export_deadline = time.monotonic() + export_timeout
                    page = context.new_page()
                    page.on(
                        "response",
                        lambda response: collect_http_failure(response, failures),
                    )
                    try:
                        log("opening the Kimi slide editor")
                        page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=remaining_timeout_ms(
                                export_deadline, "opening the local export host", 120_000
                            ),
                        )
                        frame = wait_for_editor(page, deadline=export_deadline)
                        download_button = open_export_dialog(
                            frame, "PPT", deadline=export_deadline
                        )
                        configure_font_embedding(
                            frame, embed_fonts, log, deadline=export_deadline
                        )
                        log("generating editable PPTX in the browser")
                        try:
                            with page.expect_download(
                                timeout=remaining_timeout_ms(
                                    export_deadline, "generating the editable PPTX"
                                )
                            ) as download_info:
                                download_button.click(
                                    timeout=remaining_timeout_ms(
                                        export_deadline, "starting the PPTX download", 30_000
                                    )
                                )
                            download = download_info.value
                            save_download_before_deadline(
                                download,
                                raw_download,
                                deadline=export_deadline,
                                stage="saving the PPTX download",
                            )
                        except Exception as exc:
                            if signature_unauthorized(failures):
                                raise ExportError(
                                    "Kimi rejected the export signature request (HTTP 401). "
                                    "The dedicated browser login expired or partitioned "
                                    "authentication could not be initialized; retry and sign "
                                    "in when prompted."
                                ) from exc
                            recent = ", ".join(
                                f"HTTP {status} {url}" for status, url in failures[-8:]
                            ) or "none"
                            raise ExportError(
                                "timed out waiting for the editable PPTX download. "
                                f"Recent browser failures: {recent}"
                            ) from exc
                    finally:
                        page.close()
                finally:
                    context.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        if not is_pptx(raw_download):
            raise ExportError("Kimi download is not a valid PPTX")
        unpatched_download = temp_dir / "unpatched-browser-output.pptx"
        if keep_download:
            shutil.copy2(raw_download, unpatched_download)
        slide_count = patch_transitions(raw_download, transition)
        summary = verify_output(
            raw_download,
            transition,
            embed_fonts,
            allow_image_only,
            expected_slides=expected_slides,
        )
        outputs_to_commit = [(raw_download, output)]
        if keep_download:
            outputs_to_commit.append((unpatched_download, debug_copy))
        commit_output_files(outputs_to_commit, force=force)

    summary["transitionPatchedSlides"] = slide_count
    summary["output"] = str(output)
    return summary


def export_pptx(
    source: Path,
    output: Path,
    transition: str,
    embed_fonts: bool,
    keep_download: bool = False,
    force: bool = False,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    login_timeout: int = 600,
    export_timeout: int = 900,
    headless: bool = False,
    allow_image_only: bool = False,
) -> Dict[str, Any]:
    resolved_output = output.expanduser().resolve()
    debug_copy = resolved_output.with_name(f"{resolved_output.stem}.browser-raw.pptx")
    lock_targets = [resolved_output]
    if keep_download:
        lock_targets.append(debug_copy)
    with exclusive_path_locks(lock_targets):
        return _export_pptx_locked(
            source,
            resolved_output,
            transition,
            embed_fonts,
            keep_download=keep_download,
            force=force,
            profile_dir=profile_dir,
            login_timeout=login_timeout,
            export_timeout=export_timeout,
            headless=headless,
            allow_image_only=allow_image_only,
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export a PPTD project to PPTX using Kimi's public browser-side writer. "
            "Defaults: fade transition and embedded fonts."
        )
    )
    parser.add_argument("input", type=Path, help=".pptd manifest or project directory")
    parser.add_argument("--output", "-o", type=Path, help="output .pptx path")
    parser.add_argument(
        "--transition",
        choices=("fade", "none"),
        default="fade",
        help="slide transition written to every slide (default: fade)",
    )
    font_group = parser.add_mutually_exclusive_group()
    font_group.add_argument(
        "--embed-fonts",
        dest="embed_fonts",
        action="store_true",
        default=True,
        help="embed fonts when available (default)",
    )
    font_group.add_argument(
        "--no-embed-fonts",
        dest="embed_fonts",
        action="store_false",
        help="disable font embedding",
    )
    parser.add_argument(
        "--keep-browser-raw",
        action="store_true",
        help="also keep the unpatched browser download beside the output",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help=(
            "dedicated Chrome/Edge profile used only for Kimi export login "
            "(default: ~/.open-kimi-ppt/browser-profile)"
        ),
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=600,
        help="seconds to wait for first-time Kimi sign-in (default: 600)",
    )
    parser.add_argument(
        "--export-timeout",
        type=int,
        default=900,
        help="seconds to wait for editor loading and PPTX generation (default: 900)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run without a visible browser; requires an existing fresh login profile",
    )
    parser.add_argument(
        "--allow-image-only",
        action="store_true",
        help="accept a PPTX with no editable shapes/text/charts (not recommended)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        manifest = find_manifest(args.input)
        output = args.output or manifest.with_suffix(".pptx")
        summary = export_pptx(
            args.input,
            output,
            args.transition,
            args.embed_fonts,
            args.keep_browser_raw,
            args.force,
            args.profile_dir,
            args.login_timeout,
            args.export_timeout,
            args.headless,
            args.allow_image_only,
        )
    except (
        ExportError,
        BrowserExportError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"open-kimi-ppt export failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
