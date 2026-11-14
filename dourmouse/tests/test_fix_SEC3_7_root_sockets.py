"""SEC3-7 (high): listening-port and connection telemetry only saw the
owner's own processes, so root daemons and root malware were invisible.

`lsof` run as the user lists only that user's sockets. On this Mac on
2026-11-13 it showed none of the root-owned listeners that `netstat -anv`
shows with their owning process: launchd on *:22, *:445, *:3031, *:5900
(Remote Login, File Sharing, Remote Application Scripting, Screen Sharing,
all socket-activated) and kdc on *:88. The samples below are lines captured
from that Mac (the user name replaced with "owner").
"""

from __future__ import annotations

import pytest

from dourmouse.security import baseline as bl
from dourmouse.security import platform_adapter as pa
from dourmouse.security import sentry

NETSTAT = """\
Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address                                 Foreign Address                               (state)          rxbytes      txbytes  rhiwat  shiwat          process:pid    state  options           gencnt    flags   flags1 usecnt rtncnt fltrs
tcp4       0      0  192.168.1.226.51881    35.186.224.26.443      ESTABLISHED         3944         2354  131072  131600   Spotify Helper:51672  00102 00000008 0000000001abe2cb 00000080 04000900      2      0 000000
tcp4       0      0  192.168.1.226.51880    34.213.235.91.443      ESTABLISHED         9498         2207  131072  131600 Google Chrome He:41992  00102 00000008 0000000001abe28e 00000080 04000900      2      0 000000
tcp4       0      0  192.168.1.226.50999    17.57.146.20.5223      ESTABLISHED         1200          900  131072  131600    mDNSResponder:412   00102 00000008 0000000001abe100 00000080 04000900      2      0 000000
tcp4       0      0  *.57621                *.*                    LISTEN                 0            0  131072  131072          Spotify:51667  00100 00000002 0000000001a995fd 00000000 00000800      1      0 000000
tcp6       0      0  *.54123                *.*                    LISTEN                 0            0  131072  131072         rapportd:618    00100 00000006 0000000001a983a4 00000001 00000800      1      0 000000
tcp4       0      0  *.54123                *.*                    LISTEN                 0            0  131072  131072         rapportd:618    00100 00000006 0000000001a983a3 00000001 00000800      1      0 000000
tcp6       0      0  ::1.18789              *.*                    LISTEN                 0            0  131072  131072             node:835    00100 00000106 0000000000001986 00000001 00000800      1      0 000000
tcp4       0      0  *.88                   *.*                    LISTEN                 0            0  131072  131072              kdc:388    00180 00000006 0000000000000902 00000001 00000800      1      0 000000
tcp6       0      0  *.88                   *.*                    LISTEN                 0            0  131072  131072              kdc:388    00180 00000006 0000000000000900 00000001 00000800      1      0 000000
tcp4       0      0  *.445                  *.*                    LISTEN                 0            0  131072  131072          launchd:1      00180 00000006 000000000000073b 00000000 00000800      1      0 000000
tcp4       0      0  *.5900                 *.*                    LISTEN                 0            0  131072  131072          launchd:1      00180 00000006 0000000000000734 00000000 00000800      1      0 000000
tcp4       0      0  *.22                   *.*                    LISTEN                 0            0  131072  131072          launchd:1      00180 00000006 000000000000072e 00000000 00000800      1      0 000000
tcp6       0      0  *.22                   *.*                    LISTEN                 0            0  131072  131072          launchd:1      00180 00000006 000000000000072d 00000000 00000800      1      0 000000
"""

LSOF_LISTEN = """\
COMMAND     PID        USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
rapportd    618 owner   10u  IPv4 0x4b492925fbaf98f4      0t0  TCP *:54123 (LISTEN)
rapportd    618 owner   11u  IPv6 0x4779c9eff8531454      0t0  TCP *:54123 (LISTEN)
node        835 owner   25u  IPv6 0xb8e5ac64bfbdffc7      0t0  TCP [::1]:18789 (LISTEN)
Spotify   51667 owner   94u  IPv4 0xd65566c34d16448b      0t0  TCP *:57621 (LISTEN)
"""

LSOF_ESTABLISHED = """\
COMMAND     PID        USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
Spotify   51672 owner   30u  IPv4 0x1111111111111111      0t0  TCP 192.168.1.226:51881->35.186.224.26:443 (ESTABLISHED)
Google    41992 owner   31u  IPv4 0x2222222222222222      0t0  TCP 192.168.1.226:51880->34.213.235.91:443 (ESTABLISHED)
"""

PS = "    1 root\n  388 root\n  412 _mdnsresponder\n  618 owner\n  835 owner\n41992 owner\n51667 owner\n51672 owner\n"


@pytest.fixture(autouse=True)
def _owner_is_the_current_user(monkeypatch):
    monkeypatch.setattr(pa, "_current_user", lambda: "owner")


def _fake_run(netstat=NETSTAT, lsof_listen=LSOF_LISTEN, lsof_est=LSOF_ESTABLISHED, ps=PS):
    def run(cmd, timeout=5.0):
        if cmd[0].endswith("netstat"):
            return (netstat is not None, netstat if netstat is not None else "netstat: not permitted")
        if cmd[0].endswith("ps"):
            return True, ps
        if cmd[0] == "lsof" and "-sTCP:LISTEN" in cmd:
            return True, lsof_listen
        if cmd[0] == "lsof":
            return True, lsof_est
        raise AssertionError(f"unexpected command {cmd}")
    return run


def test_netstat_lines_name_the_owner_of_every_socket():
    rows = pa.parse_netstat_anv(NETSTAT)
    assert rows is not None
    ssh = [r for r in rows if r["local_port"] == 22]
    assert {(r["command"], r["pid"], r["family"], r["local_address"]) for r in ssh} == {
        ("launchd", 1, "IPv4", "*"), ("launchd", 1, "IPv6", "*")}
    spaced = next(r for r in rows if r["pid"] == 51672)
    assert spaced["command"] == "Spotify Helper" and spaced["state"] == "ESTABLISHED"
    assert (spaced["remote_address"], spaced["remote_port"]) == ("35.186.224.26", 443)
    loop6 = next(r for r in rows if r["local_port"] == 18789)
    assert loop6["local_address"] == "::1"


def test_an_unknown_netstat_layout_is_not_guessed():
    old = "Active Internet connections\nProto Recv-Q Send-Q Local Address Foreign Address (state)\ntcp4 0 0 *.22 *.* LISTEN\n"
    assert pa.parse_netstat_anv(old) is None


def test_root_listeners_are_merged_with_their_owner(monkeypatch):
    monkeypatch.setattr(pa, "_run", _fake_run())
    out = pa.get_listening_ports()
    assert out["available"] is True and out["complete"] is True
    by_port = {}
    for p in out["listening_ports"]:
        by_port.setdefault(p["port"], []).append(p)
    # Root listeners lsof cannot see are now there, with their owner and service.
    assert {p["command"] for p in by_port[22]} == {"launchd"}
    assert all(p["user"] == "root" and p["source"] == "netstat" for p in by_port[22])
    assert by_port[22][0]["exposure"] == "ALL_INTERFACES"
    assert by_port[22][0]["service"] == "Remote Login (SSH)"
    assert by_port[445][0]["service"] == "File Sharing (SMB)"
    assert by_port[88][0]["command"] == "kdc"
    # What lsof already saw is unchanged and not duplicated.
    assert [p["command"] for p in by_port[54123]] == ["rapportd", "rapportd"]
    assert all("source" not in p for p in by_port[54123])
    assert len(by_port[57621]) == 1 and len(by_port[18789]) == 1


def test_when_netstat_fails_the_partial_view_says_so(monkeypatch):
    monkeypatch.setattr(pa, "_run", _fake_run(netstat=None))
    out = pa.get_listening_ports()
    assert out["available"] is True and out["complete"] is False
    assert "only this user" in out["gaps"][0].lower()
    assert {p["port"] for p in out["listening_ports"]} == {54123, 18789, 57621}


def test_root_connections_are_merged(monkeypatch):
    monkeypatch.setattr(pa, "_run", _fake_run())
    out = pa.get_established_connections()
    assert out["complete"] is True
    root = [c for c in out["connections"] if c.get("source") == "netstat"]
    assert [(c["command"], c["pid"], c["user"], c["remote_port"]) for c in root] == [
        ("mDNSResponder", 412, "_mdnsresponder", 5223)]
    assert len(out["connections"]) == 3  # lsof's two are not repeated


def _scan_state(ports, conns=()):
    return {"firewall": {"available": True, "enabled": True},
            "listening_ports": {"available": True, "complete": True, "listening_ports": list(ports)},
            "established_connections": {"available": True, "complete": True, "connections": list(conns)},
            "arp_neighbors": {"available": True, "neighbors": []}}


def _port(command, port, user="root", source="netstat", service=None, pid=1):
    p = {"command": command, "pid": pid, "user": user, "family": "IPv4", "protocol": "TCP", "bind_address": "*",
         "port": port, "exposure": "ALL_INTERFACES"}
    if source:
        p["source"] = source
    if service:
        p["service"] = service
    return p


def test_a_root_listener_on_all_interfaces_is_a_finding():
    findings = sentry._detect_findings(_scan_state([_port("launchd", 445, service="File Sharing (SMB)")]))
    [f] = [f for f in findings if f.kind == "exposed_port"]
    assert "File Sharing (SMB)" in f.title and "445" in f.title
    assert "System Settings" in f.recommended_action


def test_ssh_is_not_reported_twice_when_the_remote_login_check_already_says_so():
    state = _scan_state([_port("launchd", 22, service="Remote Login (SSH)")])
    assert [f.kind for f in sentry._detect_findings(state)].count("exposed_port") == 1
    state["host_protections"] = {"checks": {"remote_login": {"on": True, "accepting": True}}}
    assert [f.kind for f in sentry._detect_findings(state)].count("exposed_port") == 0


def _learn(store, state, t0=0.0):
    t = t0
    for _ in range(bl.LEARNING_MIN_SCANS + 1):
        sentry.run_scan(state_fn=lambda: state, store=store, now=lambda t=t: t, write_alerts=False)
        t += bl.LEARNING_MIN_SECONDS
    return t


def test_listeners_that_were_always_there_are_not_called_new(tmp_path):
    store = sentry.SentryStore(tmp_path / "s.db")
    lsof_only = _scan_state([_port("rapportd", 54123, user="me", source=None, pid=618)])
    lsof_only["listening_ports"].pop("complete")
    t = _learn(store, lsof_only)
    full = _scan_state([_port("rapportd", 54123, user="me", source=None, pid=618), _port("launchd", 22)])
    first = sentry.run_scan(state_fn=lambda: full, store=store, now=lambda: t, write_alerts=False)
    assert "new_listening_port" not in [f.kind for f in first.all_findings]
    # A root listener that really is new after that is reported.
    later = _scan_state([_port("rapportd", 54123, user="me", source=None, pid=618), _port("launchd", 22),
                         _port("evil", 4444, pid=999)])
    second = sentry.run_scan(state_fn=lambda: later, store=store, now=lambda: t + 1, write_alerts=False)
    assert [f.title for f in second.all_findings if f.kind == "new_listening_port"] == [
        "evil started listening on TCP port 4444"]


def test_a_partial_view_does_not_clear_findings(tmp_path, monkeypatch):
    store = sentry.SentryStore(tmp_path / "s.db")
    marked = []
    monkeypatch.setattr(store, "mark_absent", lambda present: marked.append(present))
    state = _scan_state([])
    for key in ("interfaces", "default_gateway", "dns"):
        state[key] = {"available": True}
    sentry.run_scan(state_fn=lambda: state, store=store, write_alerts=False)
    assert len(marked) == 1  # a complete view does clear findings that are gone
    state["listening_ports"]["complete"] = False
    sentry.run_scan(state_fn=lambda: state, store=store, write_alerts=False)
    assert len(marked) == 1  # a partial one does not


@pytest.mark.skipif(not __import__("sys").platform == "darwin", reason="live macOS check")
def test_live_netstat_parses_on_this_mac():
    ok, out = pa._run(["/usr/sbin/netstat", "-anv", "-p", "tcp"], timeout=10.0)
    assert ok
    rows = pa.parse_netstat_anv(out)
    assert rows is not None and all(r["pid"] is None or r["pid"] > 0 for r in rows)
