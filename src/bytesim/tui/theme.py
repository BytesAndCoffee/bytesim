"""Colours for cable roles, cluster health and groups."""
from __future__ import annotations

GROUP_COLORS = ["orange1", "orchid", "spring_green2", "gold1", "deep_sky_blue1", "salmon1"]

ROLE_COLOR = {"data": "grey50", "control": "orchid", "control-backup": "orchid", "fabric": "dark_cyan"}

HA_COLOR = {"ok": "green", "degraded": "orange1", "split": "bold red", "down": "red"}


def group_color(name: str) -> str:
    return GROUP_COLORS[sum(map(ord, name)) % len(GROUP_COLORS)]
