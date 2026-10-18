"""Colours for cable roles, cluster health and groups."""
from __future__ import annotations

GROUP_COLORS = ["#ffaf00", "orchid", "#00ff5f", "#ffd700", "#00afff", "#ff875f"]

ROLE_COLOR = {"data": "#808080", "control": "orchid", "control-backup": "orchid", "fabric": "#00af87"}

LAG_COLOR = "#5fd7ff"                                   # cables bundled by `lag`
PEER_COLOR = "#af87ff"                               # peer links inside an MLAG domain
HEAT = ("bold #ff8700", "#ffd700", "#afaf5f")   # cable load: heavy, busy, light
HA_COLOR = {"ok": "green", "degraded": "#ffaf00", "split": "bold red", "down": "red"}


def group_color(name: str) -> str:
    return GROUP_COLORS[sum(map(ord, name)) % len(GROUP_COLORS)]
