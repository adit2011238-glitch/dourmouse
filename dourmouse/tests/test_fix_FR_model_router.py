"""FR fix P2-30: '429' is a status, not a substring."""

from __future__ import annotations

from dourmouse.model_router import is_rate_limit_error


class _E(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status


def test_digits_inside_other_numbers_are_not_a_rate_limit():
    assert not is_rate_limit_error(Exception("server error, request id 4291 failed"))
    assert not is_rate_limit_error(Exception("connect to port 8429 timed out"))
    assert not is_rate_limit_error(Exception("wrote 14298 bytes then reset"))


def test_a_5xx_whose_message_carries_429_is_not_a_rate_limit():
    assert not is_rate_limit_error(_E("upstream error ref 429", status=500))


def test_real_rate_limits_still_match():
    assert is_rate_limit_error(_E("anything", status=429))
    assert is_rate_limit_error(Exception("Error code: 429 - too fast"))
    assert is_rate_limit_error(Exception("HTTP 429"))
    assert is_rate_limit_error(Exception("You hit the Rate Limit"))
    assert is_rate_limit_error(Exception("Too Many Requests"))


# ---- P2-31 ----------------------------------------------------------------

from dourmouse.execution_policy import RunPolicy  # noqa: E402


def test_read_only_calls_are_counted_per_fanout_branch():
    policy = RunPolicy(max_identical=3)
    for branch in ("b1", "b2", "b3", "b4", "b5"):
        for _ in range(3):
            assert policy.decide("news_headlines", {}, consequential=False, scope=branch) is None
        assert policy.decide("news_headlines", {}, consequential=False, scope=branch) is not None  # 4th in ONE branch


def test_gated_calls_are_still_capped_across_the_whole_request():
    policy = RunPolicy(max_identical=3)
    for branch in ("b1", "b2", "b3"):
        assert policy.decide("gmail_send", {"to": "a"}, consequential=True, scope=branch) is None
    assert policy.decide("gmail_send", {"to": "a"}, consequential=True, scope="b4") is not None


def test_unscoped_behaviour_is_unchanged():
    policy = RunPolicy(max_identical=3)
    for _ in range(3):
        assert policy.decide("t", {}, consequential=False) is None
    assert policy.decide("t", {}, consequential=False) is not None
