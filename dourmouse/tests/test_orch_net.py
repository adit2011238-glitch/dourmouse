"""Tests for the neural orchestration layer (v5.6).

Covers the math (analytic gradients vs finite differences — the hand-written
backprop must be exactly right), the learning loop (featurizer, training,
store dedupe, feedback reweighting, auto-retrain thresholds), the SAFETY
properties (gate off / untrained -> zero change to orchestration; the blend
can refine ties but never overturn a deterministic match), and the wiring
(dispatch experience logging, planner OR-branch and routing blend, bootstrap
idempotence).
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from dourmouse import orch_net as orch_net_module
from dourmouse import planner
from dourmouse.dispatch import (
    DispatchRegistry,
    Subagent,
    ToolSpec,
    run_dispatch_messages,
)
from dourmouse.orch_net import (
    _FEATURE_DIM,
    _HASH_DIM,
    NeuroStore,
    OrchNet,
    bootstrap_from_sessions,
    featurize,
    neural_agent_scores,
    neural_is_multi_step,
    open_store,
    orch_enabled,
    status,
)


# --------------------------------------------------------------------------- #
# Gate & paths
# --------------------------------------------------------------------------- #
class TestGate:
    def test_on_by_default(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_NET", raising=False)
        assert orch_enabled() is True

    def test_off_values(self, monkeypatch):
        for v in ("0", "false", "no", "off"):
            monkeypatch.setenv("DOURMOUSE_NET", v)
            assert orch_enabled() is False

    def test_explicit_override(self):
        assert orch_enabled("0") is False
        assert orch_enabled("1") is True

    def test_store_dir_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_NET_DIR", str(tmp_path / "custom"))
        assert orch_net_module.default_store_dir() == tmp_path / "custom"

    def test_workspace_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DOURMOUSE_NET_DIR", raising=False)
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        assert orch_net_module.default_store_dir() == tmp_path / "neuro"


# --------------------------------------------------------------------------- #
# Featurizer
# --------------------------------------------------------------------------- #
class TestFeaturize:
    def test_shape_and_deterministic(self):
        v1 = featurize("check my inbox then draft a reply", hour=9.0)
        v2 = featurize("check my inbox then draft a reply", hour=9.0)
        assert v1.shape == (_FEATURE_DIM,)
        assert v1.dtype == np.float64
        assert np.array_equal(v1, v2)

    def test_distinct_prompts_differ(self):
        a = featurize("hello world", hour=9.0)
        b = featurize("search the web for news", hour=9.0)
        assert not np.array_equal(a, b)

    def test_scalars_normalized(self):
        v = featurize("what is the price of bitcoin now please", hour=12.0)
        scalars = v[_HASH_DIM:]
        assert (scalars >= 0.0).all() and (scalars <= 1.0).all()
        assert scalars[-1] == pytest.approx(12.0 / 24.0)

    def test_hour_injectable(self):
        assert featurize("hi", hour=0.0)[-1] == 0.0
        assert featurize("hi", hour=24.0)[-1] == 1.0


# --------------------------------------------------------------------------- #
# The math — forward shapes + gradients vs finite differences
# --------------------------------------------------------------------------- #
class TestOrchNetMath:
    def test_forward_shapes_and_softmax(self):
        net = OrchNet(dim=_FEATURE_DIM, hidden=8, n_agents=3)
        x = featurize("draft an email", hour=10.0)
        p_multi, p_agents, z_agents = net.forward(x)
        assert p_multi.shape == (1,)
        assert 0.0 < float(p_multi[0]) < 1.0
        assert p_agents.shape == (1, 3)
        assert np.isclose(p_agents.sum(), 1.0)
        assert z_agents.shape == (1, 3)

    def test_batched_forward(self):
        net = OrchNet(dim=_FEATURE_DIM, hidden=8, n_agents=2)
        X = np.stack([featurize("a", hour=1.0), featurize("b", hour=2.0)])
        p_m, p_a, _ = net.forward(X)
        assert p_m.shape == (2,)
        assert p_a.shape == (2, 2)
        assert np.allclose(p_a.sum(axis=1), 1.0)

    def test_grads_match_finite_differences(self):
        """The hand-written backprop must be analytically exact — this is the
        whole point of the finite-difference check."""
        rng = np.random.default_rng(3)
        net = OrchNet(dim=8, hidden=4, n_agents=3, seed=1)
        X = rng.normal(size=(2, 8))
        y_m = np.array([1.0, 0.0])
        y_a = np.array([2, -1])  # row 1 is masked (pure chat)
        w = np.array([1.3, 0.7])
        l2 = 1e-4
        grads = net._grads(X, y_m, y_a, w, l2)
        eps = 1e-6

        def loss() -> float:
            return net._loss(X, y_m, y_a, w, l2)

        for name in ("W1", "b1", "w2", "W3", "b3"):
            arr = getattr(net, name)
            flat = arr.reshape(-1)
            numeric = np.zeros_like(flat)
            for i in range(flat.size):
                orig = flat[i]
                flat[i] = orig + eps
                hi = loss()
                flat[i] = orig - eps
                lo = loss()
                flat[i] = orig
                numeric[i] = (hi - lo) / (2.0 * eps)
            assert np.allclose(grads[name].reshape(-1), numeric, atol=1e-5), name
        orig_b2 = net.b2
        net.b2 = orig_b2 + eps
        hi = loss()
        net.b2 = orig_b2 - eps
        lo = loss()
        net.b2 = orig_b2
        assert np.isclose(grads["b2"], (hi - lo) / (2.0 * eps), atol=1e-5)


# --------------------------------------------------------------------------- #
# Training actually learns
# --------------------------------------------------------------------------- #
class TestTraining:
    def _seed_store(self, store: NeuroStore, pos: int, neg: int) -> None:
        for i in range(pos):
            store.log_experience(
                {
                    "prompt": f"check the inbox and then draft a reply to sender {i}",
                    "ts": f"2026-08-09T09:0{i % 5}:00",
                    "tools_used": ["read_mail", "write_draft"],
                    "agents_used": ["mail"],
                    "outcome_ok": True,
                }
            )
        for i in range(neg):
            store.log_experience(
                {
                    "prompt": f"hello number {i} how are you doing",
                    "ts": f"2026-08-09T10:0{i % 5}:00",
                    "tools_used": [],
                    "agents_used": [],
                    "outcome_ok": True,
                }
            )

    def test_training_fits_synthetic_labels(self, tmp_path):
        store = NeuroStore(tmp_path / "neuro")
        self._seed_store(store, pos=16, neg=16)
        report = store.train(["mail"])
        assert "loss" in report
        assert report["train_acc"] >= 0.8
        assert store.weights_path.is_file()
        s = store.status()
        assert s["active"] is True
        assert s["trained_count"] == 32
        assert s["agents"] == ["mail"]

    def test_training_saves_and_reloads(self, tmp_path):
        store = NeuroStore(tmp_path / "neuro")
        self._seed_store(store, pos=12, neg=12)
        store.train(["mail"])
        net = OrchNet.load(store.weights_path, n_agents=1)
        p, _, _ = net.forward(featurize("check inbox then draft reply", hour=9.0))
        assert float(p[0]) > 0.5  # the net remembers the positive pattern

    def test_maybe_train_threshold(self, tmp_path):
        store = NeuroStore(tmp_path / "neuro")
        for i in range(24):
            store.log_experience(
                {
                    "prompt": f"hello {i}",
                    "ts": f"2026-08-09T10:{i:02d}:00",
                    "tools_used": [],
                    "agents_used": [],
                    "outcome_ok": True,
                }
            )
        assert store.maybe_train(["mail"]) is None  # below the 25 floor
        store.log_experience(
            {
                "prompt": "hello 24",
                "ts": "2026-08-09T11:00:00",
                "tools_used": [],
                "agents_used": [],
                "outcome_ok": True,
            }
        )
        report = store.maybe_train(["mail"])
        assert report is not None and "loss" in report
        # No new data -> not due again.
        assert store.maybe_train(["mail"]) is None
        # 20 more records -> due again.
        for i in range(20):
            store.log_experience(
                {
                    "prompt": f"more {i}",
                    "ts": f"2026-08-09T12:{i:02d}:00",
                    "tools_used": ["a_b", "c_d"],
                    "agents_used": ["mail"],
                    "outcome_ok": True,
                }
            )
        assert store.maybe_train(["mail"]) is not None


# --------------------------------------------------------------------------- #
# Store — dedupe, feedback, idempotence
# --------------------------------------------------------------------------- #
class TestStore:
    def test_log_dedupe_and_count(self, tmp_path):
        store = NeuroStore(tmp_path / "neuro")
        rec = {"prompt": "p", "ts": "2026-08-09T10:00:00"}
        assert store.log_experience(rec) is True
        assert store.log_experience(rec) is False  # same content hash
        assert store.log_experience({**rec, "ts": "2026-08-09T11:00:00"}) is True
        assert store.count() == 2

    def test_empty_prompt_rejected(self, tmp_path):
        store = NeuroStore(tmp_path / "neuro")
        assert store.log_experience({"prompt": "   ", "ts": "t"}) is False
        assert store.count() == 0

    def test_apply_feedback_reweights(self, tmp_path):
        store = NeuroStore(tmp_path / "neuro")
        store.log_experience(
            {
                "prompt": "do the thing",
                "ts": "2026-08-09T10:00:00",
                "session_stem": "session_1",
                "tools_used": ["a_b"],
                "agents_used": ["mail"],
                "outcome_ok": True,
            }
        )
        assert store.apply_feedback("session_1", "good") == 1
        recs = store.load_experiences()
        assert recs[0]["feedback"] == "good"
        assert store._read_meta().get("dirty") is True
        assert store.apply_feedback("other", "good") == 0
        with pytest.raises(ValueError):
            store.apply_feedback("session_1", "meh")

    def test_status_reports_states(self, tmp_path):
        store = NeuroStore(tmp_path / "neuro")
        s = store.status()
        assert s["enabled"] is True and s["active"] is False
        assert s["experience_count"] == 0


# --------------------------------------------------------------------------- #
# Module helpers — gating and inactive degradation
# --------------------------------------------------------------------------- #
class TestModuleHelpers:
    def test_disabled_gate_no_store(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_NET", "0")
        monkeypatch.setenv("DOURMOUSE_NET_DIR", str(tmp_path / "neuro"))
        assert status()["enabled"] is False
        assert open_store() is None
        assert neural_is_multi_step("anything at all") is False
        assert neural_agent_scores("anything", ["mail"]) is None
        assert not (tmp_path / "neuro").exists()  # never even created

    def test_inactive_without_weights(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_NET", "1")
        monkeypatch.setenv("DOURMOUSE_NET_DIR", str(tmp_path / "neuro"))
        # 40 experiences but no trained weights -> predictions degrade to
        # False/None (zero regression until trained).
        store = NeuroStore(tmp_path / "neuro")
        for i in range(40):
            store.log_experience(
                {
                    "prompt": f"x {i}",
                    "ts": f"2026-08-09T10:{i % 60:02d}:00",
                    "tools_used": [],
                    "agents_used": [],
                    "outcome_ok": True,
                }
            )
        assert neural_is_multi_step("check this then that") is False
        assert neural_agent_scores("check this", ["mail"]) is None


# --------------------------------------------------------------------------- #
# Dispatch integration — one experience per top-level run
# --------------------------------------------------------------------------- #
class _FakeFn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeTc:
    def __init__(self, tc_id: str, name: str, arguments: str) -> None:
        self.id = tc_id
        self.type = "function"
        self.function = _FakeFn(name, arguments)


class _FakeMsg:
    def __init__(self, content: str, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeResponse:
    """OpenAI-shaped response: ``response.choices[0].message``."""

    def __init__(self, msg: _FakeMsg) -> None:
        self.choices = [type("_C", (), {"message": msg})()]


def _resp(content: str, tool_calls=None) -> _FakeResponse:
    return _FakeResponse(_FakeMsg(content, tool_calls))


class _FakeCompletions:
    def __init__(self, script) -> None:
        self.calls: list = []
        self._script = list(script)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._script.pop(0)


class _FakeClient:
    def __init__(self, script) -> None:
        self.chat = type(
            "_Chat", (), {"completions": _FakeCompletions(script)}
        )()


def _tiny_registry() -> DispatchRegistry:
    reg = DispatchRegistry()
    reg.register_subagent(
        Subagent(
            "test_agent",
            "test",
            "echoes text back",
            (
                ToolSpec(
                    "echo",
                    "echo the given text",
                    {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    lambda args: f"echo: {args.get('text', '')}",
                ),
            ),
        )
    )
    return reg


class TestDispatchLogging:
    def test_sink_receives_experience(self):
        reg = _tiny_registry()
        script = [
            _resp("", [_FakeTc("1", "echo", '{"text": "hi"}')]),
            _resp("done"),
        ]
        collected: list = []

        run_dispatch_messages(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "say hi via echo"},
            ],
            reg,
            client=_FakeClient(script),
            experience_sink=collected.append,
            session_stem="session_x",
        )
        assert len(collected) == 1
        rec = collected[0]
        assert rec["prompt"] == "say hi via echo"
        assert rec["tools_used"] == ["echo"]
        assert rec["agents_used"] == ["test_agent"]
        assert rec["session_stem"] == "session_x"
        assert rec["outcome_ok"] is True
        assert rec["model"]

    def test_no_sink_no_write(self):
        reg = _tiny_registry()
        script = [_resp("done")]
        report = run_dispatch_messages(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ],
            reg,
            client=_FakeClient(script),
        )
        assert report["final_text"] == "done"

    def test_nested_runs_do_not_log(self):
        """Only depth-0 runs feed the learner — a delegate must not clobber
        the parent's experience or double-count."""
        reg = _tiny_registry()
        collected: list = []
        script = [_resp("hi")]
        run_dispatch_messages(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ],
            reg,
            client=_FakeClient(script),
            experience_sink=collected.append,
            depth=1,
        )
        assert collected == []


# --------------------------------------------------------------------------- #
# Planner integration — the blend is safe by construction
# --------------------------------------------------------------------------- #
class TestPlannerBlend:
    def _two_agent_registry(self) -> DispatchRegistry:
        reg = DispatchRegistry()
        reg.register_subagent(
            Subagent(
                "mail",
                "mail",
                "reads and sends email",
                (
                    ToolSpec(
                        "read_mail",
                        "read emails",
                        {"type": "object", "properties": {}},
                        lambda a: "ok",
                    ),
                ),
            )
        )
        reg.register_subagent(
            Subagent(
                "tasks",
                "tasks",
                "manages the task list",
                (
                    ToolSpec(
                        "list_tasks",
                        "list tasks",
                        {"type": "object", "properties": {}},
                        lambda a: "ok",
                    ),
                ),
            )
        )
        return reg

    def test_neural_boost_adds_positive_evidence(self, monkeypatch):
        reg = self._two_agent_registry()
        # No token overlap at all -> baseline scores are 0 for both; the
        # net's positive evidence for 'mail' makes it appear.
        monkeypatch.setattr(
            orch_net_module, "neural_agent_scores",
            lambda q, names: {"mail": 5.0, "tasks": 0.0},
        )
        hits = planner.find_agents_for_query(reg, "neutral words with no match")
        assert hits and hits[0]["name"] == "mail"
        assert hits[0]["score"] == pytest.approx(0.5 * 5.0)

    def test_neural_never_overturns_deterministic_match(self, monkeypatch):
        """The safety property: even a maximal neural logit (+5 -> +2.5) sits
        below the deterministic tool-mention/domain bonuses, so a strong
        deterministic match still wins."""
        reg = self._two_agent_registry()
        monkeypatch.setattr(
            orch_net_module, "neural_agent_scores",
            lambda q, names: {"mail": 5.0, "tasks": -2.0},
        )
        hits = planner.find_agents_for_query(reg, "list tasks please")
        assert hits[0]["name"] == "tasks"

    def test_looks_multi_step_neural_or(self, monkeypatch):
        monkeypatch.setattr(orch_net_module, "neural_is_multi_step", lambda p: True)
        assert planner.looks_multi_step("hello there how are you") is True
        monkeypatch.setattr(orch_net_module, "neural_is_multi_step", lambda p: False)
        assert planner.looks_multi_step("hello there how are you") is False

    def test_inactive_net_unchanged(self, monkeypatch):
        """With the gate off, planner behaves exactly as before (neural
        helpers return None/False and the delayed import is a no-op)."""
        monkeypatch.setenv("DOURMOUSE_NET", "0")
        reg = self._two_agent_registry()
        hits = planner.find_agents_for_query(reg, "list tasks please")
        assert hits[0]["name"] == "tasks"
        assert planner.looks_multi_step("hello there") is False


# --------------------------------------------------------------------------- #
# Bootstrap — history replay, idempotent
# --------------------------------------------------------------------------- #
class TestBootstrap:
    def test_replays_and_dedupes(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_NET", "1")
        monkeypatch.setenv("DOURMOUSE_NET_DIR", str(tmp_path / "neuro"))
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "s1.jsonl").write_text(
            json.dumps(
                {
                    "user": "check inbox then reply",
                    "timestamp": "2026-08-09T10:00:00",
                    "final_text": "done",
                    "transcript": [
                        {"type": "tool_use", "name": "read_mail"},
                        {"type": "tool_result", "text": "ok"},
                    ],
                }
            )
            + "\n"
        )
        (sessions / "s2.jsonl").write_text(
            json.dumps(
                {
                    "user": "hello",
                    "timestamp": "2026-08-09T11:00:00",
                    "final_text": "hi",
                    "transcript": [],
                }
            )
            + "\n"
        )
        r1 = bootstrap_from_sessions(sessions)
        assert r1["added"] == 2
        assert r1["total_considered"] == 2
        # Idempotent: replaying adds nothing (content-hash dedupe).
        r2 = bootstrap_from_sessions(sessions)
        assert r2["added"] == 0
        store = open_store()
        assert store is not None
        assert store.count() == 2

    def test_bootstrap_disabled_gate(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOURMOUSE_NET", "0")
        monkeypatch.setenv("DOURMOUSE_NET_DIR", str(tmp_path / "neuro"))
        r = bootstrap_from_sessions(tmp_path)
        assert r["note"] == "disabled"
