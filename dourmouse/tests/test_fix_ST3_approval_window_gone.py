"""STRESS1-2 (2026-10-27): closing the window with an approval card open froze every other chat for
the full 300 s confirmation timeout. A card nobody can see is now declined within about a second."""

from __future__ import annotations

import socket
import threading
import time

from dourmouse.webui import WebConfirmationGate, _connection_alive, _PendingConfirmation


def test_a_card_declines_within_a_second_when_the_window_has_gone():
    p = _PendingConfirmation("c1", "send the mail?")
    gone = threading.Event()
    threading.Timer(0.3, gone.set).start()
    t0 = time.perf_counter()
    approved = p.wait(gone.is_set)
    assert approved is False and p.answered is False
    assert time.perf_counter() - t0 < 3.0  # was 300 s


def test_an_answer_still_wins_and_the_probe_is_not_needed():
    p = _PendingConfirmation("c2", "x")
    threading.Timer(0.2, lambda: p.resolve(True)).start()
    assert p.wait(lambda: False) is True


def test_a_broken_probe_never_approves():
    p = _PendingConfirmation("c3", "x")

    def _bad():
        raise RuntimeError("probe broke")

    import dourmouse.webui as w

    old = w._CONFIRM_TIMEOUT_SECONDS
    w._CONFIRM_TIMEOUT_SECONDS = 1.5
    try:
        assert p.wait(_bad) is False  # timed out, not approved
    finally:
        w._CONFIRM_TIMEOUT_SECONDS = old


def test_connection_alive_tells_an_open_socket_from_a_closed_one():
    a, b = socket.socketpair()
    try:
        assert _connection_alive(a) is True  # open and idle
        b.close()
        assert _connection_alive(a) is False  # the peer closed
    finally:
        a.close()


def test_the_gate_passes_its_probe_to_the_card():
    gate = WebConfirmationGate(lambda e: None)
    seen = []
    gate.set_emit(lambda e: seen.append(e))
    gone = threading.Event()
    gate.set_should_stop(gone.is_set)
    threading.Timer(0.3, gone.set).start()
    t0 = time.perf_counter()
    assert gate("really delete everything?") is False
    assert time.perf_counter() - t0 < 3.0
    assert seen and seen[0]["type"] == "confirmation_requested"


def test_a_new_turn_resets_the_probe():
    gate = WebConfirmationGate(lambda e: None)
    gate.set_should_stop(lambda: True)
    gate.set_emit(lambda e: None)  # a new turn installs its own stream
    assert gate._should_stop is None


def test_a_cut_off_upload_is_not_saved_as_a_complete_file(tmp_path, monkeypatch):
    """ST-4: the bytes that arrived before the client left used to be kept as the whole file."""
    import http.client

    import dourmouse.webui as webui_module
    from dourmouse.tests.test_webui import _echo_registry

    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = webui_module.run_server(_echo_registry(), port=0, client=None, config=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.send(f"POST /api/upload?name=cut.bin HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: 100000\r\n\r\n".encode() + b"x" * 5000)
        s.shutdown(socket.SHUT_WR)  # the client leaves with 95,000 bytes still owed
        time.sleep(1.0)
        s.close()
        assert not list((tmp_path / "ws" / "uploads").glob("cut.bin"))
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/upload?name=whole.bin", body=b"y" * 2000)
        assert c.getresponse().status == 200  # a complete upload still works
        assert (tmp_path / "ws" / "uploads" / "whole.bin").stat().st_size == 2000
    finally:
        srv.shutdown()
        srv.server_close()
