"""Finding #108 (MS-10): a network change triggers a scan at once, and every
scan reaches its listeners (the live event stream, the analyst)."""

from __future__ import annotations

import threading

from dourmouse.security import netwatch as nw
from dourmouse.security.sentry import SentryRuntime, SentryStore


def test_the_first_read_is_a_baseline_and_only_real_changes_fire():
    ids = iter([("10.0.0.1", "en0", "Home"), ("10.0.0.1", "en0", "Home"), ("172.20.10.1", "en0", "Phone"),
                ("", "", "offline")])
    seen = []
    w = nw.NetworkWatcher(on_change=lambda a, b: seen.append((a, b)), identity_fn=lambda: next(ids))
    assert [w.poll() for _ in range(4)] == [False, False, True, True]
    assert seen[0] == (("10.0.0.1", "en0", "Home"), ("172.20.10.1", "en0", "Phone")) and w.changes == 2
    assert nw.describe(seen[1][1]) == "offline"
    assert nw.describe(("172.20.10.1", "en0", "Phone")) == "Phone via en0 (router 172.20.10.1)"


def _state():
    return {"interfaces": {"available": True, "interfaces": []},
            "default_gateway": {"available": True, "gateway": "10.0.0.1", "interface": "en0"},
            "dns": {"available": True, "resolvers": []}, "arp_neighbors": {"available": True, "neighbors": []},
            "listening_ports": {"available": True, "listening_ports": []},
            "firewall": {"available": True, "enabled": False}}


def test_a_scan_reaches_every_listener_and_a_broken_one_does_not_stop_it(tmp_path):
    rt = SentryRuntime(store=SentryStore(tmp_path / "s.db"), state_fn=_state)
    got = []
    rt.add_listener(lambda r, why: 1 / 0)
    rt.add_listener(lambda r, why: got.append(nw.scan_event(r, why)))
    rt.run_one_tick_now("network_change")
    assert got[0]["type"] == "security_scan" and got[0]["reason"] == "network_change"
    assert got[0]["counts"]["high"] >= 1  # the firewall-off finding


def test_concurrent_scans_run_one_at_a_time(tmp_path):
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow_state():
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        import time
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return _state()

    rt = SentryRuntime(store=SentryStore(tmp_path / "s.db"), state_fn=slow_state)
    threads = [threading.Thread(target=rt.run_one_tick_now) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1 and rt.tick_count == 4
