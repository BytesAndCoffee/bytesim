import pytest

from bytesim import demos
from bytesim.model import Network


@pytest.fixture
def basic() -> Network:
    net, _ = demos.basic()
    net.reconcile()
    net.auto_layout(140)
    return net


@pytest.fixture
def ha() -> Network:
    net, _ = demos.ha()
    net.reconcile()
    net.auto_layout(140)
    return net
