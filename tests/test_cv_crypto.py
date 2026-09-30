"""CV encryption at rest (gosha/cv_crypto.py + gosha/cover_letter.py)."""

from __future__ import annotations

import os
import stat

import pytest
from cryptography.fernet import Fernet

import gosha.cover_letter as storage
from gosha import cv_crypto
from gosha.cv_crypto import CvKeyError

CV = "Ana Popescu — Python, Django, PostgreSQL. Cluj-Napoca."


@pytest.fixture(autouse=True)
def cv_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "CV_DIR", tmp_path / "cvs")
    return tmp_path / "cvs"


@pytest.fixture
def key(monkeypatch) -> str:
    value = Fernet.generate_key().decode()
    monkeypatch.setenv(cv_crypto.KEY_ENV, value)
    return value


@pytest.fixture
def production(monkeypatch):
    """Call inside the test body to look like production.

    pytest (re)sets PYTEST_CURRENT_TEST when the call phase starts, so this
    cannot happen during fixture setup.
    """
    def enter() -> None:
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("GOSHA_ENV", raising=False)
        monkeypatch.delenv(cv_crypto.KEY_ENV, raising=False)

    return enter


def test_saved_cv_is_encrypted_and_owner_only(key, cv_dir):
    path = storage.save_cv(7, CV)

    assert path == cv_dir / "7.enc"
    raw = path.read_bytes()
    assert b"Popescu" not in raw and b"Django" not in raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (cv_dir / "7.txt").exists()
    assert storage.load_cv(7) == CV
    assert storage.get_cv_path(7) == path


def test_plaintext_cv_is_migrated_on_read_keeping_upload_time(
    monkeypatch, cv_dir,
):
    monkeypatch.delenv(cv_crypto.KEY_ENV, raising=False)
    storage.save_cv(8, CV)  # dev: plaintext
    legacy = cv_dir / "8.txt"
    os.utime(legacy, (1_700_000_000, 1_700_000_000))

    monkeypatch.setenv(cv_crypto.KEY_ENV, Fernet.generate_key().decode())
    assert storage.load_cv(8) == CV

    assert not legacy.exists()
    encrypted = cv_dir / "8.enc"
    assert b"Django" not in encrypted.read_bytes()
    assert encrypted.stat().st_mtime == pytest.approx(1_700_000_000)


def test_startup_migration_encrypts_every_legacy_file(monkeypatch, cv_dir):
    monkeypatch.delenv(cv_crypto.KEY_ENV, raising=False)
    for uid in (1, 2, 3):
        storage.save_cv(uid, f"{CV} #{uid}")
    (cv_dir / "notes.txt").write_text("not a CV")  # ignored: not a user id

    assert storage.encrypt_plaintext_cvs() == 0  # no key yet: nothing to do

    monkeypatch.setenv(cv_crypto.KEY_ENV, Fernet.generate_key().decode())
    assert storage.encrypt_plaintext_cvs() == 3
    assert storage.encrypt_plaintext_cvs() == 0  # idempotent

    assert sorted(p.name for p in cv_dir.glob("*.enc")) == ["1.enc", "2.enc", "3.enc"]
    assert storage.load_cv(2) == f"{CV} #2"
    assert storage.stored_cv_user_ids() == [1, 2, 3]


def test_key_rotation_decrypts_with_old_key_and_writes_with_new(monkeypatch):
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()
    monkeypatch.setenv(cv_crypto.KEY_ENV, old)
    storage.save_cv(9, CV)

    monkeypatch.setenv(cv_crypto.KEY_ENV, f"{new},{old}")
    assert storage.load_cv(9) == CV
    storage.save_cv(9, CV + " v2")

    monkeypatch.setenv(cv_crypto.KEY_ENV, new)  # old key retired
    assert storage.load_cv(9) == CV + " v2"


def test_wrong_key_fails_loudly(monkeypatch, key):
    storage.save_cv(10, CV)
    monkeypatch.setenv(cv_crypto.KEY_ENV, Fernet.generate_key().decode())
    with pytest.raises(CvKeyError, match="could not be decrypted"):
        storage.load_cv(10)


def test_encrypted_cv_without_key_fails_loudly(monkeypatch, key):
    storage.save_cv(11, CV)
    monkeypatch.delenv(cv_crypto.KEY_ENV)
    with pytest.raises(CvKeyError, match="not set"):
        storage.load_cv(11)


def test_malformed_key_is_rejected(monkeypatch):
    monkeypatch.setenv(cv_crypto.KEY_ENV, "not-a-fernet-key")
    with pytest.raises(CvKeyError, match="not a valid Fernet key"):
        cv_crypto.require_key_configured()


def test_production_without_key_refuses_to_store_plaintext(
    monkeypatch, production, cv_dir,
):
    production()
    with pytest.raises(CvKeyError, match="GOSHA_ENV=development"):
        cv_crypto.require_key_configured()
    with pytest.raises(CvKeyError):
        storage.save_cv(12, CV)
    assert not cv_dir.exists() or not any(cv_dir.iterdir())


def test_development_may_store_plaintext(monkeypatch, production, cv_dir):
    production()
    monkeypatch.setenv("GOSHA_ENV", "development")
    cv_crypto.require_key_configured()
    storage.save_cv(13, CV)
    assert (cv_dir / "13.txt").read_text(encoding="utf-8") == CV


def test_api_refuses_to_start_without_key_in_production(monkeypatch, production):
    production()
    monkeypatch.setenv("SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "1")
    from gosha.api.app import create_app

    with pytest.raises(CvKeyError):
        create_app()


def test_delete_removes_both_formats(monkeypatch, cv_dir, key):
    storage.save_cv(14, CV)
    (cv_dir / "14.txt").write_text("stale plaintext")
    assert storage.delete_cv(14) is True
    assert list(cv_dir.glob("14.*")) == []
    assert storage.delete_cv(14) is False
