"""VPNs for WorkBoxes (relay.vpn): profiles and tunnels; gluetun in a network a holder keeps, the
WorkBoxes in it; the fallback loop and rotation."""

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from relay import config, vpn
from relay.app import create_app
from relay.containers import Containers
from relay.db import Database
from relay.providers import Registry

H = {"x-relay-request": "1"}
MULLVAD = {"VPN_SERVICE_PROVIDER": "mullvad", "VPN_TYPE": "wireguard", "WIREGUARD_PRIVATE_KEY": "secret-key",
           "FIREWALL_OUTBOUND_SUBNETS": "10.10.0.0/16"}
WINDSCRIBE = {"VPN_SERVICE_PROVIDER": "windscribe", "VPN_TYPE": "openvpn", "OPENVPN_USER": "me",
              "OPENVPN_PASSWORD": "pw"}
ADDRESSES = [  # `ip -j -4 addr`: the LAN's, and the containers' own (left out)
    {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
    {"ifname": "ens18", "addr_info": [{"family": "inet", "local": "192.168.1.5", "prefixlen": 24}]},
    {"ifname": "podman0", "addr_info": [{"family": "inet", "local": "10.88.0.1", "prefixlen": 16}]},
]
SERVERS = {  # (a little of gluetun's server list: where a provider's servers are, per protocol)
    "windscribe": [{"vpn": "openvpn", "region": "Netherlands", "city": "Amsterdam", "hostname": "nl-1"},
                   {"vpn": "openvpn", "region": "Netherlands", "city": "Rotterdam", "hostname": "nl-2"},
                   {"vpn": "wireguard", "region": "Germany", "city": "Frankfurt", "hostname": "de-1"}],
    "mullvad": [{"vpn": "wireguard", "country": "Sweden", "city": "Malmo", "hostname": "se-1"}],
    "vyprvpn": [{"vpn": "openvpn", "region": "Austria", "hostname": "at-1"}],
}


class Podman:
    """podman as relay.vpn and relay.containers use it: containers by name (state, spec, labels, id,
    start time, argv, the network they joined). A gluetun connects unless its place is in `nowhere`,
    or it's in `down`; its public address is its place's and how often it reconnected."""

    def __init__(self, mark):
        self.boxes: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.clock = 1000
        self.nowhere = {"Atlantis"}
        self.down: set[str] = set()  # (containers' ids)
        self.lists = 0
        self.mark = mark

    def tick(self) -> int:
        self.clock += 10
        return self.clock

    def gluetun(self, name):
        box = self.boxes.get(name)
        if not box or box["state"] != "running" or box["id"] in self.down:
            return None
        env = dict(a.split("=", 1) for a in box["argv"][1::1] if "=" in a and a.split("=", 1)[0].isupper())
        place = env.get("SERVER_CITIES") or env.get("SERVER_REGIONS") or env.get("SERVER_COUNTRIES") or ""
        if place in self.nowhere:
            return None
        return {"public_ip": f"198.51.100.{box['servers']}", "city": place or None, "country": "Somewhere",
                "env": env}

    def run(self, argv, **kwargs):
        if argv[:2] == ["ip", "-j"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(ADDRESSES), "")
        if argv[0] != "podman":
            return subprocess.CompletedProcess(argv, 0, "", "")
        args = argv[1:]
        self.calls.append(args)
        done = lambda out="", code=0: subprocess.CompletedProcess(argv, code, out, "")  # noqa: E731
        if args[:2] == ["container", "inspect"] and "HostConfig.NetworkMode" in args[3]:  # (several at once)
            boxes = [b for b in self.boxes.values() if b["id"] in args[4:]]
            return done("".join(f"{'container:' + b['joined'] if b['joined'] else 'pasta'}|{b['labels'].get('io.relay.vpn', '')}\n"
                                for b in boxes))
        if args[:2] == ["ps", "-q"]:
            return done("".join(f"{b['id']}\n" for b in self.boxes.values()
                                if b["state"] == "running" and b["labels"].get("io.relay.managed") == "1"))
        if args[0] == "stop":  # (and those in its network, as podman does)
            within = self.boxes[args[-1]]["id"]
            for box in [self.boxes[args[-1]], *(b for b in self.boxes.values() if b["joined"] == within)]:
                box["state"] = "exited"
            return done()
        if args[:2] == ["container", "inspect"]:
            box = self.boxes.get(args[-1])
            if box is None:
                return done(code=125)
            form = args[args.index("--format") + 1]
            if form == "{{json .}}":
                return done(json.dumps([{"Id": box["id"], "State": {"Status": box["state"]},
                                         "Config": {"Labels": box["labels"]}}]))
            if "StartedAt.Unix" in form:
                return done(str(box["started"]))
            return done(f"{box['state']} {box['labels'].get('io.relay.spec')}")
        if args[:2] == ["run", "-d"]:
            name = args[args.index("--name") + 1]
            labels = dict((a[8:].split("=", 1) + [""])[:2] for a in args if a.startswith("--label="))
            joined = next((a.split(":", 1)[1] for a in args if a.startswith("--network=container:")), None)
            self.boxes[name] = {"id": f"{name}-id{self.tick()}", "state": "running", "labels": labels,
                                "started": self.clock, "argv": args, "joined": joined, "servers": 1}
            return done()
        if args[:2] == ["run", "--rm"]:
            if "--entrypoint" in args:  # (clearing a tunnel's network of the last gluetun's rules)
                return done()
            folder = Path(args[args.index("-v") + 1].split(":")[0])  # (gluetun without settings: its server list)
            for name, servers in SERVERS.items():
                (folder / f"{name}.json").write_text(json.dumps({"version": 2, "servers": servers}))
            (folder / "manifest.json").write_text("{}")
            self.lists += 1
            return done(code=1)
        if args[0] in ("start", "restart"):
            self.boxes[args[-1]].update(state="running", started=self.tick())
            return done()
        if args[0] == "rm":
            name = args[-1]
            if name not in self.boxes:
                return done(code=1)
            gone, look = [name], [name]
            while look:  # (those in its network, and in theirs)
                within = self.boxes[look.pop()]["id"]
                joined = [n for n, b in self.boxes.items() if b["joined"] == within]
                gone += joined
                look += joined
            if len(gone) > 1 and "--depend" not in args:
                return done(code=125)  # (as podman: not while others are in its network)
            for each in gone:
                self.boxes.pop(each, None)
            return done()
        if args[0] == "exec" and "wget" in args:
            name, url = args[1], args[-1]
            seen = self.gluetun(name)
            if url.endswith("/v1/vpn/status"):
                if '"running"' in " ".join(args):
                    self.boxes[name]["servers"] += 1  # (another server)
                return done('{"outcome":"ok"}')
            if seen is None:
                return done(code=8)  # (its servers answer with an error while the tunnel doesn't work)
            if url.endswith(":9999/"):
                return done()
            if f"X-API-Key: {vpn.control_key()}" not in args:
                return done(code=8)  # (401)
            return done(json.dumps({k: v for k, v in seen.items() if k != "env"}))
        if args[0] == "logs":
            return done("INFO [http server] 200 GET /v1/publicip/ip\nERROR [vpn] no server found\n"
                        "INFO [http server] 401 GET /v1/publicip/ip")
        if args[:2] == ["image", "inspect"]:
            return done("sha256:0123456789abcdef")
        if args[:2] == ["ps", "-a"] and any("io.relay.vpn" in a for a in args):
            return done("".join(f"{n}|{b['labels'].get('io.relay.vpn', '')}|{b['labels'].get('io.relay.instance', '')}\n"
                                for n, b in self.boxes.items() if "io.relay.vpn" in b["labels"]))
        return done()


@pytest.fixture
def podman(monkeypatch):
    from relay.containers import instance
    fake = Podman(instance())
    monkeypatch.setattr(subprocess, "run", fake.run)
    # (waiting for a connection: on a clock of its own, which sleeping moves on)
    clock = [0.0]
    monkeypatch.setattr(vpn, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0],
                                                     sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    return fake


def tunnel(**changes):
    found = {"id": "t1", "name": "Private", "folders": [], "all": False, "ports": [3000],
             "exits": [{"profile": "p1", "place": {"SERVER_COUNTRIES": "Sweden"}}],
             "rotate": {"minutes": 0, "to": "server", "idle": True}, "fallback": {"after": 0.05, "back": 30}}
    return {**found, **changes}


def make(tmp_path, tunnels=None, **settings):
    found = config.Settings(port=8420, **settings)
    found.vpn = {"profiles": [{"id": "p1", "name": "Mullvad", "env": dict(MULLVAD)},
                              {"id": "p2", "name": "Windscribe", "env": dict(WINDSCRIBE)}],
                 "tunnels": tunnels if tunnels is not None else [tunnel(folders=[str(tmp_path / "private")])]}
    tunnels_ = vpn.Vpn(found)
    containers = Containers(state=tmp_path / "state")
    containers.checked = "ok"
    containers.network = tunnels_.network
    containers.via_vpn = tunnels_.covers
    containers.vpn_url = tunnels_.relay_url()
    for folder in ("private", "private/sub", "open", "other"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    return tunnels_, containers


def argv_of(podman, name):
    return podman.boxes[name]["argv"]


def env_of(podman, name):
    argv = argv_of(podman, name)
    return dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-e")


def test_the_one_vpn_of_before_is_a_profile_and_a_tunnel():
    found = vpn.normalize({"env": {**WINDSCRIBE, "SERVER_REGIONS": "Netherlands", "SERVER_CITIES": "Amsterdam"},
                           "folders": ["/ws/a"], "all": False, "ports": [3000]})
    assert found["profiles"] == [{"id": "p1", "name": "Windscribe", "env": WINDSCRIBE}]
    assert found["tunnels"][0]["exits"] == [{"profile": "p1", "place": {"SERVER_REGIONS": "Netherlands",
                                                                          "SERVER_CITIES": "Amsterdam"}}]
    assert found["tunnels"][0]["folders"] == ["/ws/a"] and found["tunnels"][0]["ports"] == [3000]
    assert found["tunnels"][0]["rotate"] == vpn.DEFAULT_ROTATE and found["tunnels"][0]["fallback"] == vpn.DEFAULT_FALLBACK
    assert vpn.normalize({}) == {"profiles": [], "tunnels": [], "image": ""}


def test_a_folders_workbox_goes_through_its_tunnel(tmp_path, podman):
    tunnels, containers = make(tmp_path, [
        tunnel(folders=[str(tmp_path / "private")]),
        tunnel(id="t2", name="Sub", folders=[str(tmp_path / "private" / "sub")], ports=[],
               exits=[{"profile": "p2", "place": {"SERVER_CITIES": "Amsterdam"}}]),
        tunnel(id="t3", name="The rest", all=True, ports=[], exits=[{"profile": "p2", "place": {}}])])
    name = containers.ensure(tmp_path / "private")
    holder, gluetun = tunnels.names(tunnels.tunnel("t1"))
    holder_box = podman.boxes[holder]
    assert argv_of(podman, name)[argv_of(podman, name).index("--init") + 1] == f"--network=container:{holder_box['id']}"
    # The holder keeps the network (pasta, the ports, gluetun's DNS); gluetun is in it
    assert f"--network=pasta:--map-host-loopback,{vpn.HOST}" in holder_box["argv"] and "--dns=127.0.0.1" in holder_box["argv"]
    assert ["-p", "3000:3000"] == holder_box["argv"][holder_box["argv"].index("-p"):holder_box["argv"].index("-p") + 2]
    assert podman.boxes[gluetun]["joined"] == holder_box["id"] and "--cap-add=NET_ADMIN" in argv_of(podman, gluetun)
    env = env_of(podman, gluetun)
    assert env["WIREGUARD_PRIVATE_KEY"] == "secret-key" and env["SERVER_COUNTRIES"] == "Sweden"
    assert env["FIREWALL_OUTBOUND_SUBNETS"] == f"10.10.0.0/16,{vpn.HOST}/32,192.168.1.0/24"
    assert json.loads(env["HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE"]) == {"auth": "apikey", "apikey": vpn.control_key()}
    assert podman.boxes[gluetun]["labels"][vpn.EXIT] == "0"
    # Its network was cleared of a gluetun before's rules, its DROP policies kept
    reset = next(c for c in podman.calls if c[:2] == ["run", "--rm"] and "--entrypoint" in c)
    assert f"--network=container:{holder_box['id']}" in reset and "-P OUTPUT DROP" in reset[-1] and "-F" in reset[-1]
    # The most specific folder's tunnel; the one for all the rest; none
    assert tunnels.tunnel_for(tmp_path / "private" / "sub")["id"] == "t2"
    assert tunnels.tunnel_for(tmp_path / "private" / "x")["id"] == "t1"
    assert tunnels.tunnel_for(tmp_path / "open")["id"] == "t3"
    tunnels.settings.vpn["tunnels"] = tunnels.settings.vpn["tunnels"][:2]
    assert not tunnels.covers(tmp_path / "open") and "--network=host" in argv_of(podman, containers.ensure(tmp_path / "open"))


def test_agents_in_a_tunnel_reach_relay_at_its_address_there(tmp_path, podman):
    tunnels, containers = make(tmp_path)
    containers.set_url("http://127.0.0.1:8420")
    tools = str(tmp_path / "tools" / "abc123" / "relay")
    inside = dict(e.split("=", 1) for e in containers.agent_env(tools, tmp_path / "private")[1::2])
    assert inside["RELAY_MCP_URL"] == f"http://{vpn.HOST}:8420/mcp/abc123"
    assert open(inside["RELAY_URL_FILE"]).read().strip() == f"http://{vpn.HOST}:8420"
    outside = dict(e.split("=", 1) for e in containers.agent_env(tools, tmp_path / "open")[1::2])
    assert outside["RELAY_MCP_URL"] == "http://127.0.0.1:8420/mcp/abc123"
    # Relay listening on one address only (not its loopback): not reachable from there
    assert vpn.Vpn(config.Settings(host="192.168.1.5")).relay_url() is None


def test_an_exit_that_doesnt_connect_falls_back_to_the_next(tmp_path, podman):
    tunnels, containers = make(tmp_path, [tunnel(folders=[str(tmp_path / "private")], exits=[
        {"profile": "p1", "place": {"SERVER_COUNTRIES": "Atlantis"}},
        {"profile": "p2", "place": {"SERVER_CITIES": "Amsterdam"}}])])
    containers.ensure(tmp_path / "private")
    found = tunnels.tunnel("t1")
    status = tunnels.status(found)
    assert status["connected"] and status["exit"] == 1 and status["exit_label"] == "Windscribe · Amsterdam"
    assert status["public_ip"] == "198.51.100.1" and status["place"] == "Amsterdam, Somewhere"
    events = [e["text"] for e in status["events"]]
    assert events[0].startswith("Fell back: the exit before didn't connect: Windscribe · Amsterdam")
    assert events[1] == "Mullvad · Atlantis didn't connect"
    # Without a fallback loop: only its exit, and why it didn't connect
    tunnels.stop(found)
    tunnels.settings.vpn["tunnels"][0]["fallback"]["after"] = 0
    monkey_timeout = vpn.READY_TIMEOUT
    vpn.READY_TIMEOUT = 0.05
    try:
        with pytest.raises(vpn.VpnError, match="Private didn't connect. Mullvad · Atlantis: ERROR .vpn. no server found"):
            containers.ensure(tmp_path / "private")
    finally:
        vpn.READY_TIMEOUT = monkey_timeout


def test_the_fallback_loop_goes_round_and_back_to_the_first(tmp_path, podman):
    tunnels, containers = make(tmp_path, [tunnel(folders=[str(tmp_path / "private")], fallback={"after": 30, "back": 10},
                                                 exits=[{"profile": "p1", "place": {"SERVER_COUNTRIES": "Sweden"}},
                                                        {"profile": "p2", "place": {"SERVER_CITIES": "Amsterdam"}}])])
    work = containers.ensure(tmp_path / "private")
    started = podman.boxes[work]["started"]
    found = tunnels.tunnel("t1")
    _, gluetun = tunnels.names(found)
    now = 10_000_000
    podman.down.add(podman.boxes[gluetun]["id"])  # (the connection drops)
    tunnels.tick(now)
    assert tunnels.status(found)["exit"] == 0  # (gluetun gets its time to reconnect itself)
    podman.nowhere.add("Sweden")  # (and the first exit doesn't come back)
    tunnels.tick(now + 31)
    status = tunnels.status(found)
    assert status["exit"] == 1 and status["connected"]
    assert "Mullvad · Sweden: no connection for 31 s; falling back" in [e["text"] for e in status["events"]]
    # The WorkBox kept its network: gluetun was made anew in the holder's
    assert podman.boxes[work]["started"] == started and containers.ensure(tmp_path / "private") == work
    assert podman.boxes[work]["started"] == started
    # Back to the first after a while: it still doesn't connect, so the second again
    tunnels.state["t1"]["since"] = now - 11 * 60
    tunnels.tick(now + 40)
    assert tunnels.status(found)["exit"] == 1
    assert "Fell back: the first exit still doesn't connect: Windscribe · Amsterdam, 198.51.100.1 (Amsterdam, Somewhere)" in [
        e["text"] for e in tunnels.status(found)["events"]]
    # …now it does
    podman.nowhere.discard("Sweden")
    tunnels.state["t1"]["since"] = now - 11 * 60
    tunnels.tick(now + 50)
    assert tunnels.status(found)["exit"] == 0
    # Round and round: from the last exit, down long enough, to the first
    tunnels.settings.vpn["tunnels"][0]["fallback"]["back"] = 0
    tunnels._connect(found, podman.boxes[tunnels.names(found)[0]]["id"], 1, "fell back")
    podman.down.add(podman.boxes[gluetun]["id"])
    tunnels.tick(now + 100)
    tunnels.tick(now + 131)
    assert tunnels.status(found)["exit"] == 0 and tunnels.status(found)["connected"]


def test_rotation_new_servers_or_the_next_exit_not_while_an_agent_works(tmp_path, podman):
    tunnels, containers = make(tmp_path, [tunnel(folders=[str(tmp_path / "private")],
                                                 rotate={"minutes": 30, "to": "server", "idle": True},
                                                 exits=[{"profile": "p1", "place": {"SERVER_COUNTRIES": "Sweden"}},
                                                        {"profile": "p2", "place": {"SERVER_CITIES": "Amsterdam"}}])])
    containers.ensure(tmp_path / "private")
    found = tunnels.tunnel("t1")
    _, gluetun = tunnels.names(found)
    now = tunnels.state["t1"]["rotated"]
    tunnels.tick(now + 60)
    assert tunnels.status(found)["public_ip"] == "198.51.100.1"  # (not yet)
    tunnels.busy = lambda: {(tmp_path / "private" / "sub").resolve()}
    tunnels.tick(now + 31 * 60)
    assert tunnels.status(found)["public_ip"] == "198.51.100.1"  # (an agent works there: later)
    tunnels.busy = set
    tunnels.tick(now + 32 * 60)
    status = tunnels.status(found)
    assert status["public_ip"] == "198.51.100.2" and status["exit"] == 0  # gluetun's own reconnect
    assert status["events"][0]["text"].startswith("Rotated: Mullvad · Sweden, 198.51.100.2")
    assert [c for c in podman.calls if c[:2] == ["exec", gluetun] and c[-1].endswith("/v1/vpn/status")]
    # To the next exit; and while an agent works, when the user says so
    tunnels.settings.vpn["tunnels"][0]["rotate"] = {"minutes": 30, "to": "next", "idle": False}
    tunnels.busy = lambda: {(tmp_path / "private").resolve()}
    tunnels.state["t1"]["rotated"] -= 31 * 60
    tunnels.tick(now + 33 * 60)
    assert tunnels.status(found)["exit"] == 1 and tunnels.status(found)["events"][0]["text"].startswith("Rotated: Windscribe")


def test_other_ports_make_the_network_anew_and_its_workboxes_with_it(tmp_path, podman):
    tunnels, containers = make(tmp_path)
    work = containers.ensure(tmp_path / "private")
    holder, gluetun = tunnels.names(tunnels.tunnel("t1"))
    before = podman.boxes[holder]["id"]
    tunnels.settings.vpn["tunnels"][0]["ports"] = [3000, 8080]
    containers.ensure(tmp_path / "private")
    assert ["rm", "-f", "-t", "5", "--depend", holder] in podman.calls  # (podman: not without them)
    assert podman.boxes[holder]["id"] != before and f"--network=container:{podman.boxes[holder]['id']}" in argv_of(podman, work)
    # A changed profile: only gluetun anew, in the same network
    tunnels.settings.vpn["profiles"][0]["env"]["SERVER_CITIES"] = "Malmo"  # (a place isn't a profile's: left out)
    tunnels.settings.vpn["profiles"][0]["env"]["WIREGUARD_ADDRESSES"] = "10.0.0.2/32"
    started = podman.boxes[work]["started"]
    containers.ensure(tmp_path / "private")
    assert env_of(podman, gluetun)["WIREGUARD_ADDRESSES"] == "10.0.0.2/32" and "SERVER_CITIES" not in env_of(podman, gluetun)
    assert podman.boxes[work]["started"] == started
    # Gone: gluetun, its network, its WorkBoxes; on the machine's network when next used
    tunnels.stop(tunnels.tunnel("t1"))
    tunnels.settings.vpn["tunnels"] = []
    assert holder not in podman.boxes and work not in podman.boxes
    assert "--network=host" in argv_of(podman, containers.ensure(tmp_path / "private"))


def test_a_removed_tunnels_containers_go(tmp_path, podman):
    tunnels, containers = make(tmp_path)
    containers.ensure(tmp_path / "private")
    holder, gluetun = tunnels.names(tunnels.tunnel("t1"))
    podman.boxes["relay-vpn-other"] = {"id": "x", "state": "running", "labels": {"io.relay.vpn": "t1",
                                       "io.relay.instance": "feedface"}, "started": 1, "argv": [], "joined": None, "servers": 1}
    podman.boxes["relay-vpn" + (f"-{podman.mark}" if podman.mark else "")] = {
        "id": "y", "state": "exited", "labels": {"io.relay.vpn": "", "io.relay.instance": podman.mark}, "started": 1,
        "argv": [], "joined": None, "servers": 1}  # (the one VPN of before)
    assert tunnels.prune() == ["relay-vpn" + (f"-{podman.mark}" if podman.mark else "")]
    tunnels.settings.vpn["tunnels"] = []
    assert set(tunnels.prune()) <= {holder, gluetun}  # (the holder takes gluetun along)
    assert holder not in podman.boxes and gluetun not in podman.boxes
    assert "relay-vpn-other" in podman.boxes  # (another Relay's is its own)


def test_the_public_address_needs_relays_key(tmp_path, podman):
    tunnels, containers = make(tmp_path)
    containers.ensure(tmp_path / "private")
    _, gluetun = tunnels.names(tunnels.tunnel("t1"))
    assert tunnels.public_ip(gluetun)["public_ip"] == "198.51.100.1"
    assert vpn.control_key() == vpn.control_key()  # (made once: gluetun isn't made anew every time)
    assert "http server" not in tunnels.log(tunnels.tunnel("t1"))  # (its requests left out: what went wrong shows)


def test_gluetuns_server_list_for_its_settings(tmp_path, podman, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    found = vpn.servers()
    assert found["providers"]["windscribe"] == {"setting": "SERVER_REGIONS", "cities": True, "types": {
        "openvpn": {"Netherlands": ["Amsterdam", "Rotterdam"]}, "wireguard": {"Germany": ["Frankfurt"]}}}
    assert found["providers"]["mullvad"]["setting"] == "SERVER_COUNTRIES"
    assert found["providers"]["vyprvpn"] == {"setting": "SERVER_REGIONS", "cities": False, "types": {"openvpn": {"Austria": []}}}
    assert "manifest" not in found["providers"]
    assert vpn.servers() == found and podman.lists == 1  # once per image


def test_unused_workboxes_are_stopped_but_not_the_vpn(tmp_path, monkeypatch):
    from relay.containers import instance
    containers = Containers(state=tmp_path / "state")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["podman", "ps"]:
            return subprocess.CompletedProcess(argv, 0, f"relay-vpn-t1 running 0|{instance()}\n"
                                                        f"relay-vpnnet-t1 running 0|{instance()}\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert containers.stop_idle(0) == 0 and not [c for c in calls if "stop" in c or "rm" in c]


def test_the_vpn_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    workspace = tmp_path / "ws"
    (workspace / "a").mkdir(parents=True)
    (workspace / "b").mkdir()
    settings = config.Settings(workspace_roots=[str(workspace)], chat_root=str(tmp_path / "chats"))
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults()
    happened = []
    monkeypatch.setattr(vpn.Vpn, "ensure", lambda self, t: happened.append(("ensure", t["id"])) or "id")
    monkeypatch.setattr(vpn.Vpn, "stop", lambda self, t: happened.append(("stop", t["id"])))
    monkeypatch.setattr(vpn.Vpn, "running", lambda self, t: True)
    monkeypatch.setattr(vpn.Vpn, "status", lambda self, t: {"running": False, "connected": False})
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    with TestClient(app) as client:
        assert client.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H).status_code == 200
        assert client.get("/api/vpn").json()["tunnels"] == []
        profile = client.post("/api/vpn/profiles", json={"name": "Windscribe", "env": {**WINDSCRIBE, "SERVER_CITIES": "x"}},
                              headers=H).json()
        assert profile["env"]["OPENVPN_PASSWORD"] == "********" and "SERVER_CITIES" not in profile["env"]
        # A masked password stays as it was
        client.put(f"/api/vpn/profiles/{profile['id']}", json={"env": {**WINDSCRIBE, "OPENVPN_PASSWORD": "********",
                                                                      "OPENVPN_USER": "you"}}, headers=H)
        kept = vpn.normalize(config.load_settings().vpn)["profiles"][0]["env"]
        assert kept["OPENVPN_PASSWORD"] == "pw" and kept["OPENVPN_USER"] == "you"
        exits = [{"profile": profile["id"], "place": {"SERVER_REGIONS": "Netherlands", "SERVER_CITIES": "Amsterdam"}}]
        made = client.post("/api/vpn/tunnels", json={"name": "Private", "folders": [str(workspace / "a")], "exits": exits,
                                                     "ports": ["3000"], "rotate": {"minutes": 60, "to": "next"},
                                                     "fallback": {"after": 45, "back": 0}}, headers=H).json()
        assert made["ports"] == [3000] and made["error"] is None and ("ensure", made["id"]) in happened
        assert made["rotate"] == {"minutes": 60, "to": "next", "idle": True} and made["fallback"] == {"after": 45, "back": 0}
        # A changed profile: its tunnels connect anew
        happened.clear()
        client.put(f"/api/vpn/profiles/{profile['id']}", json={"name": "Windscribe NL"}, headers=H)
        assert happened == [("ensure", made["id"])]
        # Saved even when it doesn't connect, or a box won't start (a port taken): why, for Settings
        monkeypatch.setattr(vpn.Vpn, "ensure", lambda self, t: (_ for _ in ()).throw(vpn.VpnError("no handshake")))
        changed = client.put(f"/api/vpn/tunnels/{made['id']}", json={"all": True}, headers=H).json()
        assert changed["error"] == "no handshake" and vpn.normalize(config.load_settings().vpn)["tunnels"][0]["all"]
        from relay.containers import ContainerError
        monkeypatch.setattr(vpn.Vpn, "ensure", lambda self, t: (_ for _ in ()).throw(ContainerError("port 3000 is taken")))
        changed = client.put(f"/api/vpn/tunnels/{made['id']}", json={"ports": [3000]}, headers=H)
        assert changed.status_code == 200 and changed.json()["error"] == "port 3000 is taken"
        wrong = [
            {"name": "B", "folders": [str(tmp_path)], "exits": exits},  # (not in a workspace root)
            {"name": "B", "folders": [str(workspace / "a")], "exits": exits},  # (another tunnel's)
            {"name": "B", "all": True, "exits": exits},  # (another one is for all)
            {"name": "B", "ports": [3000], "exits": exits},  # (another one's)
            {"name": "B", "ports": [settings.port], "exits": exits},
            {"name": "B", "exits": []},
            {"name": "B", "exits": [{"profile": "nope"}]},
            {"name": "B", "exits": [{"profile": profile["id"], "place": {"SERVER_PORT": "1"}}]},
            {"name": "B", "exits": exits, "rotate": {"minutes": 2}},
            {"name": "B", "exits": exits, "fallback": {"after": 5}},
        ]
        for data in wrong:
            assert client.post("/api/vpn/tunnels", json=data, headers=H).status_code == 400, data
        assert client.post("/api/vpn/profiles", json={"name": "X", "env": {"VPN_TYPE": "openvpn"}}, headers=H).status_code == 400
        # Its problems as notifications: on unless turned off (Settings → Notifications)
        assert client.get("/api/settings").json()["notifications"]["notify_vpn"] is True
        assert client.put("/api/settings", json={"notifications": {"notify_vpn": "yes"}}, headers=H).status_code == 400
        client.put("/api/settings", json={"notifications": {"notify_vpn": False}}, headers=H)
        assert config.load_settings().notify_vpn is False
        # A profile in use stays; a tunnel goes, its containers with it
        assert client.delete(f"/api/vpn/profiles/{profile['id']}", headers=H).status_code == 409
        assert client.delete(f"/api/vpn/tunnels/{made['id']}", headers=H).json() == {"ok": True}
        assert ("stop", made["id"]) in happened
        assert client.delete(f"/api/vpn/profiles/{profile['id']}", headers=H).json() == {"ok": True}
        # gluetun's server list, for the forms; why, when it doesn't come
        from relay.routes import vpn as routes
        monkeypatch.setattr(routes, "servers", lambda image: {"image": image, "providers": {"mullvad": {}}})
        assert client.get("/api/vpn/servers").json() == {"image": vpn.IMAGE, "providers": {"mullvad": {}}}
        monkeypatch.setattr(routes, "servers", lambda image: (_ for _ in ()).throw(vpn.VpnError("no podman")))
        assert client.get("/api/vpn/servers").status_code == 409


def test_the_fallback_loop_moves_past_an_exit_that_doesnt_connect_either(tmp_path, podman):
    tunnels, containers = make(tmp_path, [tunnel(folders=[str(tmp_path / "private")], fallback={"after": 30, "back": 0},
                                                 exits=[{"profile": "p1", "place": {"SERVER_COUNTRIES": "Sweden"}},
                                                        {"profile": "p1", "place": {"SERVER_COUNTRIES": "Norway"}},
                                                        {"profile": "p2", "place": {"SERVER_CITIES": "Amsterdam"}}])])
    containers.ensure(tmp_path / "private")
    found = tunnels.tunnel("t1")
    _, gluetun = tunnels.names(found)
    podman.nowhere |= {"Sweden", "Norway"}
    podman.down.add(podman.boxes[gluetun]["id"])
    now = 10_000_000
    for step in range(5):
        tunnels.tick(now + step * 31)
    assert tunnels.status(found)["exit"] == 2 and tunnels.status(found)["connected"]


def test_a_gluetun_that_stopped_is_started_again(tmp_path, podman):
    tunnels, containers = make(tmp_path, [tunnel(folders=[str(tmp_path / "private")], fallback={"after": 30, "back": 0})])
    containers.ensure(tmp_path / "private")
    found = tunnels.tunnel("t1")
    _, gluetun = tunnels.names(found)
    podman.boxes[gluetun]["state"] = "exited"  # (one exit: nothing to fall back to)
    now = 10_000_000
    tunnels.tick(now)
    tunnels.tick(now + 31)
    status = tunnels.status(found)
    assert status["connected"] and status["events"][0]["text"].startswith("Started again: Mullvad · Sweden")
    assert "Mullvad · Sweden: gluetun stopped" in [e["text"] for e in status["events"]]


def test_subagents_copies_and_tasks_branches_go_through_their_projects_tunnel(tmp_path, monkeypatch):
    from relay import forks
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    project = tmp_path / "ws" / "app"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text("print(1)\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=T", "-c", "user.email=t@x", "commit", "-q", "-m", "start"]):
        subprocess.run(["git", *args], cwd=project, check=True)
    settings = config.Settings()
    settings.vpn = {"profiles": [{"id": "p1", "name": "Mullvad", "env": dict(MULLVAD)}],
                    "tunnels": [tunnel(folders=[str(project)])]}
    tunnels = vpn.Vpn(settings)
    # A task on its own branch: a worktree in Relay's data
    branch = config.worktrees_dir() / "conv1"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "relay/task-conv1", str(branch)], cwd=project, check=True)
    assert vpn.origin(branch / "src") == (project / "src").resolve()
    assert tunnels.tunnel_for(branch / "src")["id"] == "t1"
    # A subagent's copy (relay.forks), of the task's branch
    copy = forks.folders() / "sub1" / "app"
    (copy / "src").mkdir(parents=True)
    (forks.folders() / "sub1" / "fork.json").write_text(json.dumps(
        {"caller": "conv1", "subagent": "sub1", "source": str(branch), "folder": str(copy), "links": []}))
    assert vpn.origin(copy / "src") == (project / "src").resolve()
    assert tunnels.covers(copy) and not tunnels.covers(tmp_path / "ws")
    # Another folder in Relay's data stays itself
    assert vpn.origin(forks.folders() / "nobody" / "x") == (forks.folders() / "nobody" / "x").resolve()


def test_an_unused_tunnel_stops_and_its_workboxes_wake_it(tmp_path, podman):
    tunnels, containers = make(tmp_path, [tunnel(folders=[str(tmp_path / "private")],
                                                 exits=[{"profile": "p1", "place": {"SERVER_COUNTRIES": "Sweden"}},
                                                        {"profile": "p2", "place": {"SERVER_CITIES": "Amsterdam"}}])])
    work = containers.ensure(tmp_path / "private")
    found = tunnels.tunnel("t1")
    holder, gluetun = tunnels.names(found)
    now = time.time()
    tunnels.tick(now + 60 * 60)  # (a WorkBox runs in it: used, however long)
    assert podman.boxes[holder]["state"] == "running"
    tunnels.busy = lambda: {(tmp_path / "private").resolve()}
    podman.boxes[work]["state"] = "exited"  # (Containers.stop_idle)
    tunnels.tick(now + 2 * 60 * 60)  # (an agent works there: used)
    tunnels.busy = set
    tunnels.tick(now + 2 * 60 * 60 + 5 * 60)
    assert podman.boxes[holder]["state"] == "running"
    tunnels.state["t1"]["exit"] = 1  # (it fell back meanwhile)
    tunnels.tick(now + 2 * 60 * 60 + vpn.IDLE_AFTER)
    assert podman.boxes[holder]["state"] == "exited" and gluetun not in podman.boxes
    assert podman.boxes[work]["state"] == "exited"  # (kept, with what's in it)
    status = tunnels.status(found)
    assert not status["running"] and status["events"][0]["text"] == "Stopped: no WorkBox in it for 10 minutes"
    # The first WorkBox used again starts it again, at its first exit, in the same network
    assert containers.ensure(tmp_path / "private") == work
    assert podman.boxes[work]["state"] == "running" and podman.boxes[work]["joined"] == podman.boxes[holder]["id"]
    status = tunnels.status(found)
    assert status["connected"] and status["exit"] == 0
    assert podman.boxes[work]["started"] > podman.boxes[holder]["started"]  # (no restart needed after)


class Told:
    """relay.notify.Notifier.vpn, kept."""

    def __init__(self):
        self.told = []

    def vpn(self, tunnel, text, recovered=False):
        self.told.append(("recovered" if recovered else "problem", tunnel, text))


def test_a_tunnels_problems_are_told_once_and_that_its_connected_again(tmp_path, podman):
    tunnels, containers = make(tmp_path, [tunnel(folders=[str(tmp_path / "private")], fallback={"after": 30, "back": 0},
                                                 rotate={"minutes": 5, "to": "server", "idle": False},
                                                 exits=[{"profile": "p1", "place": {"SERVER_COUNTRIES": "Atlantis"}},
                                                        {"profile": "p1", "place": {"SERVER_COUNTRIES": "Sweden"}},
                                                        {"profile": "p2", "place": {"SERVER_CITIES": "Amsterdam"}}])])
    told = Told()
    tunnels.notifier = told
    containers.ensure(tmp_path / "private")  # (its first exit doesn't connect, the next does: not a problem)
    found = tunnels.tunnel("t1")
    assert told.told == [] and tunnels.status(found)["exit"] == 1
    assert [e["kind"] for e in tunnels.status(found)["events"]] == ["info", "warn"]
    _, gluetun = tunnels.names(found)
    now = tunnels.state["t1"]["rotated"]
    tunnels.tick(now + 60)
    tunnels.tick(now + 4 * 60)
    assert told.told == []  # (a rotation, a new server: not told)
    # It loses its connection: told; the next exit connects: told that too, once
    podman.down.add(podman.boxes[gluetun]["id"])
    tunnels.tick(now + 4 * 60 + 1)
    tunnels.tick(now + 4 * 60 + 32)
    assert told.told == [("problem", "Private", "Mullvad · Sweden: no connection for 31 s; falling back"),
                         ("recovered", "Private", "Connected again: Windscribe · Amsterdam, 198.51.100.1 (Amsterdam, Somewhere)")]
    # Nothing connects, round and round: told once in half an hour
    told.told.clear()
    podman.nowhere |= {"Sweden", "Amsterdam"}
    for step in range(8):
        podman.down.add(podman.boxes[gluetun]["id"])
        tunnels.tick(now + 600 + step * 31)
    assert [kind for kind, _, _ in told.told] == ["problem"]
    # Kept over a restart of Relay; a deleted tunnel's goes
    again = vpn.Vpn(tunnels.settings)
    assert again.status(found)["events"][0] == tunnels.status(found)["events"][0]
    again.forget("t1")
    assert vpn.Vpn(tunnels.settings).status(found)["events"] == []


def test_no_exit_connecting_at_all_is_told(tmp_path, podman):
    tunnels, containers = make(tmp_path, [tunnel(folders=[str(tmp_path / "private")],
                                                 exits=[{"profile": "p1", "place": {"SERVER_COUNTRIES": "Atlantis"}}])])
    told = Told()
    tunnels.notifier = told
    with pytest.raises(vpn.VpnError):
        containers.ensure(tmp_path / "private")
    assert told.told == [("problem", "Private", "No exit connects: Mullvad · Atlantis")]


def test_vpn_notifications_go_out_unless_turned_off(monkeypatch):
    from relay import notify
    sent = []
    monkeypatch.setattr(notify, "_post", lambda url, body, headers: sent.append((url, body.decode(), headers)))
    monkeypatch.setattr(notify.threading, "Thread", lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)))
    settings = config.Settings(ntfy_url="https://ntfy.example/relay", public_url="https://relay.example")
    notifier = notify.Notifier(settings)
    notifier.vpn("Private", "Mullvad · Sweden: no connection for 31 s; falling back")
    url, body, headers = sent[-1]
    assert url == "https://ntfy.example/relay" and body.startswith("Mullvad · Sweden")
    assert headers["Title"] == "VPN Private: a problem" and headers["Priority"] == "high" and headers["Click"] == "https://relay.example"
    notifier.vpn("Private", "Connected again: …", recovered=True)
    assert sent[-1][2]["Title"] == "VPN Private: connected again" and sent[-1][2]["Tags"] == "white_check_mark"
    settings.notify_vpn = False
    notifier.vpn("Private", "again")
    assert len(sent) == 2


def test_a_tunnels_network_outlives_relay_and_one_that_didnt_is_made_anew(tmp_path, podman, monkeypatch):
    """Relay as a systemd service: a restart ends what's in its cgroup, pasta (the holder's network) with it
    unless the holder starts in a scope of its own. One an older Relay started lost its network: gluetun
    said "Network unreachable" while Relay saw it running, so ensure (and the keeping) make it anew."""
    tunnels, containers = make(tmp_path)
    containers.ensure(tmp_path / "private")
    found = tunnels.tunnel("t1")
    holder, gluetun = tunnels.names(found)
    before = podman.boxes[holder]["id"]
    monkeypatch.setattr(vpn, "network_alive", lambda name: name != holder)
    tunnels.tick()
    assert podman.boxes[holder]["id"] != before and podman.boxes[gluetun]["joined"] == podman.boxes[holder]["id"]
    assert any("network was gone" in e["text"] for e in tunnels.status(found)["events"])
    # Its WorkBox (gone with the old holder's network) goes on in the new one
    name = containers.ensure(tmp_path / "private")
    assert podman.boxes[name]["joined"] == podman.boxes[holder]["id"]

    # Under systemd the holder's podman runs in a scope of its own; elsewhere (or with no user manager) as it is
    from relay import containers as module
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        fails = argv[0] == "systemd-run" and "nobus" in seen[0]
        return subprocess.CompletedProcess(argv, 1 if fails else 0, "", "Failed to connect to bus" if fails else "")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(module, "_in_a_service", lambda: False)
    module._run_scoped(["podman", "start", "x"], True)
    assert seen[-1] == ["podman", "start", "x"]
    monkeypatch.setattr(module, "_in_a_service", lambda: True)
    module._run_scoped(["podman", "start", "x"], False)
    assert seen[-1] == ["podman", "start", "x"]
    module._run_scoped(["podman", "start", "x"], True)
    assert seen[-1][:5] == ["systemd-run", "--user", "--scope", "--quiet", "--collect"] and seen[-1][5:] == ["podman", "start", "x"]
    seen.clear()
    seen.append(["nobus"])
    module._run_scoped(["podman", "start", "x"], True)
    assert seen[-1] == ["podman", "start", "x"]  # (no user manager to ask: as it is)


def test_a_holders_pasta_is_found_wherever_it_is(tmp_path, monkeypatch):
    """pasta by its name, wherever it's installed; none to be seen in a /proc that doesn't show the
    machine's processes (a pid namespace of Relay's own) can't be told from gone: alive then."""
    from relay import containers as module

    def inspect(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "pasta:--map-host-loopback|/run/user/1000/netns/netns-abc\n", "")
    monkeypatch.setattr(subprocess, "run", inspect)
    proc = tmp_path / "proc"

    def process(pid, *argv):
        (proc / str(pid)).mkdir(parents=True, exist_ok=True)
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    process(1, "/sbin/init")
    process(50, "/usr/libexec/podman/pasta.avx2", "--config-net", "--netns", "/run/user/1000/netns/netns-abc", "-q")
    assert module.network_alive("holder", proc)
    process(50, "/usr/libexec/podman/pasta.avx2", "--netns", "/run/user/1000/netns/netns-other")
    assert not module.network_alive("holder", proc)  # (another box's pasta: this one's is gone)
    (proc / "50" / "cmdline").write_bytes(b"sleep\0")
    assert not module.network_alive("holder", proc)  # (no pasta at all, and /proc shows the machine's)
    (proc / "1" / "cmdline").chmod(0)
    assert module.network_alive("holder", proc) or os.access(proc / "1" / "cmdline", os.R_OK)  # (root reads it)
