"""ByteSim: a text-mode network simulator.

The model (``bytesim.model``) can be used on its own::

    from bytesim import Network
    net = Network()
    net.add("r"); net.add("c")
    net.connect("r1", None, "c1", None)
"""
__version__ = "0.3.1"

from .model import Device, Iface, Link, NetError, Network  # noqa: E402

__all__ = ["Device", "Iface", "Link", "NetError", "Network", "__version__"]
