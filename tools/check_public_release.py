"""Fail a release if tracked files contain common private artifacts or tokens.

This is a guardrail, not a replacement for manual review or GitHub secret scanning.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PREFIXES = (
    "user_data/", "data/", "logs/", ".claude/", ".venv", "venv/",
    "frontend/node_modules/", "frontend/dist/",
)
PRIVATE_SUFFIXES = (
    ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3",
    ".session", ".log", ".err", ".pem", ".key", ".p12", ".pfx",
)
TOKEN_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{35,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
PERSONAL_PATH = re.compile(r"(?i)C:[\\/]Users[\\/][^\\/\s]+")


def main() -> int:
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    paths = [part.decode("utf-8") for part in raw.split(b"\0") if part]
    findings: list[str] = []
    for rel in paths:
        normalized = rel.replace("\\", "/")
        name = Path(normalized).name.lower()
        if (normalized.startswith(PRIVATE_PREFIXES)
                or name == ".env" or (name.startswith(".env.") and name != ".env.example")
                or name == "config.json" or name.endswith(PRIVATE_SUFFIXES)):
            findings.append(f"{rel}: private path")
            continue
        path = ROOT / rel
        if path.stat().st_size > 2_000_000:
            findings.append(f"{rel}: oversized file requires review")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            findings.append(f"{rel}: binary file requires review")
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if PERSONAL_PATH.search(line):
                findings.append(f"{rel}:{line_number}: personal absolute path")
            if any(pattern.search(line) for pattern in TOKEN_PATTERNS):
                findings.append(f"{rel}:{line_number}: possible credential")
    for finding in findings:
        print(finding)
    print(f"Checked {len(paths)} tracked files; findings: {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
