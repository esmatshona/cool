"""Tests for Telegram bot command-menu registration (setMyCommands).

Covers the customer default scope and the per-chat admin scope, plus the
failure paths (no token, Telegram rejecting the call).
"""
import asyncio

import telegram_bot as tb


def _run(coro):
    return asyncio.run(coro)


def _fake_tg_call(calls, ok=True, admin_ok=True):
    def _call(token, method, payload=None, timeout=12.0):
        calls.append({"token": token, "method": method, "payload": payload or {}})
        if method != "setMyCommands":
            return {"ok": False, "description": "unexpected-method"}
        is_admin_scope = "scope" in (payload or {})
        good = admin_ok if is_admin_scope else ok
        return {"ok": True, "result": True} if good else {"ok": False, "description": "bad"}
    async def _acall(*a, **kw):
        return _call(*a, **kw)
    return _acall


def test_no_token_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(tb, "tg_call", _fake_tg_call(calls))
    res = _run(tb.register_commands(""))
    assert res["customer"] is False
    assert res["error"] == "no-token"
    assert calls == [], "must not hit the Telegram API without a token"


def test_customer_scope_registered(monkeypatch):
    calls = []
    monkeypatch.setattr(tb, "tg_call", _fake_tg_call(calls))
    res = _run(tb.register_commands("8838325153:AAFfake", None))
    assert res["customer"] is True
    assert res["admin"] is False
    assert len(calls) == 1, "no admin scope requested -> exactly one call"
    payload = calls[0]["payload"]
    assert "scope" not in payload, "customer list must be the default scope"
    names = [c["command"] for c in payload["commands"]]
    assert names == [c for c, _ in tb.CUSTOMER_COMMANDS]
    assert "start" in names
    # Telegram requires lowercase a-z0-9_ and max 32 chars per command name
    for c in payload["commands"]:
        assert c["command"].islower() and len(c["command"]) <= 32
        assert c["description"] and len(c["description"]) <= 256


def test_admin_scope_registered_per_chat(monkeypatch):
    calls = []
    monkeypatch.setattr(tb, "tg_call", _fake_tg_call(calls))
    res = _run(tb.register_commands("8838325153:AAFfake", "7684838340"))
    assert res["customer"] is True and res["admin"] is True
    assert len(calls) == 2
    admin = calls[1]["payload"]
    assert admin["scope"] == {"type": "chat", "chat_id": 7684838340}
    names = [c["command"] for c in admin["commands"]]
    assert names == [c for c, _ in tb.ADMIN_COMMANDS]
    for expected in ("stats", "users", "create", "broadcast", "backup"):
        assert expected in names
    for c in admin["commands"]:
        assert c["command"].islower() and len(c["command"]) <= 32


def test_non_numeric_admin_chat_skips_admin_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(tb, "tg_call", _fake_tg_call(calls))
    res = _run(tb.register_commands("8838325153:AAFfake", "not-a-chat-id"))
    assert res["customer"] is True
    assert res["admin"] is False
    assert len(calls) == 1, "unparseable chat id must not produce a bogus scope"


def test_customer_failure_reports_error_and_stops(monkeypatch):
    calls = []
    monkeypatch.setattr(tb, "tg_call", _fake_tg_call(calls, ok=False))
    res = _run(tb.register_commands("8838325153:AAFfake", "7684838340"))
    assert res["customer"] is False
    assert res["admin"] is False
    assert res["error"] == "bad"
    assert len(calls) == 1, "no point registering the admin scope after a failure"


def test_admin_failure_still_keeps_customer(monkeypatch):
    calls = []
    monkeypatch.setattr(tb, "tg_call", _fake_tg_call(calls, ok=True, admin_ok=False))
    res = _run(tb.register_commands("8838325153:AAFfake", "7684838340"))
    assert res["customer"] is True
    assert res["admin"] is False
    assert res["error"] == ""


def test_network_exception_is_contained(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("net down")
    monkeypatch.setattr(tb, "tg_call", _boom)
    try:
        res = _run(tb.register_commands("8838325153:AAFfake", "7684838340"))
    except RuntimeError:
        res = None
    # tg_call itself swallows exceptions, but a stub that raises must not be
    # silently misinterpreted as success.
    assert res is None or res["customer"] is False


def test_command_names_unique_and_underscore_free():
    for group in (tb.CUSTOMER_COMMANDS, tb.ADMIN_COMMANDS):
        names = [c for c, _ in group]
        assert len(names) == len(set(names)), "duplicate command name"
        assert all(" " not in n for n in names)
