"""Bootstrap a dedicated venv for ATLAS's own dependency tree.

ATLAS has its own requirements (pandas, scipy, etc.) that are NOT installed
anywhere on this machine yet (confirmed: `import atlas` fails with
`No module named 'pandas'`). This script creates a venv OUTSIDE the ATLAS
repo (default: <this project>/.atlas-venv, per Integration Rule 7 — new code/
artifacts stay out of ATLAS's own module structure) and installs EXACTLY
what ATLAS's own requirements.lock/requirements.txt specifies. It never
guesses or reimplements ATLAS's dependency list from memory.

Usage:
    python -m dourmouse.setup_atlas_venv --atlas-repo-path /path/to/atlas
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VENV_PATH = PROJECT_ROOT / ".atlas-venv"


def _require_python312() -> str:
    exe = shutil.which("python3.12")
    if not exe:
        raise RuntimeError(
            "python3.12 not found on PATH. ATLAS's venv is built with the "
            "same interpreter version used for dourmouse itself (Homebrew "
            "python3.12). Install it first — refusing to silently fall back "
            "to a different Python version."
        )
    return exe


def setup_atlas_venv(atlas_repo_path: Path, venv_path: Path = DEFAULT_VENV_PATH) -> Path:
    """Create venv_path (if missing) and pip-install ATLAS's real requirements.

    Raises FileNotFoundError / RuntimeError / subprocess.CalledProcessError
    loudly on any failure — never falls back to a partial or fabricated setup.
    """
    atlas_repo_path = Path(atlas_repo_path).expanduser().resolve()
    if not atlas_repo_path.is_dir():
        raise FileNotFoundError(f"ATLAS repo path does not exist: {atlas_repo_path}")

    req_file = atlas_repo_path / "requirements.lock"
    if not req_file.is_file():
        req_file = atlas_repo_path / "requirements.txt"
    if not req_file.is_file():
        raise FileNotFoundError(
            f"No requirements.lock or requirements.txt found under {atlas_repo_path}"
        )

    python312 = _require_python312()

    if not venv_path.exists():
        subprocess.run([python312, "-m", "venv", str(venv_path)], check=True)

    venv_python = venv_path / "bin" / "python"
    subprocess.run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(venv_python), "-m", "pip", "install", "-r", str(req_file)], check=True)

    return venv_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atlas-repo-path", required=True, help="Root of the real ATLAS repo")
    parser.add_argument(
        "--venv-path", default=str(DEFAULT_VENV_PATH), help="Where to create ATLAS's own venv"
    )
    args = parser.parse_args()

    venv_path = setup_atlas_venv(Path(args.atlas_repo_path), Path(args.venv_path))
    print(f"ATLAS venv ready at: {venv_path}")
    print(f"Set ATLAS_VENV_PATH={venv_path} in .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
