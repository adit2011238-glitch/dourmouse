"""Embedder interface and the offline stub (plan 2.1; the Gemini embedder is plan 2.3).

The store never imports a network client. It is handed an object with ``embed_documents`` and ``embed_query`` and a
``remote`` flag. A remote embedder sends text off this Mac, so the store asks the privacy gate first (see
``library.gate``). The stub below is deterministic and local, for tests and for running with no key."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

DIM = 768
MODEL_STUB = "stub-hash-768"
_WORD = re.compile(r"[\w'-]+", re.UNICODE)


class Embedder(Protocol):
    model: str
    dim: int
    remote: bool

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashEmbedder:
    """Bag-of-words hashed into ``dim`` buckets and normalised. Shared words give a positive cosine, so tests can show
    hybrid ranking without a model. Never claims to understand meaning."""

    model = MODEL_STUB
    remote = False

    def __init__(self, dim: int = DIM) -> None:
        self.dim = int(dim)

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for w in _WORD.findall((text or "").lower()):
            h = int.from_bytes(hashlib.blake2b(w.encode("utf-8"), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)
