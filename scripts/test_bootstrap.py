from pathlib import Path

import pytest

import bootstrap


def install_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bootstrap, "__file__", str(tmp_path / "scripts/bootstrap.py"))
    (tmp_path / ".env.example").write_text(
        "POSTGRES_PASSWORD=\nAPP_DB_PASSWORD=\nBACKUP_DB_PASSWORD=\n"
        "JWT_SECRET=\nREDIS_PASSWORD=\nGENERATION_BACKEND=template\n",
        encoding="utf-8",
    )
    return tmp_path


def test_first_bootstrap_creates_distinct_secrets_and_preserves_them_on_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = install_root(tmp_path, monkeypatch)
    bootstrap.main()
    original = (root / ".env").read_bytes()
    values = dict(line.split("=", 1) for line in original.decode().splitlines())
    secrets = [
        value for name, value in values.items() if name.endswith(("PASSWORD", "SECRET"))
    ]
    assert len(secrets) == 5 and len(set(secrets)) == 5
    assert all(len(value) >= 32 for value in secrets)
    bootstrap.main()
    assert (root / ".env").read_bytes() == original
    output = capsys.readouterr().out
    assert all(value not in output for value in secrets)


def test_upgrade_adds_only_missing_backup_secret_without_rotating_existing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = install_root(tmp_path, monkeypatch)
    original = (
        "# Keep local configuration\nPOSTGRES_PASSWORD=existing-postgres-secret\n"
        "APP_DB_PASSWORD=existing-application-secret\nJWT_SECRET=existing-session-secret\n"
        "REDIS_PASSWORD=existing-redis-secret\nPOSTGRES_DB=existing_database\n"
    )
    (root / ".env").write_text(original, encoding="utf-8")
    bootstrap.main()
    updated = (root / ".env").read_text(encoding="utf-8")
    assert updated.startswith(original)
    added = updated[len(original) :].strip().splitlines()
    assert len(added) == 1 and added[0].startswith("BACKUP_DB_PASSWORD=")
    secret = added[0].split("=", 1)[1]
    assert len(secret) >= 32
    bootstrap.main()
    assert (root / ".env").read_text(encoding="utf-8") == updated
    assert secret not in capsys.readouterr().out
