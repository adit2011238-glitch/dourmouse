"""FIX-R3 W3R-8: a config file saved with a UTF-8 byte order mark (Windows editors do this) is merged
as UTF-8: the first key is the real key, so a settings save replaces its line instead of appending a
duplicate, the rewritten file carries no BOM, and the settings reader sees the key by its name."""
from __future__ import annotations

from dourmouse import config

BOM = "﻿"


def test_merge_strips_a_leading_bom_and_replaces_the_first_key():
    assert config.merge_env_text(BOM + "A=1\nB=2\n", {"A": "3"}) == "A=3\nB=2\n"


def test_update_user_config_replaces_the_bom_key_in_place():
    path = config.user_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((BOM + "FIRST=old\n# kept\nOTHER=x\n").encode("utf-8"))
    config.update_user_config({"FIRST": "new"})
    text = path.read_bytes().decode("utf-8")
    assert not text.startswith(BOM)
    assert text == "FIRST=new\n# kept\nOTHER=x\n"


def test_the_settings_reader_sees_the_first_key_of_a_bom_file():
    path = config.user_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((BOM + "FIRST=one\n").encode("utf-8"))
    assert config._read_user_config_file() == {"FIRST": "one"}
