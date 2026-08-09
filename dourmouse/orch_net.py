"""Neural orchestration layer (v5.6) — a neural network that learns, purely,
how Dourmouse should orchestrate itself.

Two orchestration decisions are learned from real outcomes:

1. MULTI-STEP DETECTION — ``p(multi_step | prompt)``: should this request get
   a numbered plan and the heavier brain? Today the planner's deterministic
   verb/sequence heuristic decides. The net learns the SAME decision from
   evidence: a request that actually ended up needing >= 2 tool calls was
   multi-step, whether or not the heuristic smelled it.
2. AGENT ROUTING — ``p(agent | prompt)`` over the registered subagents: which
   agent should own this request? Today ``find_agents_for_query`` scores
   token overlap. The net learns from the agent whose tools were ACTUALLY
   used in past runs, weighted by outcome quality and operator feedback.

The model is a real neural network trained by hand-written backprop in pure
numpy (no torch/sklearn dependency — it trains in under a second on this
machine and stays zero-install):

- Features  x in R^519 : a hashing-trick bag-of-words over the prompt (each
  token hashes to one of 512 bits via SHA-256, so the vocabulary never
  needs a fixed list) plus 7 scalar features (length, outcome verbs,
  sequence markers, question words, tool-name mentions, domain words, hour
  of day — hour is injectable so tests stay deterministic).
- Forward:  h = ReLU(W1 x + b1)            (64 hidden units)
            p_multi = sigmoid(w2 . h + b2) (multi-step head)
            z = W3 h + b3,  p_agent = softmax(z)   (routing head)
- Loss:     L = BCE(p_multi, y_multi) + CE(p_agent, y_agent) + lambda*L2
            where y_agent is masked for pure-chat rows (no agent label),
            and every row is weighted by outcome quality and feedback.
- Optimizer: Adam — m_t = b1*m_{t-1} + (1-b1)*g_t,
                     v_t = b2*v_{t-1} + (1-b2)*g_t^2,
                     theta_t = theta_{t-1} - eta * mhat_t / (sqrt(vhat_t)+eps)
            with bias-corrected moments; early stopping on a held-out split.

SAFETY (house rule 2.8 — orchestration stays deterministic, no LLM judgment;
the net is a deterministic function of learned weights, never an LLM):
- The net BLENDS, never replaces. Routing adds ``0.5 * max(0, logit)`` to
  the token-overlap score; its maximum boost (~2) is below the deterministic
  tool-mention (+5), domain (+4), and name (+3) bonuses, so it can refine
  ties but never overturn a strong deterministic match.
- Multi-step planning is ``heuristic OR (net confident > 0.65)`` — a false
  positive merely adds an honest plan block; behavior is unchanged until the
  net is trained.
- Activation is gated: DOURMOUSE_NET off, fewer than ``_MIN_EXPERIENCES``
  records, or no trained weights all degrade to today's behavior with zero
  regression. NOT CONFIGURED is reported honestly, never claimed learned.
- Retraining is incremental and automatic: every 20 new experiences (or when
  operator feedback lands) a background thread retrains and saves new
  weights under a lock; the model never changes mid-predict.
- The store lives under <workspace>/neuro/ (workspace/ is git-ignored, so
  learned state is runtime state, not source). Experience records are
  deduplicated by a content hash, so bootstrap replay and feedback rewrites
  are idempotent.

Env gates:
- DOURMOUSE_NET=0 / false / no / off  -> layer fully off (no store created).
- DOURMOUSE_NET_DIR                   -> where the store lives (default:
  <workspace>/neuro). Tests point this at a tmp_path.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

_NET_ENV = "DOURMOUSE_NET"
_NET_DIR_ENV = "DOURMOUSE_NET_DIR"
_WORKSPACE_ENV = "DOURMOUSE_WORKSPACE"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_OFF_VALUES = {"", "0", "false", "no", "off"}

# ---- Architecture & training constants ----------------------------------- #
_HASH_DIM = 512      # hashing-trick bag-of-words width
_N_SCALARS = 7       # scalar features appended after the bag
_FEATURE_DIM = _HASH_DIM + _N_SCALARS
_HIDDEN = 64         # hidden layer width
_MIN_EXPERIENCES = 25  # floor before the net may influence orchestration
_RETRAIN_EVERY = 20  # new experiences since last train before auto-retrain
_P_MULTI_THRESHOLD = 0.65  # conservative OR-branch for plan creation
_ROUTE_LAMBDA = 0.5  # blend weight for neural routing logits
_L2 = 1e-4
_LR = 1e-2
_BETAS = (0.9, 0.999)
_EPS = 1e-8
_EPOCHS = 300
_BATCH = 32
_EARLY_STOP = 15

# Scalar-feature detectors (kept small and local so orch_net stays
# independent of planner's lists — no import cycle, no shared mutable state).
_VERB_RE = re.compile(
    r"\b(?:search|find|write|create|draft|run|check|list|summarize|fetch|"
    r"open|delete|read|propose|compare|build|test|fix|debug|convert|send|"
    r"save|make|edit|scan|monitor)\b"
)
_SEQ_RE = re.compile(r"\b(?:then|after|next|afterwards|finally|also)\b")
_QUESTION_RE = re.compile(
    r"\b(?:what|how|why|when|where|who|which|can|should|could|would)\b"
)
_TOOL_RE = re.compile(r"\b[a-z0-9_]{3,}_[a-z0-9_]{3,}\b")
_DOMAIN_WORDS = frozenset(
    {
        "inbox", "email", "emails", "gmail", "mail", "news", "headline",
        "weather", "btc", "bitcoin", "stock", "stocks", "quote", "quotes",
        "price", "prices", "forex", "task", "tasks", "todo", "calendar",
        "schedule", "meeting", "wikipedia",
    }
)


# --------------------------------------------------------------------------- #
# Gate & paths
# --------------------------------------------------------------------------- #
def orch_enabled(value: str | None = None) -> bool:
    """DOURMOUSE_NET gate. Anything but 0/false/no/off/empty enables it.

    ``value`` overrides the env (test seam, mirroring learn.learn_enabled).
    """
    if value is None:
        value = os.environ.get(_NET_ENV, "1")
    return str(value).strip().lower() not in _OFF_VALUES


def default_store_dir() -> Path:
    """DOURMOUSE_NET_DIR env, else <workspace>/neuro (mirrors learn)."""
    raw = os.environ.get(_NET_DIR_ENV)
    if raw:
        return Path(raw).expanduser()
    wraw = os.environ.get(_WORKSPACE_ENV)
    root = Path(wraw).expanduser() if wraw else _PROJECT_ROOT / "workspace"
    return root / "neuro"


# --------------------------------------------------------------------------- #
# Featurizer — prompt -> fixed-size numeric vector (hashing trick + scalars)
# --------------------------------------------------------------------------- #
def featurize(prompt: str, hour: float | None = None) -> np.ndarray:
    """Deterministic numeric fingerprint of a prompt.

    Bag-of-words via the hashing trick: each token hashes (SHA-256 first
    byte) to one of ``_HASH_DIM`` bits, so the feature space is fixed no
    matter how the vocabulary grows. Seven normalized scalar features are
    appended. ``hour`` is injectable (defaults to local hour) so tests and
    bootstrap replay are deterministic.
    """
    vec = np.zeros(_FEATURE_DIM, dtype=np.float64)
    lower = prompt.lower()
    for tok in re.findall(r"[a-z0-9_]{2,}", lower):
        vec[hashlib.sha256(tok.encode()).digest()[0] % _HASH_DIM] = 1.0
    if hour is None:
        hour = float(datetime.now().hour)  # noqa: DTZ005 - house pattern: naive local time
    verbs = len(_VERB_RE.findall(lower))
    seq = len(_SEQ_RE.findall(lower))
    q = len(_QUESTION_RE.findall(lower))
    tools = len(_TOOL_RE.findall(prompt))
    dom = sum(1 for w in _DOMAIN_WORDS if re.search(rf"\b{re.escape(w)}\b", lower))
    vec[_HASH_DIM:] = (
        min(1.0, len(prompt) / 400.0),
        min(1.0, verbs / 5.0),
        min(1.0, seq / 3.0),
        min(1.0, q / 4.0),
        min(1.0, tools / 4.0),
        min(1.0, dom / 4.0),
        min(1.0, hour / 24.0),
    )
    return vec


# --------------------------------------------------------------------------- #
# The model — a 2-layer MLP with two heads, trained by hand-written backprop
# --------------------------------------------------------------------------- #
def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


class OrchNet:
    """Small MLP: ReLU hidden layer -> multi-step sigmoid head + agent
    softmax head. Weights are plain numpy arrays, saved with ``np.savez``.
    """

    def __init__(
        self, dim: int = _FEATURE_DIM, hidden: int = _HIDDEN,
        n_agents: int = 1, seed: int = 7,
    ) -> None:
        self.dim = dim
        self.hidden = hidden
        self.n_agents = n_agents
        rng = np.random.default_rng(seed)
        self.W1 = rng.normal(0.0, math.sqrt(2.0 / dim), (hidden, dim))
        self.b1 = np.zeros(hidden)
        self.w2 = rng.normal(0.0, math.sqrt(2.0 / hidden), hidden)
        self.b2 = 0.0
        self.W3 = rng.normal(0.0, math.sqrt(2.0 / hidden), (n_agents, hidden))
        self.b3 = np.zeros(n_agents)

    # -- forward --------------------------------------------------------- #
    def forward(
        self, x: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Returns (p_multi, p_agent, z_agent) as arrays: shapes (N,),
        (N, K), (N, K) where N=1 when ``x`` is a single sample (D,)."""
        X = x.reshape(1, -1) if x.ndim == 1 else x
        h = np.maximum(0.0, X @ self.W1.T + self.b1)
        z_multi = h @ self.w2 + self.b2
        p_multi = _sigmoid(z_multi)
        z_agents = h @ self.W3.T + self.b3
        zs = z_agents - z_agents.max(axis=1, keepdims=True)
        e = np.exp(zs)
        p_agents = e / e.sum(axis=1, keepdims=True)
        return p_multi, p_agents, z_agents

    # -- loss & gradients ------------------------------------------------- #
    def _loss(
        self, X: np.ndarray, y_multi: np.ndarray, y_agent: np.ndarray,
        w: np.ndarray, l2: float,
    ) -> float:
        p_multi, p_agents, _ = self.forward(X)
        eps = 1e-12
        lm = -float(
            np.mean(
                w * (
                    y_multi * np.log(p_multi + eps)
                    + (1.0 - y_multi) * np.log(1.0 - p_multi + eps)
                )
            )
        )
        mask = y_agent >= 0
        la = 0.0
        if mask.any():
            rows = np.nonzero(mask)[0]
            pick = p_agents[rows, y_agent[rows]]
            la = -float(np.sum(w[rows] * np.log(pick + eps)) / len(rows))
        reg = l2 * (
            float(np.sum(self.W1 ** 2))
            + float(np.sum(self.w2 ** 2))
            + float(np.sum(self.W3 ** 2))
        )
        return lm + la + reg

    def _grads(
        self, X: np.ndarray, y_multi: np.ndarray, y_agent: np.ndarray,
        w: np.ndarray, l2: float,
    ) -> dict[str, np.ndarray]:
        """Analytic gradients via the chain rule (verified against finite
        differences in tests). Only the masked agent rows contribute to the
        routing head; every row contributes to the multi-step head."""
        h = np.maximum(0.0, X @ self.W1.T + self.b1)
        n = X.shape[0]
        p_multi, p_agents, _ = self.forward(X)
        # The loss uses MEANS (BCE over the batch, CE over the labeled rows),
        # so the gradients carry the same denominators — without them the
        # analytic gradients are exactly batch-size too large (caught by the
        # finite-difference check in tests).
        g_z_multi = (p_multi - y_multi) * w / n
        g_z_agents = np.zeros_like(p_agents)
        mask = y_agent >= 0
        if mask.any():
            rows = np.nonzero(mask)[0]
            onehot = np.zeros_like(p_agents[rows])
            onehot[np.arange(len(rows)), y_agent[rows]] = 1.0
            g_z_agents[rows] = (p_agents[rows] - onehot) * w[rows, None] / len(rows)
        g_h = g_z_multi[:, None] * self.w2[None, :] + g_z_agents @ self.W3
        g_h[h <= 0.0] = 0.0
        # d/dW of lambda*||W||^2 is 2*lambda*W (the L2 term lives in the
        # loss as lambda * sum(W**2)); the finite-difference check in tests
        # pins this exactly.
        grads = {
            "W1": g_h.T @ X + 2.0 * l2 * self.W1,
            "b1": g_h.sum(axis=0),
            "w2": g_z_multi @ h + 2.0 * l2 * self.w2,
            "b2": float(g_z_multi.sum()),
            "W3": g_z_agents.T @ h + 2.0 * l2 * self.W3,
            "b3": g_z_agents.sum(axis=0),
        }
        return grads

    # -- training ---------------------------------------------------------- #
    def fit(
        self,
        X: np.ndarray,
        y_multi: np.ndarray,
        y_agent: np.ndarray,
        w: np.ndarray,
        Xv: np.ndarray,
        yv_multi: np.ndarray,
        yv_agent: np.ndarray,
        wv: np.ndarray,
        epochs: int = _EPOCHS,
        batch: int = _BATCH,
        lr: float = _LR,
        l2: float = _L2,
        early_stop: int = _EARLY_STOP,
        seed: int = 0,
    ) -> dict[str, float]:
        """Adam mini-batch training with early stopping on the held-out set.

        Returns {loss, train_acc, val_acc, agent_acc} for the BEST weights
        (restored at the end, so the saved model is the validation winner,
        not the last epoch's).
        """
        n = X.shape[0]
        params = ["W1", "b1", "w2", "b2", "W3", "b3"]
        m = {p: np.zeros_like(getattr(self, p)) for p in params}
        v = {p: np.zeros_like(getattr(self, p)) for p in params}
        rng = np.random.default_rng(seed)
        best_val = float("inf")
        best_weights: dict[str, np.ndarray] = {
            p: np.array(getattr(self, p), copy=True) for p in params
        }
        no_improve = 0
        t = 0
        for _epoch in range(epochs):
            perm = rng.permutation(n)
            for i in range(0, n, batch):
                idx = perm[i : i + batch]
                grads = self._grads(X[idx], y_multi[idx], y_agent[idx], w[idx], l2)
                t += 1
                for p in params:
                    g = grads[p]
                    m[p] = _BETAS[0] * m[p] + (1.0 - _BETAS[0]) * g
                    v[p] = _BETAS[1] * v[p] + (1.0 - _BETAS[1]) * g * g
                    mhat = m[p] / (1.0 - _BETAS[0] ** t)
                    vhat = v[p] / (1.0 - _BETAS[1] ** t)
                    setattr(
                        self, p,
                        getattr(self, p) - lr * mhat / (np.sqrt(vhat) + _EPS),
                    )
            val = self._loss(Xv, yv_multi, yv_agent, wv, l2)
            if val < best_val - 1e-6:
                best_val = val
                best_weights = {
                    p: np.array(getattr(self, p), copy=True) for p in params
                }
                no_improve = 0
            else:
                no_improve += 1
                if no_improve >= early_stop:
                    break
        for p in params:
            setattr(self, p, best_weights[p])
        return {
            "loss": float(self._loss(Xv, yv_multi, yv_agent, wv, l2)),
            "train_acc": _multi_acc(self, X, y_multi),
            "val_acc": _multi_acc(self, Xv, yv_multi),
            "agent_acc": _agent_acc(self, Xv, yv_agent),
        }

    # -- persistence -------------------------------------------------------- #
    def save(self, path: Path) -> None:
        np.savez(
            path,
            W1=self.W1, b1=self.b1, w2=self.w2, b2=np.array([self.b2]),
            W3=self.W3, b3=self.b3,
        )

    @classmethod
    def load(cls, path: Path, dim: int = _FEATURE_DIM,
             hidden: int = _HIDDEN, n_agents: int | None = None) -> OrchNet:
        data = np.load(path)
        n = n_agents if n_agents is not None else data["W3"].shape[0]
        net = cls(dim=dim, hidden=hidden, n_agents=n)
        net.W1 = data["W1"]
        net.b1 = data["b1"]
        net.w2 = data["w2"]
        net.b2 = float(data["b2"][0])
        net.W3 = data["W3"]
        net.b3 = data["b3"]
        return net


def _multi_acc(net: OrchNet, X: np.ndarray, y_multi: np.ndarray) -> float:
    if len(X) == 0:
        return 0.0
    p, _, _ = net.forward(X)
    return float(np.mean((p >= 0.5).astype(float) == y_multi))


def _agent_acc(net: OrchNet, X: np.ndarray, y_agent: np.ndarray) -> float:
    if len(X) == 0:
        return 0.0
    mask = y_agent >= 0
    if not mask.any():
        return 0.0
    _, p, _ = net.forward(X)
    pred = p[mask].argmax(axis=1)
    return float(np.mean(pred == y_agent[mask]))


# --------------------------------------------------------------------------- #
# Experience store — what the system actually did, and how well
# --------------------------------------------------------------------------- #
@dataclass
class NeuroStore:
    """Files: experiences.jsonl (append-only, deduped by content hash),
    orch_net.npz (weights), meta.json (training bookkeeping)."""

    base: Path
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _ids: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        if self.experiences_path.is_file():
            try:
                for line in self.experiences_path.read_text(
                    errors="replace"
                ).splitlines():
                    if line.strip():
                        self._ids.add(str(json.loads(line).get("id", "")))
            except (json.JSONDecodeError, OSError):
                pass

    @property
    def experiences_path(self) -> Path:
        return self.base / "experiences.jsonl"

    @property
    def weights_path(self) -> Path:
        return self.base / "orch_net.npz"

    @property
    def meta_path(self) -> Path:
        return self.base / "meta.json"

    # -- records ----------------------------------------------------------- #
    def log_experience(self, record: dict[str, Any]) -> bool:
        """Append one orchestration experience; False when it's a duplicate.

        ``record`` keys: prompt, ts (ISO), and optional session_stem,
        plan_given, tools_used, agents_used, agents_scoped, outcome_ok,
        feedback, model. The id is a content hash so bootstrap replay and
        live logging can never double-count the same turn.
        """
        prompt = str(record.get("prompt") or "")
        if not prompt.strip():
            return False
        rec: dict[str, Any] = {
            "id": hashlib.sha256(
                f"{prompt}|{record.get('ts', '')}".encode()
            ).hexdigest()[:16],
            "prompt": prompt[:500],
            "ts": record.get("ts")
            or datetime.now().isoformat(  # noqa: DTZ005 - house pattern
                timespec="seconds"
            ),
            "session_stem": record.get("session_stem"),
            "plan_given": bool(record.get("plan_given")),
            "tools_used": [str(t) for t in (record.get("tools_used") or [])][:20],
            "agents_used": [str(a) for a in (record.get("agents_used") or [])][:20],
            "agents_scoped": [str(a) for a in (record.get("agents_scoped") or [])][:20],
            "outcome_ok": bool(record.get("outcome_ok")),
            "feedback": record.get("feedback"),
            "model": record.get("model"),
        }
        with self._lock:
            if rec["id"] in self._ids:
                return False
            self._ids.add(rec["id"])
            with self.experiences_path.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            return True

    def count(self) -> int:
        return len(self._ids)

    def load_experiences(self) -> list[dict[str, Any]]:
        if not self.experiences_path.is_file():
            return []
        out: list[dict[str, Any]] = []
        try:
            for line in self.experiences_path.read_text(errors="replace").splitlines():
                if line.strip():
                    out.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            return []
        return out

    def apply_feedback(self, session_stem: str, rating: str) -> int:
        """Reweight every experience from a session with an operator rating.

        The rating is the reward signal: "good" lifts the sample weight,
        "bad" suppresses it, so the net unlearns what the operator disliked.
        Marks the store dirty so the next auto-retrain fires even though the
        record count did not change. Returns how many records were updated.
        """
        rating = (rating or "").strip().lower()
        if rating not in ("good", "bad"):
            raise ValueError("rating must be 'good' or 'bad'")
        updated = 0
        with self._lock:
            recs = self.load_experiences()
            changed = False
            for rec in recs:
                if rec.get("session_stem") == session_stem:
                    rec["feedback"] = rating
                    updated += 1
                    changed = True
            if changed:
                with self.experiences_path.open("w") as fh:
                    for rec in recs:
                        fh.write(json.dumps(rec) + "\n")
                self._ids = {str(r.get("id", "")) for r in recs}
                meta = self._read_meta()
                meta["dirty"] = True
                self._write_meta(meta)
        return updated

    # -- meta --------------------------------------------------------------- #
    def _read_meta(self) -> dict[str, Any]:
        if not self.meta_path.is_file():
            return {}
        try:
            return json.loads(self.meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_meta(self, meta: dict[str, Any]) -> None:
        self.meta_path.write_text(json.dumps(meta, indent=2))

    # -- training ----------------------------------------------------------- #
    def train(self, agent_names: Sequence[str] | None = None) -> dict[str, Any]:
        """Train on all logged experiences; save weights + meta on success.

        Labels are self-supervised from outcomes: y_multi = 1 iff the run
        used >= 2 tool calls; y_agent = the most-used agent (masked for pure
        chat rows); sample weight = 1.0 clean / 0.5 flawed, x2 for operator
        "good", x0.25 for "bad". The agent vocabulary is the union of the
        caller's hint (registry roster) and every agent actually used, so
        the softmax head always covers the full roster.
        """
        recs = self.load_experiences()
        if not recs:
            return {"error": "no experiences to train on"}
        roster: list[str] = [str(a) for a in (agent_names or [])]
        used = sorted({a for r in recs for a in r.get("agents_used") or []})
        vocab: list[str] = []
        for name in [*roster, *used]:
            if name not in vocab:
                vocab.append(name)
        if not vocab:
            return {"error": "no agents in experiences"}
        name_to_idx = {name: i for i, name in enumerate(vocab)}
        X: list[np.ndarray] = []
        y_multi: list[float] = []
        y_agent: list[int] = []
        w: list[float] = []
        for rec in recs:
            ts = rec.get("ts") or ""
            hour = 12.0
            try:
                hour = float(datetime.fromisoformat(ts).hour)
            except (ValueError, TypeError):
                pass
            X.append(featurize(str(rec.get("prompt") or ""), hour=hour))
            tools = rec.get("tools_used") or []
            y_multi.append(1.0 if len(tools) >= 2 else 0.0)
            agents = rec.get("agents_used") or []
            if agents:
                # Most-used agent wins; ties break alphabetically (stable).
                counts: dict[str, int] = {}
                for a in agents:
                    counts[a] = counts.get(a, 0) + 1
                best = max(sorted(counts), key=lambda a: counts[a])
                y_agent.append(name_to_idx.get(best, 0))
            else:
                y_agent.append(-1)
            weight = 1.0 if rec.get("outcome_ok") else 0.5
            fb = rec.get("feedback")
            if fb == "good":
                weight *= 2.0
            elif fb == "bad":
                weight *= 0.25
            w.append(weight)
        Xa = np.stack(X)
        y_ma = np.array(y_multi, dtype=np.float64)
        y_aa = np.array(y_agent, dtype=np.int64)
        wa = np.array(w, dtype=np.float64)
        split = max(1, int(len(Xa) * 0.8))
        rng = np.random.default_rng(0)
        perm = rng.permutation(len(Xa))
        tr, va = perm[:split], perm[split:]
        if len(va) == 0:
            va = tr
        net = OrchNet(dim=_FEATURE_DIM, hidden=_HIDDEN, n_agents=len(vocab))
        metrics = net.fit(
            Xa[tr], y_ma[tr], y_aa[tr], wa[tr],
            Xa[va], y_ma[va], y_aa[va], wa[va],
        )
        net.save(self.weights_path)
        meta = self._read_meta()
        meta.update(
            {
                "trained_count": len(recs),
                "last_train": datetime.now().isoformat(timespec="seconds"),  # noqa: DTZ005 - house pattern
                "agent_names": vocab,
                "dirty": False,
                **{f"metric_{k}": v for k, v in metrics.items()},
            }
        )
        self._write_meta(meta)
        return {
            "trained_count": len(recs),
            "agents": len(vocab),
            **metrics,
        }

    def maybe_train(self, agent_names: Sequence[str] | None = None) -> dict[str, Any] | None:
        """Auto-retrain when due: >= _MIN_EXPERIENCES AND (>= _RETRAIN_EVERY
        new records since the last train OR operator feedback marked dirty).
        Returns the train report, or None when not due."""
        meta = self._read_meta()
        count = self.count()
        trained = int(meta.get("trained_count", 0))
        if count < _MIN_EXPERIENCES:
            return None
        if (count - trained) < _RETRAIN_EVERY and not meta.get("dirty"):
            return None
        return self.train(agent_names)

    def status(self) -> dict[str, Any]:
        meta = self._read_meta()
        count = self.count()
        trained = bool(self.weights_path.is_file())
        active = trained and count >= _MIN_EXPERIENCES
        return {
            "enabled": True,
            "active": active,
            "experience_count": count,
            "trained_count": int(meta.get("trained_count", 0)),
            "last_train": meta.get("last_train"),
            "loss": meta.get("metric_loss"),
            "val_acc": meta.get("metric_val_acc"),
            "train_acc": meta.get("metric_train_acc"),
            "agent_acc": meta.get("metric_agent_acc"),
            "agents": meta.get("agent_names", []),
            "store_dir": str(self.base),
        }


# --------------------------------------------------------------------------- #
# Module-level helpers — what dispatch / planner / webui call
# --------------------------------------------------------------------------- #
_TRAIN_LOCK = threading.Lock()
_MODEL_CACHE: dict[str, tuple[float, OrchNet, dict[str, Any]]] = {}


def open_store() -> NeuroStore | None:
    """The live store, or None when the gate is off (honest NOT CONFIGURED)."""
    if not orch_enabled():
        return None
    try:
        return NeuroStore(default_store_dir())
    except OSError:
        return None


def _load_active_model() -> tuple[OrchNet, dict[str, Any]] | None:
    """Cached (net, meta) if active: enabled + trained + enough experiences.

    The cache is invalidated by weights-file mtime, so a background retrain
    is picked up on the next prediction without any locks on the hot path.
    """
    store = open_store()
    if store is None or not store.weights_path.is_file():
        return None
    try:
        mtime = store.weights_path.stat().st_mtime
    except OSError:
        return None
    key = str(store.weights_path)
    cached = _MODEL_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1], cached[2]
    meta = store._read_meta()
    if store.count() < _MIN_EXPERIENCES:
        return None
    agents = meta.get("agent_names") or []
    if not agents:
        return None
    net = OrchNet.load(store.weights_path, n_agents=len(agents))
    _MODEL_CACHE[key] = (mtime, net, meta)
    return net, meta


def neural_is_multi_step(prompt: str) -> bool:
    """Learned multi-step detector: True only when the net is active AND
    confident (p > _P_MULTI_THRESHOLD). False otherwise — the planner's
    deterministic heuristic is the floor, this only adds."""
    loaded = _load_active_model()
    if loaded is None:
        return False
    net, _ = loaded
    p_multi, _, _ = net.forward(featurize(prompt))
    return bool(float(p_multi[0]) > _P_MULTI_THRESHOLD)


def neural_agent_scores(
    query: str, agent_names: Sequence[str],
) -> dict[str, float] | None:
    """Learned routing evidence: {agent_name: logit} for the requested agents.

    Unknown agents (new to the roster, never trained) get 0.0 — the blend
    then adds nothing for them, so they still win only on deterministic
    evidence. Returns None when inactive (caller falls back to pure
    deterministic scoring)."""
    loaded = _load_active_model()
    if loaded is None:
        return None
    net, meta = loaded
    vocab = meta.get("agent_names") or []
    name_to_idx = {name: i for i, name in enumerate(vocab)}
    _, _, z = net.forward(featurize(query))
    scores: dict[str, float] = {}
    for name in agent_names:
        idx = name_to_idx.get(name)
        scores[str(name)] = float(z[0, idx]) if idx is not None else 0.0
    return scores


def log_experience(
    record: dict[str, Any], agent_names: Sequence[str] | None = None,
) -> bool:
    """Live experience sink for dispatch: append + trigger auto-retrain.

    ``agent_names`` (the full roster) hints the routing head's vocabulary so
    it always covers every registered agent, not only those seen in data.
    """
    store = open_store()
    if store is None:
        return False
    added = store.log_experience(record)
    if added:
        _schedule_retrain(agent_names)
    return added


def _schedule_retrain(agent_names: Sequence[str] | None = None) -> None:
    """Background retrain — never blocks a reply, never two trainers at once.

    The lock is acquired HERE (single-flight) and released inside the worker
    thread, so a trainer already running causes the new request to be
    dropped (the running trainer's next trigger will cover the new data).
    """
    if not _TRAIN_LOCK.acquire(blocking=False):
        return

    def _work() -> None:
        try:
            store = open_store()
            if store is not None:
                store.maybe_train(agent_names)
        finally:
            _TRAIN_LOCK.release()

    threading.Thread(target=_work, daemon=True, name="neuro-retrain").start()


def retrain_now(agent_names: Sequence[str] | None = None) -> bool:
    """Force a background retrain (POST /api/neuro/train).

    Returns False when a trainer is already running or the gate is off.
    The caller polls status() for the result — training is async.
    """
    if not _TRAIN_LOCK.acquire(blocking=False):
        return False

    def _work() -> None:
        try:
            store = open_store()
            if store is not None:
                store.train(agent_names)
        finally:
            _TRAIN_LOCK.release()

    threading.Thread(target=_work, daemon=True, name="neuro-retrain").start()
    return True


def apply_feedback(session_stem: str, rating: str) -> int:
    """Operator rating -> sample weights, then a background retrain."""
    store = open_store()
    if store is None:
        return 0
    updated = store.apply_feedback(session_stem, rating)
    if updated:
        _schedule_retrain()
    return updated


def status() -> dict[str, Any]:
    """Honest overall status for the HUD: the gate can be off, the store can
    be untrained, or the net can be live — each is reported distinctly."""
    if not orch_enabled():
        return {
            "enabled": False,
            "active": False,
            "experience_count": 0,
            "trained_count": 0,
            "last_train": None,
            "loss": None,
            "val_acc": None,
            "train_acc": None,
            "agent_acc": None,
            "agents": [],
            "store_dir": None,
            "note": "DOURMOUSE_NET is off — orchestration uses the "
                    "deterministic heuristics; nothing is learned.",
        }
    store = open_store()
    if store is None:
        return {
            "enabled": True, "active": False, "experience_count": 0,
            "trained_count": 0, "last_train": None, "loss": None,
            "val_acc": None, "train_acc": None, "agent_acc": None,
            "agents": [], "store_dir": None,
            "note": "store unavailable",
        }
    return store.status()


def bootstrap_from_sessions(sessions_dir: Path | None = None) -> dict[str, Any]:
    """Replay historical session JSONL transcripts into experiences.

    Gives the net real history on first run instead of starting from zero.
    Every record with a user prompt becomes an experience (pure-chat turns
    are valuable multi-step negatives); tool calls map to agent owners via
    the general roster. Idempotent: content-hash dedupe means re-running
    adds nothing. Returns {added, total_considered}."""
    store = open_store()
    if store is None:
        return {"added": 0, "total_considered": 0, "note": "disabled"}
    if sessions_dir is None:
        wraw = os.environ.get(_WORKSPACE_ENV)
        root = Path(wraw).expanduser() if wraw else _PROJECT_ROOT / "workspace"
        sessions_dir = root / "sessions"
    sd = Path(sessions_dir)
    if not sd.is_dir():
        return {"added": 0, "total_considered": 0, "note": "no sessions dir"}
    owner: dict[str, str] = {}
    try:
        from dourmouse.general_roster import build_general_registry

        for sub in build_general_registry().all_subagents():
            for tool in sub.tools:
                owner[tool.name] = sub.name
    except Exception:  # noqa: BLE001, S110 - roster failure just skips ownership
        pass
    added = 0
    considered = 0
    for path in sorted(sd.glob("*.jsonl")):
        try:
            for line in path.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                user = (rec.get("user") or "").strip()
                if not user:
                    continue
                transcript = rec.get("transcript") or []
                if not isinstance(transcript, list):
                    continue
                considered += 1
                tools_used = [
                    str(e.get("name"))
                    for e in transcript
                    if isinstance(e, dict) and e.get("type") == "tool_use"
                ]
                agents_used: list[str] = []
                for t in tools_used:
                    o = owner.get(t)
                    if o and o not in agents_used:
                        agents_used.append(o)
                has_caveat = any(
                    isinstance(e, dict)
                    and (e.get("type") == "plan_reminder"
                         or (e.get("type") == "assistant_text"
                             and "not executed via tools" in str(e.get("text") or "")))
                    for e in transcript
                )
                tool_errors = any(
                    isinstance(e, dict) and e.get("type") == "tool_result"
                    and str(e.get("text") or "").startswith(("ERROR", "REFUSED"))
                    for e in transcript
                )
                exhausted = any(
                    isinstance(e, dict) and e.get("type") == "result"
                    and e.get("is_error")
                    for e in transcript
                )
                plan_given = any(
                    isinstance(e, dict) and e.get("type") == "plan"
                    for e in transcript
                )
                record = {
                    "prompt": user,
                    "ts": rec.get("timestamp") or path.stem,
                    "session_stem": path.stem,
                    "plan_given": plan_given,
                    "tools_used": tools_used,
                    "agents_used": agents_used,
                    "outcome_ok": bool(
                        not has_caveat and not tool_errors and not exhausted
                        and (rec.get("final_text") or "")
                    ),
                }
                if store.log_experience(record):
                    added += 1
        except (OSError, json.JSONDecodeError):
            continue
    if added:
        _schedule_retrain()
    return {"added": added, "total_considered": considered}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Neural orchestration layer")
    parser.add_argument(
        "--train", action="store_true", help="train now (default when --status not given)"
    )
    parser.add_argument("--bootstrap", action="store_true", help="replay session history")
    parser.add_argument("--status", action="store_true", help="print status")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status(), indent=2))
    else:
        if args.bootstrap:
            print("bootstrap:", bootstrap_from_sessions())
        store = open_store()
        if store is None:
            print("NOT CONFIGURED: DOURMOUSE_NET is off")
        else:
            report = store.train()
            print("train:", json.dumps(report, indent=2, default=str))
            print(json.dumps(status(), indent=2))
