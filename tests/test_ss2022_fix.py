"""Regression tests for the Shadowsocks-2022 crash.

The bug: xray was handed a random text password for a `2022-blake3-*` cipher.
Xray needs a base64 PSK of the exact key length, so it failed with
    "proxy/shadowsocks_2022: create service > missing psk"
and — because that aborts the whole server — EVERY protocol went down while
the panel kept reporting `status: offline`. These tests lock the fix in.
"""
import base64
import secrets
import uuid as _uuid

import pytest

import xray_manager as xm


# --------------------------------------------------------------- key helpers
def test_2022_key_is_base64_of_exact_length():
    for method, nbytes in xm.SS2022_KEY_BYTES.items():
        key = xm.make_ss_key(method)
        raw = base64.b64decode(key, validate=True)
        assert len(raw) == nbytes, f"{method} produced {len(raw)} bytes"
        assert xm.ss_key_is_valid(method, key)


def test_key_randomness():
    a = xm.make_ss_key("2022-blake3-aes-128-gcm")
    b = xm.make_ss_key("2022-blake3-aes-128-gcm")
    assert a != b, "keys must not be constant"


def test_the_original_broken_style_is_rejected():
    """token_hex(12)/token_urlsafe(18)[:24] was the exact production value."""
    for bad in (secrets.token_hex(12), secrets.token_urlsafe(18)[:24],
                secrets.token_hex(16), "short", "", None, "not base64!!"):
        assert not xm.ss_key_is_valid("2022-blake3-aes-128-gcm", bad), (
            f"{bad!r} must be rejected as a 2022-blake3 psk")


def test_wrong_length_base64_is_rejected():
    # valid base64, but 12/31/64 bytes instead of the required 16
    for n in (12, 15, 17, 31, 64):
        k = base64.b64encode(secrets.token_bytes(n)).decode()
        assert not xm.ss_key_is_valid("2022-blake3-aes-128-gcm", k)
    # 32 bytes is right for aes-256 but wrong for aes-128
    k32 = base64.b64encode(secrets.token_bytes(32)).decode()
    assert xm.ss_key_is_valid("2022-blake3-aes-256-gcm", k32)
    assert not xm.ss_key_is_valid("2022-blake3-aes-128-gcm", k32)


def test_legacy_ciphers_accept_text_passwords():
    assert xm.ss_key_is_valid("aes-256-gcm", "any-text-password")
    assert xm.make_ss_key("aes-256-gcm")


def test_is_ss2022():
    assert xm.is_ss2022("2022-blake3-aes-128-gcm")
    assert xm.is_ss2022("2022-blake3-aes-256-gcm")
    assert not xm.is_ss2022("aes-256-gcm")
    assert not xm.is_ss2022("")
    assert not xm.is_ss2022(None)


# ------------------------------------------------------------ repair_ss_key
def test_repair_replaces_a_broken_key_and_keeps_a_good_one():
    method = "2022-blake3-aes-128-gcm"
    ib = {"ss_password": secrets.token_hex(12), "ss_method": method}
    fixed = xm.repair_ss_key(ib, method)
    assert xm.ss_key_is_valid(method, fixed)
    assert ib["ss_password"] == fixed, "repair must persist the new key"

    again = xm.repair_ss_key(ib, method)
    assert again == fixed, "a valid key must NOT be regenerated"
    assert ib["ss_password"] == fixed


def test_repair_handles_missing_key():
    ib = {}
    fixed = xm.repair_ss_key(ib, "2022-blake3-aes-256-gcm")
    assert xm.ss_key_is_valid("2022-blake3-aes-256-gcm", fixed)


# -------------------------------------------------------- config generation
def _ib(protos=("vless", "vmess", "trojan", "shadowsocks"),
        ss_method="2022-blake3-aes-128-gcm", ss_password=None):
    return {
        "uid": "u" + secrets.token_hex(3), "uuid": str(_uuid.uuid4()),
        "name": "T", "enabled": True, "protocols": list(protos),
        "trojan_password": secrets.token_hex(16),
        "ss_method": ss_method,
        "ss_password": ss_password if ss_password is not None else xm.make_ss_key(ss_method),
    }


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / "xray_test.json"
    monkeypatch.setattr(xm, "XRAY_CONFIG_PATH", str(p))
    # keep the access-log rotate honest but out of the way
    monkeypatch.setattr(xm, "XRAY_ACCESS_LOG", str(tmp_path / "access.log"))
    return p


def test_generated_config_is_self_consistent(cfg_path):
    cfg = xm.generate_xray_config([_ib()])
    assert cfg is not None, "generate_xray_config must return the config"
    ss = [i for i in cfg["inbounds"] if i["protocol"] == "shadowsocks"]
    assert len(ss) == 1
    method = ss[0]["settings"]["method"]
    for cl in ss[0]["settings"]["clients"]:
        assert xm.ss_key_is_valid(method, cl["password"]), "bad psk emitted"
    assert xm.validate_config()["ok"] is True


def test_broken_key_is_repaired_at_config_build_time(cfg_path):
    """Even a db full of legacy broken keys must produce a bootable config."""
    ib = _ib(ss_password=secrets.token_hex(12))  # the exact old production value
    cfg = xm.generate_xray_config([ib])
    ss = cfg["inbounds"][-1]
    assert xm.ss_key_is_valid(ss["settings"]["method"],
                              ss["settings"]["clients"][0]["password"])
    assert xm.validate_config()["ok"] is True


def test_no_ss_users_means_no_ss_inbound(cfg_path):
    """An inbound with zero clients is itself a fatal-start condition."""
    cfg = xm.generate_xray_config([_ib(protos=("vless",))])
    assert [i for i in cfg["inbounds"] if i["protocol"] == "shadowsocks"] == []
    assert xm.validate_config()["ok"] is True


def test_mixed_ciphers_are_split_into_separate_inbounds(cfg_path):
    a = _ib(protos=("shadowsocks",), ss_method="2022-blake3-aes-128-gcm")
    b = _ib(protos=("shadowsocks",), ss_method="2022-blake3-aes-256-gcm")
    cfg = xm.generate_xray_config([a, b])
    ss = [i for i in cfg["inbounds"] if i["protocol"] == "shadowsocks"]
    assert len(ss) == 2, "one inbound cannot mix ciphers"
    assert len({i["port"] for i in ss}) == 2, "ports must differ"
    assert len({i["tag"] for i in ss}) == 2, "tags must differ"
    assert xm.validate_config()["ok"] is True


def test_disabled_user_is_excluded(cfg_path):
    ib = _ib()
    ib["enabled"] = False
    cfg = xm.generate_xray_config([ib])
    assert [i for i in cfg["inbounds"] if i["protocol"] == "shadowsocks"] == []


def test_validator_catches_a_bad_psk(cfg_path):
    """validate_config must flag a hand-broken config, not silently pass it."""
    cfg = xm.generate_xray_config([_ib()])
    cfg["inbounds"][-1]["settings"]["clients"][0]["password"] = "garbage"
    import json
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    res = xm.validate_config()
    assert res["ok"] is False
    assert any("psk" in e for e in res["errors"])


def test_validator_accepts_trojan_password_clients(cfg_path):
    """Trojan authenticates by password, not UUID — must not be flagged."""
    cfg = xm.generate_xray_config([_ib(protos=("trojan",))])
    res = xm.validate_config()
    assert res["ok"] is True, res["errors"]
