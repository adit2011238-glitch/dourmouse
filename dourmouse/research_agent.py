"""Research Agent — wraps ATLAS's real research pipeline as an SDK tool.

Per Integration Rule 7.1, this module never reimplements ATLAS logic. It
locates the real ATLAS repo (via ATLAS_REPO_PATH) and its own runtime venv
(via ATLAS_VENV_PATH), then subprocess-invokes ATLAS's actual
``Atlas().research(...)`` entry point (atlas/core.py) in-process there,
returning its real JSON output.

Until both paths are configured, calling the tool raises NotImplementedError
with a clear message (Rule 2.2 — no silent stubs, never fabricate ATLAS
output).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

# Bundled with this package; run inside ATLAS_VENV_PATH via `python <script>`.
_RUNNER_SCRIPT = Path(__file__).parent / "_atlas_runner.py"


class AtlasNotConfiguredError(NotImplementedError):
    pass


def get_atlas_repo_path() -> Path:
    raw = os.environ.get("ATLAS_REPO_PATH")
    if not raw:
        raise AtlasNotConfiguredError(
            "ATLAS_REPO_PATH is not set. This is a placeholder boundary, not a "
            "bug: the Research Agent refuses to fabricate ATLAS output. Set "
            "ATLAS_REPO_PATH in .env to the real ATLAS repo root to enable it."
        )
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise AtlasNotConfiguredError(f"ATLAS_REPO_PATH does not exist: {path}")
    return path


def get_atlas_venv_python() -> Path:
    raw = os.environ.get("ATLAS_VENV_PATH")
    if not raw:
        raise AtlasNotConfiguredError(
            "ATLAS_VENV_PATH is not set. ATLAS has its own dependency tree "
            "(pandas, scipy, ...) that must be installed in a dedicated venv "
            "before the Research Agent can call its real entry points. Set "
            "ATLAS_VENV_PATH in .env once that venv exists."
        )
    # Windows venvs put the interpreter in Scripts/, POSIX in bin/. Try
    # both layouts so a venv created on either platform resolves.
    root = Path(raw).expanduser()
    candidates = [root / "Scripts" / "python.exe", root / "bin" / "python"]
    if os.name != "nt":
        candidates.reverse()
    py = next((c for c in candidates if c.is_file()), None)
    if py is None:
        raise AtlasNotConfiguredError(f"No python interpreter found under {root}")
    return py


def run_atlas_research(
    symbols: list[str],
    population_size: int = 20,
    generations: int = 4,
    windows: int = 3,
    portfolio_method: str = "greedy",
) -> dict[str, Any]:
    """Call ATLAS's REAL Atlas().research() entry point via subprocess.

    Raises AtlasNotConfiguredError if ATLAS_REPO_PATH / ATLAS_VENV_PATH are
    missing. Raises RuntimeError (with real stderr) if the ATLAS subprocess
    itself fails — never falls back to fake output.
    """
    repo = get_atlas_repo_path()
    venv_python = get_atlas_venv_python()

    request = {
        "symbols": symbols,
        "population_size": population_size,
        "generations": generations,
        "windows": windows,
        "portfolio_method": portfolio_method,
    }

    proc = subprocess.run(
        [str(venv_python), str(_RUNNER_SCRIPT)],
        input=json.dumps(request),
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=900,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"ATLAS research subprocess failed (exit {proc.returncode}).\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"ATLAS subprocess returned non-JSON output:\n{proc.stdout}"
        ) from exc


# OpenAI-style function-calling tool spec (framework-agnostic — consumed by
# the NVIDIA-NIM-backed orchestrator's hand-rolled tool-calling loop).
RESEARCH_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_atlas_research",
        "description": (
            "Run ATLAS's real discovery + walk-forward validation pipeline on "
            "one or more ticker symbols and return the actual champions/"
            "results JSON. This calls ATLAS's real Atlas().research() entry "
            "point — it does not reimplement or simulate ATLAS logic."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ticker symbols to research, e.g. ['SPY'].",
                },
                "population_size": {"type": "integer", "default": 20},
                "generations": {"type": "integer", "default": 4},
                "windows": {"type": "integer", "default": 3},
            },
            "required": ["symbols"],
        },
    },
}


def call_research_tool(arguments: dict[str, Any]) -> str:
    """Synchronous tool handler: arguments in, plain-text result out.

    Never returns fabricated research data (Rule 2.2) — reports
    NOT CONFIGURED / ATLAS RUN FAILED honestly instead.
    """
    symbols = arguments["symbols"]
    population_size = arguments.get("population_size", 20)
    generations = arguments.get("generations", 4)
    windows = arguments.get("windows", 3)

    try:
        result = run_atlas_research(
            symbols=symbols,
            population_size=population_size,
            generations=generations,
            windows=windows,
        )
    except AtlasNotConfiguredError as exc:
        return f"NOT CONFIGURED: {exc}"
    except RuntimeError as exc:
        return f"ATLAS RUN FAILED: {exc}"

    return json.dumps(result, indent=2)
