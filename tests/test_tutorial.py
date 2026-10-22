"""docs/tutorial.md stays true: its commands, typed in order into the real app, do
what the tutorial says they do."""
import asyncio
import re
from pathlib import Path

from test_app import _session

TUTORIAL = Path(__file__).resolve().parent.parent / "docs" / "tutorial.md"


def commands():
    blocks = re.findall(r"```text\n(.*?)```", TUTORIAL.read_text(), re.S)
    return [line.strip() for block in blocks for line in block.splitlines() if line.strip()]


def test_the_tutorial_works_as_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                         # it saves into the working directory
    cmds = commands()
    assert cmds[:3] == ["add c 1 4", "add s 8 2", "add r"] and cmds[-1] == "save clean-lab.json"
    app, log = asyncio.run(_session(cmds))
    text = "\n".join(log)
    chunks = re.split(r"^› ", text, flags=re.M)        # one chunk per command, in order
    out = {}
    for chunk in chunks[1:]:
        cmd, _, body = chunk.partition("\n")
        out.setdefault(cmd.strip(), []).append(body)
    net = app.net
    assert {d: net.devices[d].nports for d in net.devices} == {
        "c1": 1, "c2": 1, "c3": 1, "c4": 1, "s1": 8, "s2": 8, "r1": 4}
    nets = {c: net.addr_of((c, 1))[0] for c in ("c1", "c2", "c3", "c4")}
    assert nets["c1"] == nets["c2"] != nets["c3"] == nets["c4"]
    assert all(net.iface(c, 1).gw == 1 for c in nets)                       # the router on their network
    first, after_fail = out["ping c1 c2"]
    assert "reply from c2" in first and "r1" not in first.split("reply")[0]  # stays on its network
    assert "reply from c2" in after_fail and "reply from c4" in out["ping c3 c4"][1]
    crossing = out["ping c1 c3"]
    assert "reply from c3" in crossing[0] and "r1" in crossing[0]
    assert "✗" in crossing[1] and "reply from c3" not in crossing[1]         # r1 is down
    assert "reply from c3" in crossing[2] and "flooded the frame for R1A" in crossing[2]
    assert re.search(r"1\s+r1 .*\n\s+2\s+c3 .*\(reached\)", out["traceroute c1 c3"][0])
    assert (tmp_path / "clean-lab.json").exists()
