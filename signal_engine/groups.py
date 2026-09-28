"""Optional Telegram group-name fallback for local installations.

Keep real group memberships in the private TG_TARGET_GROUPS environment variable.
"""

from __future__ import annotations

SIGNAL_GROUPS: list[dict[str, str]] = []


def get_group_full_names() -> list[str]:
    return [group["full"] for group in SIGNAL_GROUPS]
