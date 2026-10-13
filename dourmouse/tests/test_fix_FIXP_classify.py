"""FIX-P R5-16: an unclassified agent fails closed; every agent in the real registry is classified."""

from __future__ import annotations

from dourmouse import privacy
from dourmouse.general_roster import build_general_registry


def test_unknown_agent_and_mcp_tools_are_t2_files():
    assert privacy.classify("read_secret_thing", {}, "mcp_tools") == ("T2", "files")
    assert privacy.hidden_in_private_mode("anything", "mcp_tools")
    # a registry agent nobody listed stays T1 at run time; the registry walk below fails the build instead
    assert privacy.classify("anything", {}, "some_new_agent") == ("T1", None)


def test_known_cases_unchanged():
    assert privacy.classify("web_search", {}, "research_info") == ("T0", None)
    assert privacy.classify("spotify_play", {}, "music") == ("T1", None)
    assert privacy.classify("get_goal_status", {}, "goals") == ("T1", None)
    assert privacy.classify("security_downloads", {}, "security") == ("T2", "files")
    assert privacy.classify("x", {}, None) == ("T1", None)  # no owner known: the old default


def test_every_agent_in_the_real_registry_is_classified():
    unclassified = []
    for sub in build_general_registry().all_subagents():
        if sub.name in privacy.SENSITIVE_AGENTS or sub.name in privacy._T1_AGENTS:
            continue
        for tool in sub.tools:
            n = tool.name
            explicit = (n in privacy._PATH_TOOLS or n in privacy._T2_TOOLS or n in privacy._T0_TOOLS
                        or n.startswith(privacy._T0_PREFIXES) or n in privacy._T1_TOOLS)
            if not explicit:
                unclassified.append((sub.name, n))
    assert unclassified == [], f"classify these tools or their agent in privacy.py: {unclassified}"


def test_h_fixp_1_native_tool_cli_delegates_return_t2_files():
    """The claude_code and codex_code tools run the CLI with its own file and shell tools; their output is whatever
    those read, so it needs the Files consent and is hidden in Private mode."""
    assert privacy.classify("claude_code", {"task": "t"}, "dev_coding") == ("T2", "files")
    assert privacy.classify("codex_code", {"task": "t"}, "dev_coding") == ("T2", "files")
    assert privacy.hidden_in_private_mode("claude_code", "dev_coding")
