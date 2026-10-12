"""FIX-W2 W2R-11: the vault is opened read-only through a percent-encoded file URI, so a folder name
holding '#', '?' or '%' is the file the owner pointed at."""
from __future__ import annotations

import sqlite3

import pytest

from dourmouse import shared_rag as sr


@pytest.mark.parametrize("folder", ["Notes #2", "what?", "100%", "a b", "x#y?z%20"])
def test_probe_opens_a_vault_under_an_awkward_folder_name(tmp_path, folder):
    d = tmp_path / folder
    d.mkdir()
    db = d / "hybrid_vault.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)")
    con.execute("INSERT INTO chunks (text, metadata) VALUES ('hello', '{}')")
    con.commit()
    con.close()
    schema = sr.probe_vault_schema(db)
    assert schema.table == "chunks" and schema.row_count == 1


def test_the_read_only_uri_helper_encodes_and_stays_read_only(tmp_path):
    d = tmp_path / "Notes #2"
    d.mkdir()
    db = d / "v.db"
    sqlite3.connect(db).close()
    uri = sr._readonly_uri(db)
    assert uri.endswith("?mode=ro") and "%23" in uri and "#" not in uri
    con = sqlite3.connect(uri, uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("CREATE TABLE t (x)")
    con.close()


def test_no_f_string_uri_is_left_in_the_module():
    import inspect

    assert 'f"file:{db_path}' not in inspect.getsource(sr)
