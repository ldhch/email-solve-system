"""M-20 Fernet secrets-at-rest tests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings, apply_secret_overrides
from app.core.exceptions import ConfigurationError
from app.core.security import (
    default_secrets_file,
    decrypt_secrets,
    encrypt_secrets,
    generate_encryption_key,
    read_secrets_file,
    write_secrets_file,
)


def test_generate_key_roundtrip() -> None:
    key = generate_encryption_key()
    values = {
        "email_password": "smtp-secret",
        "deepseek_api_key": "sk-test-123",
        "openai_api_key": "sk-openai",
        "secret_key": "jwt-secret",
        "agent_service_token": "token-abc",
        "": "empty-key-skipped",
    }
    tokens = encrypt_secrets(values, key)
    assert "email_password" in tokens
    assert tokens["email_password"] != values["email_password"]
    assert "" not in tokens
    plain = decrypt_secrets(tokens, key)
    assert plain == {
        "email_password": "smtp-secret",
        "deepseek_api_key": "sk-test-123",
        "openai_api_key": "sk-openai",
        "secret_key": "jwt-secret",
        "agent_service_token": "token-abc",
    }


def test_write_read_secrets_file(tmp_path) -> None:
    key = generate_encryption_key()
    target = tmp_path / "secrets.bin"
    write_secrets_file({"email_password": "pw"}, key, target)
    assert target.exists()
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["email_password"] != "pw"
    assert raw["email_password"].startswith("gAAAA")  # Fernet token prefix
    assert read_secrets_file(key, target) == {"email_password": "pw"}
    assert read_secrets_file(key, tmp_path / "missing.bin") == {}


def test_wrong_key_raises(tmp_path) -> None:
    key = generate_encryption_key()
    target = tmp_path / "secrets.bin"
    write_secrets_file({"email_password": "pw"}, key, target)
    with pytest.raises(ConfigurationError):
        read_secrets_file(generate_encryption_key(), target)


def test_invalid_key_raises() -> None:
    with pytest.raises(ConfigurationError):
        encrypt_secrets({"email_password": "pw"}, "not-a-fernet-key")


def test_default_secrets_file_uses_settings_data_root() -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        data_dir="/app/data",
        attachment_dir="",
    )
    assert default_secrets_file(settings) == Path("/app/data/secrets.bin")


def test_default_secrets_file_fallback_is_repo_data() -> None:
    assert default_secrets_file() == REPO_ROOT / "data" / "secrets.bin"


def test_cli_encrypt_secrets_default_path_next_to_data_root(
    tmp_path, monkeypatch
) -> None:
    """`encrypt-secrets` without --file must land next to the data root
    (same directory as app.db), also inside the container (/app/data)."""

    import app.cli as cli

    key = generate_encryption_key()
    settings = Settings(
        database_url="sqlite:///:memory:",
        encryption_key=key,
        secret_key="test-secret-key",
        email_password="smtp-pw",
        deepseek_api_key="",
        openai_api_key="",
        agent_service_token="",
        attachment_dir=str(tmp_path / "attachments"),
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    rc = cli.cmd_encrypt_secrets(argparse.Namespace(file=None))
    assert rc == 0
    target = tmp_path / "secrets.bin"
    assert target.exists()
    assert read_secrets_file(key, target) == {
        "email_password": "smtp-pw",
        "secret_key": "test-secret-key",
    }


def test_apply_secret_overrides(tmp_path) -> None:
    key = generate_encryption_key()
    secrets_file = tmp_path / "secrets.bin"
    write_secrets_file({"email_password": "encrypted-pw"}, key, secrets_file)

    settings = Settings(
        database_url="sqlite:///:memory:",
        encryption_key=key,
        email_password="plain-pw",
        deepseek_api_key="plain-ds",
    )
    result = apply_secret_overrides(settings, secrets_file)
    assert result.email_password == "encrypted-pw"
    assert result.deepseek_api_key == "plain-ds"  # not in the file


def test_apply_secret_overrides_missing_file(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        encryption_key=generate_encryption_key(),
        email_password="plain-pw",
    )
    result = apply_secret_overrides(settings, tmp_path / "absent.bin")
    assert result.email_password == "plain-pw"


def test_apply_secret_overrides_empty_key_falls_back(tmp_path) -> None:
    key = generate_encryption_key()
    secrets_file = tmp_path / "secrets.bin"
    write_secrets_file({"email_password": "encrypted-pw"}, key, secrets_file)
    settings = Settings(
        database_url="sqlite:///:memory:",
        encryption_key="",
        email_password="plain-pw",
    )
    result = apply_secret_overrides(settings, secrets_file)
    assert result.email_password == "plain-pw"


def test_apply_secret_overrides_wrong_key_falls_back(tmp_path) -> None:
    key = generate_encryption_key()
    secrets_file = tmp_path / "secrets.bin"
    write_secrets_file({"email_password": "encrypted-pw"}, key, secrets_file)
    settings = Settings(
        database_url="sqlite:///:memory:",
        encryption_key=generate_encryption_key(),  # different key
        email_password="plain-pw",
    )
    result = apply_secret_overrides(settings, secrets_file)
    assert result.email_password == "plain-pw"


def test_cli_encrypt_secrets(tmp_path, monkeypatch) -> None:
    import app.cli as cli

    key = generate_encryption_key()
    settings = Settings(
        database_url="sqlite:///:memory:",
        encryption_key=key,
        email_password="smtp-pw",
        deepseek_api_key="sk-123",
        openai_api_key="",
        agent_service_token="",
        secret_key="jwt-secret",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    target = tmp_path / "secrets.bin"
    rc = cli.cmd_encrypt_secrets(argparse.Namespace(file=str(target)))
    assert rc == 0
    assert read_secrets_file(key, target) == {
        "email_password": "smtp-pw",
        "deepseek_api_key": "sk-123",
        "secret_key": "jwt-secret",
    }


def test_cli_encrypt_secrets_requires_key(monkeypatch, capsys) -> None:
    import app.cli as cli

    settings = Settings(
        database_url="sqlite:///:memory:",
        encryption_key="",
        email_password="pw",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    rc = cli.cmd_encrypt_secrets(argparse.Namespace(file="/tmp/never-written.bin"))
    assert rc == 1
    assert "ENCRYPTION_KEY" in capsys.readouterr().err
