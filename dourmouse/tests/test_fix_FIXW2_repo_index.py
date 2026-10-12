"""FIX-W2 W2R-9: the repo scan's credential-name filter matches whole file-name words, not substrings,
and a symlinked file inside a scanned root is not read."""
from __future__ import annotations

import pytest

from dourmouse import repo_index as ri

ORDINARY = [
    "tokenizer.py", "token_bucket.ts", "tokens.css", "design-tokens.json", "password_reset.html",
    "secrets_manager.py", "SECRET_SCAN.md", "auth_tokens.md", "apikey_rotation.md", "TokenCounter.java",
    "csrf_token_helper.py", "passwordStrength.js", "secret_sharing.py",
]
CREDENTIALS = [
    "credentials.json", "credential.json", "secrets.yaml", "secret.txt", "service_account.json",
    "serviceAccountKey.json", "client_secret_123.json", "client_secrets.json", "tokens.json", "token.pickle",
    "google_token.json", "github_token", "oauth-token.json", "api_keys.txt", "apikey.txt", ".env.production", ".env.local",
    "password.txt", "passwords.csv", "passwd", "private_key.json", "auth.json", "id_rsa", "id_ed25519.pub",
    "prod.pem", "my.key", "store.p12", ".netrc", ".npmrc", ".pgpass", ".git-credentials",
    "credentials.py", "secrets.py", "secrets.md", "passwords.md", "tokens.md", "credentials.html",
]


@pytest.mark.parametrize("name", ORDINARY)
def test_ordinary_source_and_docs_are_collected(tmp_path, name):
    (tmp_path / name).write_text("x = 1\n")
    assert [p.name for p in ri._collect_files(tmp_path)] == [name]


@pytest.mark.parametrize("name", CREDENTIALS)
def test_credential_material_is_still_skipped(tmp_path, name):
    (tmp_path / name).write_text("x = 1\n")
    assert ri._collect_files(tmp_path) == []


def test_a_symlinked_file_inside_the_root_is_not_collected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "id_rsa_copy.txt"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "notes.md").symlink_to(secret)
    (root / "real.md").write_text("# real\n")
    assert [p.name for p in ri._collect_files(root)] == ["real.md"]


def test_a_symlink_to_a_file_inside_the_root_is_also_skipped_not_double_indexed(tmp_path):
    (tmp_path / "real.md").write_text("# real\n")
    (tmp_path / "alias.md").symlink_to(tmp_path / "real.md")
    assert [p.name for p in ri._collect_files(tmp_path)] == ["real.md"]
