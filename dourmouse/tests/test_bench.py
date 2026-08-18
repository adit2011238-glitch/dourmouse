"""The benchmark must be trustworthy before its numbers justify a model swap."""

from __future__ import annotations

import json

import pytest

from dourmouse import bench
from dourmouse.bench import ModelResult, PromptResult, Sample, percentile


# --------------------------------------------------------------------------- #
# percentile — the statistic the whole comparison rests on
# --------------------------------------------------------------------------- #

def test_percentile_of_empty_is_none_not_a_crash():
    assert percentile([], 50) is None


def test_percentile_of_one_sample_works():
    """--repeat 1 is a legitimate smoke run; it must not raise."""
    assert percentile([42.0], 50) == 42.0
    assert percentile([42.0], 95) == 42.0


def test_percentile_picks_a_real_observation():
    values = [10.0, 20.0, 30.0, 40.0, 100.0]
    assert percentile(values, 50) == 30.0
    assert percentile(values, 95) == 100.0


def test_p95_tracks_the_tail_that_a_mean_would_hide():
    """The reason p95 is the headline and mean is not."""
    values = [10.0] * 19 + [5000.0]
    assert percentile(values, 50) == 10.0
    assert percentile(values, 95) >= 5000.0
    mean = sum(values) / len(values)
    assert mean > 200  # a mean would misreport this as uniformly slow


def test_percentile_is_order_independent():
    assert percentile([30.0, 10.0, 20.0], 50) == percentile([10.0, 20.0, 30.0], 50)


# --------------------------------------------------------------------------- #
# Sample
# --------------------------------------------------------------------------- #

def test_tokens_per_second_computed_from_completion_tokens_only():
    s = Sample(ms=2000.0, ok=True, completion_tokens=100, prompt_tokens=9999)
    assert s.tokens_per_second == 50.0  # prompt tokens must not inflate it


@pytest.mark.parametrize(
    "sample",
    [
        Sample(ms=100.0, ok=False, completion_tokens=10),
        Sample(ms=100.0, ok=True, completion_tokens=None),
        Sample(ms=0.0, ok=True, completion_tokens=10),
    ],
    ids=["failed", "no-usage", "zero-duration"],
)
def test_tokens_per_second_is_none_when_not_computable(sample):
    assert sample.tokens_per_second is None


# --------------------------------------------------------------------------- #
# run_model
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake_caller():
    """Deterministic caller: fixed latency, records what it was asked."""
    seen: list[str] = []

    def call(text: str) -> Sample:
        seen.append(text)
        return Sample(ms=100.0 + len(seen), ok=True, completion_tokens=20)

    call.seen = seen  # type: ignore[attr-defined]
    return call


def test_runs_every_prompt_the_requested_number_of_times(fake_caller):
    prompts = [{"id": "a", "category": "x", "text": "A"}, {"id": "b", "category": "y", "text": "B"}]

    result = bench.run_model("m", repeat=3, prompts=prompts, caller=fake_caller)

    assert len(result.prompts) == 2
    assert all(len(p.samples) == 3 for p in result.prompts)
    # 2 prompts x 3 reps, plus one cold-start call
    assert len(fake_caller.seen) == 7


def test_cold_start_is_recorded_and_excluded_from_the_samples(fake_caller):
    """A model load is a real cost, but averaging it in hides steady state."""
    prompts = [{"id": "a", "category": "x", "text": "A"}]

    result = bench.run_model("m", repeat=2, prompts=prompts, caller=fake_caller)

    assert result.cold_start_ms == 101.0        # the first call
    assert result.prompts[0].ok_ms() == [102.0, 103.0]  # and not among these


def test_repeat_below_one_is_clamped(fake_caller):
    prompts = [{"id": "a", "category": "x", "text": "A"}]
    result = bench.run_model("m", repeat=0, prompts=prompts, caller=fake_caller)
    assert len(result.prompts[0].samples) == 1


def test_failed_calls_are_data_not_exceptions():
    def flaky(text: str) -> Sample:
        return Sample(ms=50.0, ok=False, error="boom")

    prompts = [{"id": "a", "category": "x", "text": "A"}]
    result = bench.run_model("m", repeat=2, prompts=prompts, caller=flaky)
    s = result.prompts[0].summary()

    assert s["n"] == 2
    assert s["n_ok"] == 0
    assert s["p50_ms"] is None      # no successful samples to summarise
    assert "boom" in s["errors"][0]


def test_partial_failure_summarises_only_successes():
    calls = {"n": 0}

    def half(text: str) -> Sample:
        calls["n"] += 1
        if calls["n"] % 2:
            return Sample(ms=9999.0, ok=False, error="timeout")
        return Sample(ms=100.0, ok=True, completion_tokens=10)

    prompts = [{"id": "a", "category": "x", "text": "A"}]
    result = bench.run_model("m", repeat=4, prompts=prompts, caller=half)
    s = result.prompts[0].summary()

    assert s["n"] == 4
    assert s["n_ok"] == 2
    assert s["p50_ms"] == 100.0  # the 9999ms failures must not skew it


def test_progress_callback_reports_each_prompt(fake_caller):
    lines: list[str] = []
    prompts = [{"id": "alpha", "category": "x", "text": "A"}]

    bench.run_model("m", repeat=1, prompts=prompts, caller=fake_caller, on_progress=lines.append)

    assert any("cold start" in ln for ln in lines)
    assert any("alpha" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# prompt set
# --------------------------------------------------------------------------- #

def test_prompt_ids_are_unique():
    ids = [p["id"] for p in bench.PROMPTS]
    assert len(ids) == len(set(ids))


def test_prompt_set_spans_distinct_cost_profiles():
    """A single-category set would average away real regressions."""
    categories = {p["category"] for p in bench.PROMPTS}
    assert len(categories) >= 5


def test_every_prompt_has_non_empty_text():
    assert all(p["text"].strip() for p in bench.PROMPTS)


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def test_summary_is_json_serialisable(fake_caller):
    prompts = [{"id": "a", "category": "x", "text": "A"}]
    result = bench.run_model("m", repeat=2, prompts=prompts, caller=fake_caller)
    json.dumps(result.summary())  # must not raise


def test_compare_orders_fastest_first():
    def result_at(model: str, ms: float) -> ModelResult:
        pr = PromptResult(prompt_id="a", category="x")
        pr.samples = [Sample(ms=ms, ok=True, completion_tokens=10)]
        return ModelResult(model=model, cold_start_ms=1.0, prompts=[pr])

    table = bench.compare([result_at("slow", 900.0), result_at("fast", 100.0)])
    lines = [ln for ln in table.splitlines() if ln.startswith(("fast", "slow"))]

    assert lines[0].startswith("fast")


def test_compare_tolerates_a_model_where_everything_failed():
    pr = PromptResult(prompt_id="a", category="x")
    pr.samples = [Sample(ms=10.0, ok=False, error="dead")]
    dead = ModelResult(model="dead", prompts=[pr])

    table = bench.compare([dead])  # must not raise on None p50
    assert "dead" in table


def test_cli_requires_a_model():
    with pytest.raises(SystemExit):
        bench.main([])
