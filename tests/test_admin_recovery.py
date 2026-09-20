"""Tests for the emergency admin recovery path (ADMIN_RESET_* env vars).

This is the documented way to regain access when the operator loses the
password on a fresh host (e.g. a new Railway volume). It must NEVER be able
to fire by accident or be abused to hijack an existing account.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as app


class _StubStore:
    def __init__(self, db):
        self._db = db

    async def get(self):
        return self._db

    async def mutate(self, fn):
        return fn(self._db)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ADMIN_RESET_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_RESET_PASSWORD", raising=False)


def _run(coro):
    return asyncio.run(coro)


def _db(username="admin", ph="old"):
    return {"admin": {"username": username, "password_hash": ph, "salt": "s"},
            "admins": []}


def test_reset_existing_admin(monkeypatch):
    """Env vars matching the existing admin must install the new password."""
    monkeypatch.setenv("ADMIN_RESET_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_RESET_PASSWORD", "MyNewStrongPass123")
    db = _db()
    app.store = _StubStore(db)
    _run(app._apply_admin_reset_from_env())
    assert app.admin_credential_ok(db["admin"], "MyNewStrongPass123")
    assert db["admins"], "recovery must also create the owners[] record"
    assert db["admins"][0]["role"] == "owner"


def test_weak_password_refused(monkeypatch):
    """A too-short password must be ignored, leaving the account untouched."""
    monkeypatch.setenv("ADMIN_RESET_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_RESET_PASSWORD", "short")
    db = _db()
    app.store = _StubStore(db)
    _run(app._apply_admin_reset_from_env())
    assert db["admin"]["password_hash"] == "old"


def test_mismatched_username_refused(monkeypatch):
    """Env must not be able to rename an existing admin (hijack guard)."""
    monkeypatch.setenv("ADMIN_RESET_USERNAME", "someone-else")
    monkeypatch.setenv("ADMIN_RESET_PASSWORD", "AnotherStrongPass123")
    db = _db()
    app.store = _StubStore(db)
    _run(app._apply_admin_reset_from_env())
    assert db["admin"]["password_hash"] == "old"
    assert db["admin"]["username"] == "admin"


def test_no_env_is_noop(monkeypatch):
    """With no env vars the recovery path must do nothing at all."""
    db = _db()
    app.store = _StubStore(db)
    _run(app._apply_admin_reset_from_env())
    assert db["admin"]["password_hash"] == "old"


def test_same_password_is_noop(monkeypatch):
    """Re-running with the correct password already set must be a no-op."""
    monkeypatch.setenv("ADMIN_RESET_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_RESET_PASSWORD", "SamePass12345")
    db = _db()
    hp = app.hash_password("SamePass12345")
    db["admin"]["password_hash"] = hp["hash"]
    db["admin"]["salt"] = hp["salt"]
    app.store = _StubStore(db)
    before = db["admin"]["password_hash"]
    _run(app._apply_admin_reset_from_env())
    assert db["admin"]["password_hash"] == before
    assert db["admins"] == []


def test_creates_admin_when_none_exists(monkeypatch):
    """On a brand new volume (no admin yet) the env must bootstrap one."""
    monkeypatch.setenv("ADMIN_RESET_USERNAME", "esmat6614")
    monkeypatch.setenv("ADMIN_RESET_PASSWORD", "BrandNewPass123")
    db = {"admin": None, "admins": []}
    app.store = _StubStore(db)
    _run(app._apply_admin_reset_from_env())
    assert db["admin"]["username"] == "esmat6614"
    assert app.admin_credential_ok(db["admin"], "BrandNewPass123")
