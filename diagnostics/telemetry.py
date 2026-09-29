"""Bounded process-local observations; no model or database call in trading hooks."""
from dataclasses import dataclass, field
from typing import Any

from .domain import utcnow
from .redaction import redact


@dataclass
class Observation:
    source: str
    status: str
    failures: int = 0
    observed_at: str = ""
    details: dict[str, Any] = field(default_factory=dict)


observations: dict[str, Observation] = {}


def observe(source: str, status: str, **details) -> None:
    old = observations.get(source)
    failures = 0 if status == "NORMAL" else (old.failures + 1 if old else 1)
    observations[source] = Observation(source, status, failures, utcnow().isoformat(), redact(details))
    if len(observations) > 256:
        del observations[next(iter(observations))]
