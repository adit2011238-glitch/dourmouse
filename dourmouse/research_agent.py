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
import sys
from pathlib import Path
from typing import Any

# Bundled with this package; run inside ATLAS_VENV_PATH via `python <script>`.
_RUNNER_SCRIPT = Path(__file__).parent / "_atlas_runner.py"

#: The directory this package's ``atlas/`` bundle would sit in inside a
#: personal dist: ``dourmouse-dist/dourmouse/...`` -> ``dourmouse-dist/atlas``.
_BUNDLED_ATLAS_DIR = Path(__file__).resolve().parent.parent / "atlas"


class AtlasNotConfiguredError(NotImplementedError):
    pass


def find_bundled_atlas() -> Path | None:
    """The ATLAS engine shipped beside this package in a personal dist.

    A personal build (``build_dist.sh --personal``) embeds the whole ATLAS
    repo at ``<dist>/atlas`` so the app is a single self-contained download
    with NO external ``ATLAS_REPO_PATH`` dependency. In the dev tree this
    function honestly returns None (there is normally no ``atlas/`` sibling),
    and the env-based resolution below remains the only path.
    """
    return _BUNDLED_ATLAS_DIR if _BUNDLED_ATLAS_DIR.is_dir() else None


def get_atlas_repo_path() -> Path:
    """The real ATLAS repo root: env ``ATLAS_REPO_PATH``, else the bundled
    ``atlas/`` next to this package, else raise honestly (Rule 2.2)."""
    raw = os.environ.get("ATLAS_REPO_PATH")
    if raw:
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise AtlasNotConfiguredError(f"ATLAS_REPO_PATH does not exist: {path}")
        return path
    bundled = find_bundled_atlas()
    if bundled is not None:
        return bundled
    raise AtlasNotConfiguredError(
        "ATLAS_REPO_PATH is not set and no bundled atlas/ was found next to "
        "this package. This is a placeholder boundary, not a bug: the "
        "Research Agent refuses to fabricate ATLAS output. Set "
        "ATLAS_REPO_PATH in .env to the real ATLAS repo root (or build the "
        "personal dist, which embeds the engine) to enable it."
    )


_bundled_venv_checked: dict[str, bool | None] = {"ok": None}


def _bundled_venv_can_run_atlas() -> bool:
    """True once the running interpreter can actually import the bundled atlas.

    Verified via a tiny subprocess (``import atlas`` under ``sys.executable``)
    so pandas/scipy are NEVER pulled into the app process, then cached. In a
    personal dist the build installs ATLAS's own requirements into the SAME
    venv the app runs from, so this is normally True on the first atlas use.
    A hand-assembled bundle without those deps gets the honest error below.
    """
    if _bundled_venv_checked["ok"] is None:
        try:
            proc = subprocess.run(
                [sys.executable, "-c", "import atlas"],
                capture_output=True,
                timeout=30,
            )
            _bundled_venv_checked["ok"] = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _bundled_venv_checked["ok"] = False
    return bool(_bundled_venv_checked["ok"])


def get_atlas_venv_python() -> Path:
    """The python that runs ATLAS: env ``ATLAS_VENV_PATH``, else (personal
    dist) the app's own interpreter, else raise honestly.

    In a personal dist ATLAS's deps live in the SAME venv as the app (built
    that way), so reusing ``sys.executable`` avoids a second venv to build
    and a second interpreter to spawn — the fastest possible path.
    """
    raw = os.environ.get("ATLAS_VENV_PATH")
    if raw:
        py = Path(raw).expanduser() / "bin" / "python"
        if not py.is_file():
            # Explicit config wins over the bundle BY DESIGN: a broken env
            # path is reported, never silently replaced.
            raise AtlasNotConfiguredError(f"No python interpreter found at {py}")
        return py
    if find_bundled_atlas() is not None and _bundled_venv_can_run_atlas():
        return Path(sys.executable)
    raise AtlasNotConfiguredError(
        "ATLAS_VENV_PATH is not set and the bundled atlas/ cannot run under "
        "the app's own interpreter. ATLAS has its own dependency tree "
        "(pandas, scipy, ...): in a personal dist run the build's dependency "
        "step, or set ATLAS_VENV_PATH in .env to a venv where ATLAS's real "
        "requirements are installed."
    )


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

    # ``_atlas_runner.py`` is invoked as a script FILE (not ``-m``), so the
    # repo root is NOT on sys.path just by chdir'ing; PYTHONPATH makes
    # ``import atlas`` resolve from the repo (dev) or the bundled engine
    # (personal dist) no matter how the app was launched.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [str(venv_python), str(_RUNNER_SCRIPT)],
        input=json.dumps(request),
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
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
