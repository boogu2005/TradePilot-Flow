from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .domain import ToolResult


class KeywordRunbook:
    def __init__(self, paths: list[Path], version: str):
        self.paths = paths
        self.version = version

    def search(self, query: str, limit: int = 5) -> ToolResult:
        terms = {term.lower() for term in re.findall(r"[A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}", query)}
        matches: list[dict[str, Any]] = []
        for path in self.paths:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            score = sum(text.lower().count(term) for term in terms)
            if score:
                lines = [line.strip() for line in text.splitlines() if any(term in line.lower() for term in terms)]
                matches.append({
                    "source": str(path), "version": self.version,
                    "applicability": next((line for line in lines if "appli" in line.lower() or "适用" in line), "Review source conditions before use"),
                    "excerpt": "\n".join(lines[:4])[:1600], "score": score,
                })
        matches.sort(key=lambda item: item["score"], reverse=True)
        if not matches:
            return ToolResult.not_found("runbook", {"items": []})
        return ToolResult.ok("runbook", {"items": matches[:max(1, min(limit, 10))]})
