"""One error type for every refusal or failure in app driving.

Each error carries a short ``code`` so the HTTP router can map it to a real
status and the model tools can say REFUSED (a policy said no) or ERROR (the
attempt failed) honestly. The message is always specific: what was refused,
why, and what would unblock it.
"""

from __future__ import annotations

#: code -> (HTTP status, is it a policy refusal rather than a failure)
CODES: dict[str, tuple[int, bool]] = {
    "invalid": (400, True),
    "denied": (403, True),
    "not_allowed": (403, True),
    "not_running": (404, False),
    "stale": (409, True),
    "tree_changed": (409, True),
    "not_frontmost": (409, True),
    "killed": (423, True),
    "secret": (422, True),
    "not_trusted": (503, False),
    "unavailable": (503, False),
    "failed": (500, False),
}


class AppDriverError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code if code in CODES else "failed"

    @property
    def http_status(self) -> int:
        return CODES[self.code][0]

    @property
    def is_refusal(self) -> bool:
        return CODES[self.code][1]

    def as_tool_text(self) -> str:
        prefix = "REFUSED" if self.is_refusal else "ERROR"
        return f"{prefix}: {self}"
