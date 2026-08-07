# v4.0 Phase 13 — the system reviews itself, honestly.
# --------------------------------------------------------------------------- #
# The digest is a pure reduction over bus traffic: exact arithmetic, zero
# fabrication. These tests pin the counting rules, the idle/active heuristics,
# the roster tool wiring, and the HTTP surface.

from datetime import datetime, timezone
from pathlib import Path

from dourmouse.self_improve import build_daily_digest, digest_from_messages


def _msg(msg_id: int, frm: str, to: str, subject: str, at: str) -> dict:
    return {
        "id": f"msg-{msg_id}",
        "from": frm,
        "to": to,
        "subject": subject,
        "body": "x",
        "at": at,
    }


class TestDigestArithmetic:
    def test_counts_sent_received_and_top_activity(self):
        msgs = [
            _msg(1, "news", "BROADCAST", "markets up", "2026-08-06T08:00:00"),
            _msg(2, "news", "BROADCAST", "markets up", "2026-08-06T08:05:00"),
            _msg(3, "tasks", "BROADCAST", "TASKS: none", "2026-08-06T08:06:00"),
            _msg(4, "news", "BROADCAST", "markets up", "2026-08-06T08:07:00"),
        ]
        digest = digest_from_messages(
            msgs, ["news", "tasks", "mail"], now=datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
        )
        news = digest["agents"]["news"]
        assert news["sent"] == 3
        assert news["top_activity"] == "markets up"
        assert news["activity_count"] == 3
        assert news["last_sent_at"] == "2026-08-06T08:07:00"
        # broadcast -> no per-agent receive credit, but the dict stays complete
        assert digest["agents"]["mail"]["sent"] == 0
        assert digest["agents"]["mail"]["received"] == 0
        assert digest["message_count"] == 4

    def test_idle_agent_flagged_silent(self):
        msgs = [_msg(1, "news", "BROADCAST", "x", "2026-08-06T08:00:00")]
        digest = digest_from_messages(msgs, ["news", "mail"], now=datetime(2026, 8, 6, tzinfo=timezone.utc))
        flagged = [s for s in digest["suggestions"] if "mail" in s and "no bus traffic" in s]
        assert flagged, "silent agent must be reported as silent, never invented"

    def test_active_agent_drives_suggestion(self):
        msgs = [_msg(i, "news", "BROADCAST", "feed", f"2026-08-06T08:0{i}:00") for i in range(4)]
        digest = digest_from_messages(msgs, ["news"], now=datetime(2026, 8, 6, tzinfo=timezone.utc))
        assert any("most active via 'feed'" in s for s in digest["suggestions"])

    def test_empty_bus_stays_honest(self):
        digest = digest_from_messages([], ["a", "b"], now=datetime(2026, 8, 6, tzinfo=timezone.utc))
        assert digest["message_count"] == 0
        assert digest["agents"]["a"]["sent"] == 0
        assert digest["suggestions"], "no anomalies found is itself a report"


class _FakeSub:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeRegistry:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def all_subagents(self):
        return [_FakeSub(n) for n in self._names]


class TestLiveDigest:
    def test_build_daily_digest_returns_full_shape(self, monkeypatch):
        # Use the real process bus but a tiny fake registry, then post one msg.
        from dourmouse import message_bus

        bus = message_bus.MessageBus()
        monkeypatch.setattr(message_bus, "set_message_bus", lambda b=None: None)
        # isolate: put the fake bus where get_message_bus reads it
        monkeypatch.setattr(message_bus, "_DEFAULT_BUS", bus)
        bus.post("news", "BROADCAST", "feed ok", "sample body")
        digest = build_daily_digest(_FakeRegistry(["news", "mail"]))
        assert digest["message_count"] == 1
        assert digest["agents"]["news"]["sent"] == 1
        assert isinstance(digest["suggestions"], list)

    def test_roster_ships_daily_digest_tool(self):
        src = (Path(__file__).resolve().parents[2] / "dourmouse" / "general_roster.py").read_text()
        assert "daily_digest" in src
        assert "_build_memory_subagent" in src

    def test_webui_ships_selfimprove_route(self):
        src = (Path(__file__).resolve().parents[2] / "dourmouse" / "webui.py").read_text()
        assert "/api/selfimprove" in src
