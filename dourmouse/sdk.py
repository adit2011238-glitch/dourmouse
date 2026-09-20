"""A programmable, headless entry point into Dourmouse (Domain H, piece 6
of 7): the same real engine every screen already uses -- ``ChatSession`` +
``DispatchRegistry`` -- wrapped for a script or another program to drive
without any UI, generalizing the one real instance of this shape already in
the codebase (``research_mesh/pipeline.py``'s own ``main()`` CLI, finding
#033), rather than designing a new pattern from scratch.

Python API::

    from dourmouse.sdk import Dourmouse

    with Dourmouse() as d:
        report = d.ask("what's on my calendar today")
        print(report["final_text"])

Zero-config by default: ``registry``/``client``/``config`` all resolve the
same real way every other real caller in this codebase already gets them
(``build_general_registry()``, and ``client=None``/``config=None`` self-
resolving through ``dispatch.py``'s own ``load_llm_config_with_fallback``/
``_build_client`` -- the exact same path ``ChatSession`` and every screen's
own dispatch call already takes when nothing is overridden). Nothing here
is a second resolution mechanism.

``Dourmouse`` is a thin facade, not a new engine: ``ask()`` returns the
SAME real report dict ``ChatSession.ask()`` returns (``final_text``,
``transcript``, ...) -- no reshaping, no fabricated fields. ``close()``
(also called automatically by the context manager) fires the real
session-stop hook (Domain H piece 4, ``dourmouse.hooks``).

CLI, for scripting/piping rather than the interactive REPL
``python -m dourmouse.chat`` already provides::

    python -m dourmouse.sdk "what's on my calendar today"
    python -m dourmouse.sdk "read config.py" --forced-agent dev_coding --json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from dourmouse.chat import ChatSession
from dourmouse.dispatch import DispatchRegistry


class Dourmouse:
    """Headless facade over one real, self-resolving ``ChatSession``.

    Every keyword argument is optional and passed straight through to
    ``ChatSession``'s own constructor (``session_file``, ``client``,
    ``config``, ``confirmation_gate``, ...) -- this class adds no new
    parameters or defaults beyond resolving ``registry`` automatically when
    the caller doesn't supply one.
    """

    def __init__(self, registry: DispatchRegistry | None = None, **session_kwargs: Any) -> None:
        if registry is None:
            from dourmouse.general_roster import build_general_registry

            registry = build_general_registry()
        self.registry = registry
        self.session = ChatSession(registry, **session_kwargs)

    def ask(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """One turn. Returns the real dispatch report unchanged -- see
        ``ChatSession.ask``'s own docstring for every accepted kwarg
        (``forced_agent``, ``event_sink``, ``model``, ...)."""
        return self.session.ask(prompt, **kwargs)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "Dourmouse":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m dourmouse.sdk",
        description=(
            "Headless, scriptable one-shot entry point into Dourmouse "
            "(for the interactive REPL, use `python -m dourmouse.chat` instead)."
        ),
    )
    parser.add_argument("prompt", nargs="+", help="the request to run")
    parser.add_argument("--forced-agent", default=None, help="run this on exactly one named subagent")
    parser.add_argument("--session", default=None, help="session file path (default: a fresh one)")
    parser.add_argument("--json", action="store_true", help="print the real report as JSON, for piping/scripting")
    args = parser.parse_args(argv)

    session_file = Path(args.session) if args.session else None
    with Dourmouse(session_file=session_file) as d:
        report = d.ask(" ".join(args.prompt), forced_agent=args.forced_agent)

    if args.json:
        print(json.dumps(report, default=str))
    else:
        print(report.get("final_text") or "(no reply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
