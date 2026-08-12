#!/usr/bin/env python3
"""Fail when release inputs contain likely local secrets or private artifacts."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    "",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}
DENIED_SUFFIXES = {".ppt", ".pptx", ".p12", ".pfx", ".pem", ".key"}
PATTERNS = {
    "API key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "local Windows user path": re.compile(r"(?i)\b[A-Z]:[/\\]Users[/\\][^/\\\s]+"),
    "local presentation workspace": re.compile(r"(?i)\b[A-Z]:[/\\].*?(?:\.pptd|\.pptx|\.page)\b"),
}


def tracked_files() -> list[Path]:
    process = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return [ROOT / line for line in process.stdout.splitlines() if line]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        if path.suffix.lower() in DENIED_SUFFIXES:
            findings.append(f"forbidden release artifact: {relative}")
            continue
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{name}: {relative}:{line}")
    if findings:
        print("Sensitive-data scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print("Sensitive-data scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
