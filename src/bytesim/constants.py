"""Device kinds, sizes and the token grammar shared by the model and the UI."""
from __future__ import annotations

import re

KINDS = {
    "g": ("gateway", 4, "magenta"),
    "r": ("router", 4, "cyan"),
    "s": ("switch", 8, "green"),
    "c": ("computer", 1, "dodger_blue1"),
}

RANK = {"g": 0, "r": 1, "s": 2, "c": 3}

ALIASES = {
    "g": "g", "gw": "g", "gateway": "g",
    "r": "r", "router": "r",
    "s": "s", "sw": "s", "switch": "s",
    "c": "c", "pc": "c", "computer": "c", "host": "c",
}

MAX_PORTS = 64

BOX_H = 4          # top border, label, port row, bottom border

LAYER_GAP = 5      # empty rows between layers (room for link routing)

H_GAP = 4          # columns between neighbouring boxes


# Auto-numbering: prefix(n) stands for n new names, each prefix + the lowest free number.
# s(2) → two new switches; fab(1) → one new group named fab<next>.
AUTO = re.compile(r"^([A-Za-z][\w-]*?)\((\d+)\)$")
REF = re.compile(r"^([grsc])(\d+)(?::(?:p|nic|eth)?(\d+))?$", re.I)

GROUP_NAME = re.compile(r"^[a-z][\w-]{0,15}$", re.I)

ADDR = re.compile(r"^\(?(\d+),(\d+)\)?$")   # (network, host) e.g. (2,14) or 2,14

HOST_MAX = 255                                # 8-bit host numbers: 1..254, 255 = broadcast

VIP_HOST = 254                                # virtual gateway shared by an HA router cluster

POOL = range(10, VIP_HOST)                    # DHCP / ad-hoc hosts; routers take 1, 2, ...

ROLES = ("data", "control", "control-backup", "fabric")

POLICIES = ("none", "quorum", "witness")
CLUSTER_MODES = ("active-active", "active-passive")   # who forwards transit traffic in a cluster
