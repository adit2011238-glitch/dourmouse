"""FIX-W2 H-FW-3: mobile_link reads and writes .env with python-dotenv's parser, so the forms the main
loader accepts (export KEY=v, quotes, spaces around =, inline comments) mean the same thing here."""
from __future__ import annotations

import stat

from dotenv import dotenv_values

import dourmouse.mobile_link as ml


def test_read_env_understands_export_and_quotes(tmp_path):
    p = tmp_path / ".env"
    p.write_text('export DOURMOUSE_HOST=0.0.0.0\nDOURMOUSE_ACCESS_TOKEN="abc=="\nKEEP = \'x y\'  # note\n')
    assert ml.read_env(p) == {"DOURMOUSE_HOST": "0.0.0.0", "DOURMOUSE_ACCESS_TOKEN": "abc==", "KEEP": "x y"}


def test_an_export_line_is_replaced_in_place_not_duplicated(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# mine\nexport DOURMOUSE_HOST=127.0.0.1\nexport DOURMOUSE_ACCESS_TOKEN=old\nKEEP=x\n")
    out = ml.write_env("0.0.0.0", "new", path=p)
    assert out["changed"] is True
    values = dotenv_values(p)
    assert values["DOURMOUSE_HOST"] == "0.0.0.0" and values["DOURMOUSE_ACCESS_TOKEN"] == "new" and values["KEEP"] == "x"
    text = p.read_text()
    assert text.count("DOURMOUSE_HOST") == 1 and text.count("DOURMOUSE_ACCESS_TOKEN") == 1
    assert "# mine" in text


def test_a_quoted_equal_value_is_seen_as_unchanged(tmp_path):
    p = tmp_path / ".env"
    original = 'DOURMOUSE_HOST="0.0.0.0"\nDOURMOUSE_ACCESS_TOKEN=\'tok123\'\nKEEP=x\n'
    p.write_text(original)
    out = ml.write_env("0.0.0.0", "tok123", path=p)
    assert out["changed"] is False
    assert p.read_text() == original


def test_the_written_file_is_private_and_other_lines_are_untouched(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n\n# note\nB='two words'\n")
    ml.write_env("0.0.0.0", "tok", path=p)
    text = p.read_text()
    assert "A=1\n\n# note\nB='two words'\n" in text
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert dotenv_values(p)["DOURMOUSE_ACCESS_TOKEN"] == "tok"


def test_existing_token_is_reused_when_it_was_written_in_export_form(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("export DOURMOUSE_ACCESS_TOKEN=keepme\n")
    monkeypatch.setattr(ml, "_ENV_PATH", p)
    assert ml._token_for_env(rotate=False) == "keepme"


def test_tv_webhook_server_env_loader_also_understands_export_and_quotes(tmp_path, monkeypatch):
    """Checked, already dotenv-based (FD2 P3-62): pinned here for the same form."""
    from dourmouse import tv_webhook_server as tv

    p = tmp_path / ".env"
    p.write_text('export TV_WEBHOOK_SECRET="s3cret"\nFOREX_DATA_PATH=/x\n')
    monkeypatch.delenv("TV_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("FOREX_DATA_PATH", raising=False)
    tv._load_env(p)
    import os

    assert os.environ["TV_WEBHOOK_SECRET"] == "s3cret" and os.environ["FOREX_DATA_PATH"] == "/x"
    monkeypatch.delenv("TV_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("FOREX_DATA_PATH", raising=False)
