# Testing Dourmouse

Real facts, gathered directly from this repo and this session's own audit
work (2026-09-17), not aspirational process. Update the numbers here
whenever they drift instead of leaving them stale.

## Running the suite

```bash
.venv/bin/python -m pytest dourmouse/tests -q
```

Current state: 4849 passed, 10 skipped, 0 failed, ~6 minutes wall time.
Every commit in `docs/ENGINEERING_AUDIT.md` states the exact pass/skip
count it was verified against — if your local run disagrees, something
real changed and the number in the commit message you're comparing
against is now what's stale, not a discrepancy to explain away.

196 test files under `dourmouse/tests/`. A `conftest.py` at that root
supplies fixtures shared across all of them (see below) — nothing in
this directory hierarchy is a second, separate test root.

## Why 10 are skipped, not 0

Every skip in this suite names a real, checked environmental precondition
(the same "honest failure over fabrication" rule the product code itself
follows) — never a silenced flaky test:

- `pytest.skip("node not on PATH in this environment")` — a handful of UI
  tests shell out to a real headless-browser-adjacent Node script; a
  machine without Node skips honestly rather than faking a DOM result.
- `pytest.skip("Ollama not reachable on 127.0.0.1:11434 ...")` and the
  real-model-catalog tests — genuinely require a live local Ollama daemon.
- `pytest.skip("sandbox-exec not available on this machine")` — macOS-only
  sandboxing primitive some ATLAS proposal tests exercise for real.
- `pytest.skip(f"real openwakeword model unavailable ...")` — needs the
  actual wakeword model files present.

If your run shows a different skip count, check which of these
preconditions changed on your machine before assuming a regression.

## Hermetic-by-default: `conftest.py`'s autouse fixtures

`dourmouse/tests/conftest.py` defines a set of `autouse=True` fixtures
that isolate every test from this developer's real machine state:
workspace, config directory, and a growing list of real environment
variables (`DOURMOUSE_NET`, `DOURMOUSE_MEMORY_REMOTE_URL`/`_TOKEN`,
`DOURMOUSE_HANDS_FREE`/`WAKEWORD`, `DOURMOUSE_AGENT_ROUTER_AUTO`,
`DOURMOUSE_ORCHESTRATOR_MODE`, `DOURMOUSE_DENOISE`, `OLLAMA_API_KEY`/
`OLLAMA_CLOUD_MODEL`, `DOURMOUSE_FAST_LANE_MODEL_SWAP`,
`DOURMOUSE_GDELT_POLLER`, the `DOURMOUSE_DESKTOP_RAG_*` family).

This list is not decorative. Read the comments on each fixture in
`conftest.py` directly — they document a real, recurring bug class this
project has hit repeatedly: `dourmouse.config`'s module-level
`load_dotenv()` pulls THIS machine's real `.env` into `os.environ` the
moment any test imports it, completely independent of any individual
test's own `monkeypatch.setenv`/`delenv` calls. Every time a new real
feature has shipped with its own env var and this developer's own `.env`
happened to already set it (Grounded Mode, the memory remote URL, the
agent router, Ollama Cloud, the fast-lane model swap), unrelated tests
started silently exercising a code path they were never written to
cover, and in two documented cases this produced real, confusing
failures before the leak was traced to its actual source.

**If you add a new `DOURMOUSE_*` (or similar) environment variable that
changes real behavior**: assume this developer's real `.env` may already
set it, and add an autouse fixture here that clears/resets it to the
hermetic default, following the exact pattern already established
(`monkeypatch.delenv(..., raising=False)` or `monkeypatch.setenv(...,
"<safe default>")`). A test that specifically wants the real behavior
sets the variable itself inside that test — the override-the-fixture
convention every existing fixture in this file already uses. Do not skip
this step because "it works on my machine" — that is exactly the failure
mode this file exists to prevent.

## Static analysis and type checking

Both are dev-only (`requirements-dev.txt`), both configured in
`pyproject.toml`, both added during the 2026-09-17 engineering audit
(`docs/ENGINEERING_AUDIT.md`), and both are real gates that have already
found and fixed live bugs — not process theater:

```bash
.venv/bin/python -m ruff check dourmouse
.venv/bin/python -m mypy dourmouse
```

`ruff`'s rule set is curated (`[tool.ruff.lint] select = [...]` in
`pyproject.toml`), not the full default catalogue — the comment above it
explains why (1,354 findings under the full catalogue, dominated by noise
irrelevant to this codebase's actual style). `mypy`'s config is
deliberately lenient (`ignore_missing_imports`, no strict mode) because
this is a first-ever pass on a previously-unannotated 337-file codebase;
see finding #019 for the full triage and the honestly-tracked remaining
backlog in both tools (`docs/GODSPEED_ROADMAP.md` Phase 1).

Neither tool is wired into a CI gate yet — there is no CI pipeline for
this repo yet at all (tracked, honest gap, not an oversight).

## Git-history secret scanning

Not a Python dependency (`gitleaks` is a Homebrew-installed Go binary,
not in `requirements-dev.txt`) and not run on every commit — a periodic,
manual check:

```bash
brew install gitleaks   # one-time
gitleaks git --log-opts="--all"
```

Scans every commit on every branch, not just the current working tree
(`git grep`-style scans miss anything committed once and later removed).
See finding #018 for the full methodology and the last real result.

## Coverage

`pytest-cov` is installed (`requirements-dev.txt`) but there is no
enforced coverage threshold and no standard invocation documented yet —
an honest, tracked gap, not a claim that coverage is measured today. To
generate a real report:

```bash
.venv/bin/python -m pytest dourmouse/tests --cov=dourmouse --cov-report=term-missing
```

## What "done" means for a test in this codebase

The house rule, stated explicitly in multiple places across this
session's own audit findings and worth repeating here: **an existing test
passing is evidence of what someone thought should work, not proof that
the system works.** Two real, previously-invisible bugs this session
(`docs/ENGINEERING_AUDIT.md` findings #011 and #019) survived specifically
*because* the only test touching that code path monkeypatched the buggy
function itself, or never called the real function at all. When you add
a test for a fix, prefer exercising the real function with a fake at the
boundary (network, subprocess, filesystem) over mocking the function
under test — the fake-transport pattern used throughout
`test_gemini_backend.py` is the house convention to follow.
