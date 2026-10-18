"""Colours for cable roles, cluster health and groups."""
from __future__ import annotations

GROUP_COLORS = ["orange1", "orchid", "spring_green2", "gold1", "deep_sky_blue1", "salmon1"]

ROLE_COLOR = {"data": "grey50", "control": "orchid", "control-backup": "orchid", "fabric": "dark_cyan"}

LAG_COLOR = "steel_blue1"                                   # cables bundled by `lag`
PEER_COLOR = "medium_purple1"                               # peer links inside an MLAG domain
HEAT = ("bold dark_orange", "gold1", "dark_khaki")   # cable load: heavy, busy, light
HA_COLOR = {"ok": "green", "degraded": "orange1", "split": "bold red", "down": "red"}


def group_color(name: str) -> str:
    return GROUP_COLORS[sum(map(ord, name)) % len(GROUP_COLORS)]
