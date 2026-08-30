"""model_router.py — Aider port part 4/4: multi-account routing."""

from __future__ import annotations

from dourmouse import model_router as mr


class TestIsRateLimitError:
    def test_detects_common_shapes(self):
        for msg in [
            "HTTP 429 Too Many Requests",
            "RateLimitError: rate limit exceeded",
            "quota_exceeded for this key",
            "Error: Too Many Requests",
        ]:
            assert mr.is_rate_limit_error(Exception(msg)), msg

    def test_does_not_flag_unrelated_errors(self):
        assert not mr.is_rate_limit_error(Exception("connection refused"))
        assert not mr.is_rate_limit_error(Exception("404 not found"))


class TestAccountsFromEnv:
    def test_single_account(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "key1")
        accounts = mr.accounts_from_env("nvidia", "NVIDIA_API_KEY")
        assert [a.name for a in accounts] == ["nvidia-1"]
        assert accounts[0].api_key == "key1"

    def test_multiple_numbered_accounts(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "key1")
        monkeypatch.setenv("NVIDIA_API_KEY_2", "key2")
        monkeypatch.setenv("NVIDIA_API_KEY_3", "key3")
        accounts = mr.accounts_from_env("nvidia", "NVIDIA_API_KEY")
        assert [a.name for a in accounts] == ["nvidia-1", "nvidia-2", "nvidia-3"]
        assert [a.api_key for a in accounts] == ["key1", "key2", "key3"]

    def test_stops_at_first_gap(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "key1")
        monkeypatch.setenv("NVIDIA_API_KEY_2", "key2")
        # NVIDIA_API_KEY_3 deliberately unset
        monkeypatch.setenv("NVIDIA_API_KEY_4", "key4")
        accounts = mr.accounts_from_env("nvidia", "NVIDIA_API_KEY")
        assert [a.name for a in accounts] == ["nvidia-1", "nvidia-2"]

    def test_no_accounts_configured(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        assert mr.accounts_from_env("nvidia", "NVIDIA_API_KEY") == []


class TestAccountPool:
    def _pool(self):
        return mr.AccountPool([
            mr.Account("a", "nvidia", "k1"),
            mr.Account("b", "nvidia", "k2"),
            mr.Account("c", "nvidia", "k3"),
        ])

    def test_round_robins_across_calls(self):
        pool = self._pool()
        seen = [pool.select().name for _ in range(6)]
        assert seen == ["a", "b", "c", "a", "b", "c"]

    def test_empty_pool_selects_none(self):
        pool = mr.AccountPool([])
        assert pool.select() is None

    def test_mark_rate_limited_excludes_from_selection(self):
        pool = self._pool()
        pool.mark_rate_limited("a", now=1000.0)
        selected = [pool.select(now=1000.0).name for _ in range(4)]
        assert "a" not in selected
        assert set(selected) == {"b", "c"}

    def test_cooldown_expires(self):
        pool = self._pool()
        pool.mark_rate_limited("a", cooldown_seconds=10.0, now=1000.0)
        assert pool.is_cooling_down("a", now=1005.0)
        assert not pool.is_cooling_down("a", now=1011.0)
        # after expiry, "a" is selectable again
        available_names = {acc.name for acc in pool.available(now=1011.0)}
        assert "a" in available_names

    def test_all_accounts_cooling_down_returns_none(self):
        pool = self._pool()
        for acc in pool.accounts():
            pool.mark_rate_limited(acc.name, now=1000.0)
        assert pool.select(now=1000.0) is None

    def test_single_account_pool_keeps_returning_it_until_it_cools(self):
        pool = mr.AccountPool([mr.Account("only", "ollama_cloud", "k")])
        assert pool.select().name == "only"
        assert pool.select().name == "only"
        pool.mark_rate_limited("only", now=1000.0)
        assert pool.select(now=1000.0) is None

    def test_clear_cooldown_makes_it_selectable_again(self):
        pool = self._pool()
        pool.mark_rate_limited("a", now=1000.0)
        pool.clear_cooldown("a")
        assert any(acc.name == "a" for acc in pool.available(now=1000.0))

    def test_len_reflects_configured_accounts_not_availability(self):
        pool = self._pool()
        pool.mark_rate_limited("a", now=1000.0)
        pool.mark_rate_limited("b", now=1000.0)
        assert len(pool) == 3  # still 3 configured, even though 2 are cooling

    def test_exclude_current_account_when_others_are_available(self):
        pool = self._pool()
        selected = pool.select(exclude="a", now=1000.0)
        assert selected.name in ("b", "c")

    def test_exclude_falls_back_when_it_is_the_only_option(self):
        """A single-account pool must not refuse to serve just because the
        caller excluded 'the one it already tried' — that would make
        every single-account setup permanently unusable after one retry."""
        pool = mr.AccountPool([mr.Account("only", "nvidia", "k")])
        selected = pool.select(exclude="only", now=1000.0)
        assert selected is not None
        assert selected.name == "only"


class TestAccountExtraFields:
    def test_extra_carries_provider_specific_data_untouched(self):
        acc = mr.Account("a", "nvidia", "k", extra={"base_url": "https://x", "model": "m"})
        assert acc.extra["base_url"] == "https://x"
        assert acc.extra["model"] == "m"
