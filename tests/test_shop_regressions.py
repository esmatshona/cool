"""Regression tests for bugs found while auditing the panel + Telegram bot.

Each test here corresponds to a REAL bug that produced wrong output for users
(fake nodes, wrong online counts, broken Clash/Sing-Box links, protocol
selection being ignored). They are kept as tests so the bugs cannot come back.

Run:  python -m pytest tests/test_shop_regressions.py -q
"""
import asyncio
import base64
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _db():
    import storage
    db = copy.deepcopy(storage.DEFAULT_DB)
    db["settings"]["public_domain"] = "panel.example.com"
    db["settings"]["sni_override"] = ""
    db["settings"]["default_fingerprint"] = "chrome"
    db["settings"]["sub_remark_prefix"] = "ALOO"
    db["inbounds"] = []
    return db


def _ib(name="TestUser", protos=("vless", "vmess", "trojan", "shadowsocks")):
    import secrets
    import uuid as _uuid
    now = time.time()
    return {
        "uid": secrets.token_hex(8), "uuid": str(_uuid.uuid4()), "name": name,
        "enabled": True, "created_at": now, "expire_days": 30, "expire_at": now + 30 * 86400,
        "quota_gb": 50.0, "max_connections": 0, "max_requests": 0, "request_count": 0,
        "used_up": 0, "used_down": 0, "fp": "chrome", "strict_single_ip": False,
        "note": "", "sub_token": secrets.token_hex(12), "sub_enabled": True,
        "plan_id": None, "plan_name": "",
        "protocols": list(protos),
        "trojan_password": secrets.token_hex(16),
        "ss_password": secrets.token_hex(12),
        "ss_method": "2022-blake3-aes-128-gcm",
    }


class _Req:
    """Minimal Request stand-in (only query_params + base_url are used)."""
    def __init__(self, query=None):
        self.query_params = query or {}

    class _URL:
        scheme = "https"
        netloc = "panel.example.com"

    base_url = _URL()
    headers = {"host": "panel.example.com"}


# --------------------------------------------------------------------------- build_links
def test_no_fake_nodes_in_subscription():
    """build_links used to inject two all-zero-UUID configs at 127.0.0.1."""
    import main
    db = _db()
    for protos in (("vless",), ("vmess",), ("trojan",), ("shadowsocks",),
                   ("vless", "vmess", "trojan", "shadowsocks")):
        ib = _ib(protos=protos)
        out = main.build_links(_Req(), db, ib)
        assert out["info_configs"] == [], "fake placeholder configs came back"
        joined = "\n".join(out["all_links"])
        assert "00000000-0000-0000-0000-000000000000" not in joined
        assert "127.0.0.1" not in joined


def test_build_links_honours_protocol_selection():
    """Each selected protocol yields exactly the expected number of links."""
    import main
    db = _db()
    cases = {
        ("vless",): 2,          # WS + XHTTP
        ("vmess",): 1,
        ("trojan",): 1,
        ("shadowsocks",): 1,
        ("vless", "vmess", "trojan", "shadowsocks"): 5,
        ("trojan", "shadowsocks"): 2,
    }
    for protos, expected in cases.items():
        ib = _ib(protos=protos)
        out = main.build_links(_Req(), db, ib)
        assert len(out["all_links"]) == expected, f"{protos} -> {len(out['all_links'])}"
        assert out["protocols"] == list(protos)


def test_build_links_never_leaks_unselected_protocols():
    import main
    db = _db()
    ib = _ib(protos=("trojan",))
    joined = "\n".join(main.build_links(_Req(), db, ib)["all_links"])
    assert joined.startswith("trojan://")
    assert "vless://" not in joined
    assert "vmess://" not in joined
    assert "ss://" not in joined


def test_build_links_uses_public_domain():
    import main
    db = _db()
    ib = _ib(protos=("vless", "trojan", "shadowsocks"))
    joined = "\n".join(main.build_links(_Req(), db, ib)["all_links"])
    assert "panel.example.com" in joined
    assert "127.0.0.1" not in joined


# ------------------------------------------------------------------- clash / sing-box
def test_clash_yaml_contains_every_selected_protocol():
    """Clash used to emit ONE vless proxy no matter what the user picked."""
    import main
    db = _db()
    ib = _ib(protos=("vless", "vmess", "trojan", "shadowsocks"))
    y = main.build_clash_yaml(_Req(), db, ib)
    for kind in ("type: vless", "type: vmess", "type: trojan", "type: ss"):
        assert kind in y, f"{kind} missing from Clash config"
    # every proxy must be listed in the group
    for n in ("VLESS-WS-TLS", "VMESS-WS-TLS", "TROJAN-WS-TLS", "SS-TCP"):
        assert n in y
    assert "proxy-groups:" in y and "MATCH,PROXY" in y


def test_clash_yaml_respects_selection():
    import main
    db = _db()
    ib = _ib(protos=("trojan", "shadowsocks"))
    y = main.build_clash_yaml(_Req(), db, ib)
    assert "type: trojan" in y and "type: ss" in y
    assert "type: vless" not in y and "type: vmess" not in y


def test_clash_yaml_is_valid_yaml():
    import main
    yaml = pytest.importorskip("yaml")
    db = _db()
    ib = _ib()
    parsed = yaml.safe_load(main.build_clash_yaml(_Req(), db, ib))
    assert parsed["mode"] == "rule"
    assert len(parsed["proxies"]) == 4
    assert parsed["proxies"][0]["server"] == "panel.example.com"


def test_singbox_config_contains_every_selected_protocol():
    import main
    db = _db()
    ib = _ib()
    cfg = main.build_singbox_config(_Req(), db, ib)
    kinds = [o["type"] for o in cfg["outbounds"]]
    assert kinds == ["vless", "vmess", "trojan", "shadowsocks", "direct"]
    assert cfg["route"]["final"] == "ALOO-TestUser-VLESS-WS-TLS"
    # valid JSON round-trip
    assert json.loads(json.dumps(cfg))["outbounds"][0]["type"] == "vless"


def test_singbox_respects_selection():
    import main
    db = _db()
    ib = _ib(protos=("shadowsocks",))
    cfg = main.build_singbox_config(_Req(), db, ib)
    kinds = [o["type"] for o in cfg["outbounds"]]
    assert kinds == ["shadowsocks", "direct"]
    assert cfg["outbounds"][0]["server_port"] == main.SS_PORT


def test_singbox_and_clash_share_credentials_with_plain_sub():
    """All three formats must describe the SAME user with the SAME secrets."""
    import main
    db = _db()
    ib = _ib(protos=("trojan", "shadowsocks"))
    y = main.build_clash_yaml(_Req(), db, ib)
    cfg = main.build_singbox_config(_Req(), db, ib)
    plain = base64.b64decode(
        base64.b64encode("\n".join(main.build_links(_Req(), db, ib)["all_links"]).encode())
    ).decode()
    assert ib["trojan_password"] in y
    assert ib["ss_password"] in y
    assert cfg["outbounds"][0]["password"] == ib["trojan_password"]
    assert cfg["outbounds"][1]["password"] == ib["ss_password"]
    assert ib["trojan_password"] in plain
    # Trojan + SS do not use a UUID, so it must NOT appear in any format
    assert ib["uuid"] not in y
    assert ib["uuid"] not in plain


# --------------------------------------------------------------------- telegram bot
def test_bot_ai_mode_captures_messages():
    """The AI chat mode keyed its dict with int(chat_id) but looked it up with
    str(chat_id), so it NEVER captured anything. This locks the fix in."""
    import telegram_bot as tb

    sent = []

    async def fake_send(token, chat, text, **kw):
        sent.append(text)
        return True

    async def fake_edit(token, chat, mid, text, markup=None):
        sent.append(text)
        return True

    class S:
        def __init__(s, db):
            s.db = db

        async def get(s):
            return s.db

        async def mutate(s, fn):
            fn(s.db)
            return s.db

        def get_sync(s):
            return s.db

    import storage
    db = copy.deepcopy(storage.DEFAULT_DB)
    db["settings"]["shop_enabled"] = True
    db["settings"]["public_domain"] = "panel.example.com"
    db["bot_users"] = []
    db["inbounds"] = []

    old = (tb.send_message, tb.edit_message, tb.send_photo, tb.send_document, tb.answer_callback)
    tb.send_message, tb.edit_message = fake_send, fake_edit
    tb.send_photo = tb.send_document = tb.answer_callback = fake_send
    try:
        async def run():
            st = S(db)
            await tb.get_customer(st, {"id": 4242, "first_name": "X"})
            await tb.shop_ai_start(st, "T", "4242")
            assert 4242 in tb._AI_HISTORY, "AI mode did not register the chat"
            sent.clear()
            consumed = await tb.handle_shop_message(
                st, "T", "4242", {"id": 4242, "first_name": "X"}, "موجودی من چقدره؟")
            assert consumed is True, "AI mode did not consume the message"
            assert any("موجودی کیف پول" in s for s in sent), sent
        asyncio.run(run())
    finally:
        (tb.send_message, tb.edit_message, tb.send_photo,
         tb.send_document, tb.answer_callback) = old
        tb._AI_HISTORY.pop(4242, None)


def test_bot_free_test_creates_customer_if_missing():
    """shop_free_test used to crash with TypeError when a user tapped the test
    button before /start (customer record did not exist yet)."""
    import telegram_bot as tb

    class S:
        def __init__(s, db):
            s.db = db

        async def get(s):
            return s.db

        async def mutate(s, fn):
            fn(s.db)
            return s.db

        def get_sync(s):
            return s.db

    import storage
    db = copy.deepcopy(storage.DEFAULT_DB)
    db["settings"]["shop_enabled"] = True
    db["settings"]["shop_test_gb"] = 1
    db["settings"]["shop_test_days"] = 1
    db["bot_users"] = []
    db["inbounds"] = []

    async def fake_send(*a, **k):
        return True

    async def fake_edit(*a, **k):
        return True

    old = (tb.send_message, tb.edit_message, tb.send_photo, tb.send_document, tb.answer_callback)
    tb.send_message, tb.edit_message = fake_send, fake_edit
    tb.send_photo = tb.send_document = tb.answer_callback = fake_send
    try:
        async def run():
            st = S(db)
            assert tb.find_customer(db, 777) is None
            await tb.shop_free_test(st, "T", "777", {"id": 777, "first_name": "NoStart"})
            assert tb.find_customer(db, 777) is not None, "customer was not auto-created"
            assert tb.find_customer(db, 777)["test_claimed"] is True
            assert len(db["inbounds"]) == 1
        asyncio.run(run())
    finally:
        (tb.send_message, tb.edit_message, tb.send_photo,
         tb.send_document, tb.answer_callback) = old


def test_bot_ai_prefers_balance_over_usage_keyword():
    """'موجودی من چقدره؟' contains 'چقدر' which used to win the usage match."""
    import telegram_bot as tb

    sent = []

    async def fake_send(token, chat, text, **kw):
        sent.append(text)
        return True

    class S:
        def __init__(s, db):
            s.db = db

        async def get(s):
            return s.db

        async def mutate(s, fn):
            fn(s.db)
            return s.db

        def get_sync(s):
            return s.db

    import storage
    db = copy.deepcopy(storage.DEFAULT_DB)
    db["bot_users"] = [{"tg_id": 9, "first_name": "Q", "inbound_uids": [],
                        "balance": 12345.0, "referrals": [], "referred_by": None,
                        "referral_earned": 0.0, "test_claimed": False, "last_spin": "",
                        "pending_bonus": 0.0, "created_at": time.time()}]

    old = tb.send_message
    tb.send_message = fake_send
    try:
        async def run():
            await tb.shop_ai_reply(S(db), "T", "9", {"id": 9}, "موجودی من چقدره؟")
            assert any("موجودی کیف پول" in s for s in sent), sent
            assert not any("مصرف فعلی" in s for s in sent), sent
        asyncio.run(run())
    finally:
        tb.send_message = old
