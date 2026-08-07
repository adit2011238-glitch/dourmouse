"""Runs INSIDE the ATLAS venv (not dourmouse's venv).

Reads a JSON request from stdin, calls ATLAS's real Atlas().research() entry
point using ATLAS's own Downloader/default_router for live market data, and
prints the real result as JSON to stdout. Any failure is a real, visible
traceback on stderr with a non-zero exit code — never a fabricated result.

This script deliberately has no fallback path: if ATLAS's real pipeline
fails, this fails loudly (Rule 2.7).
"""

from __future__ import annotations

import json
import sys


def _json_default(obj):
    # Best-effort conversion for pandas/numpy objects ATLAS may return.
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "item"):  # numpy scalar
        return obj.item()
    if hasattr(obj, "tolist"):  # numpy array / pandas Series/Index
        return obj.tolist()
    if hasattr(obj, "to_dict"):  # pandas DataFrame/Series
        return obj.to_dict()
    return str(obj)


def main() -> int:
    request = json.loads(sys.stdin.read())

    from atlas.core import Atlas
    from atlas.data import Downloader, default_router

    downloader = Downloader(default_router())

    market_data = {}
    for symbol in request["symbols"]:
        frame = downloader.load(symbol)
        market_data[symbol] = frame.tail(600)

    benchmark = None
    if request.get("benchmark_symbol"):
        benchmark = downloader.load(request["benchmark_symbol"]).tail(600)

    result = Atlas().research(
        market_data,
        population_size=request.get("population_size", 20),
        generations=request.get("generations", 4),
        windows=request.get("windows", 3),
        benchmark=benchmark,
        portfolio_method=request.get("portfolio_method", "greedy"),
    )

    json.dump(result, sys.stdout, default=_json_default)
    return 0


if __name__ == "__main__":
    sys.exit(main())
