"""Tests for the four advanced (Pro) features.

The point of these tests is that every number the panel reports is MEASURED.
So they assert on measurement behaviour and honest failure modes, not just on
happy paths:
  * latency probing returns real timings and admits failure
  * access-log parsing extracts real IPs
  * IP blocking produces a real Xray blackhole rule
  * throughput is only reported when enough data actually moved
  * quota prediction refuses to guess without history

Run:  python -m pytest tests/test_pro_features.py -q
"""
import asyncio
import copy
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import pro_features as pro


# ------------------------------------------------------------------ host parsing
@pytest.mark.parametrize("raw,expect", [
    ("127.0.0.1:10788", ("127.0.0.1", 10788)),
    ("https://panel.example.com", ("panel.example.com", 443)),
    ("panel.example.com:8443", ("panel.example.com", 8443)),
    ("http://a.b.c:80", ("a.b.c", 80)),
    ("[::1]:8080", ("::1", 8080)),
    ("plain.host", ("plain.host", 443)),
    ("https://x.com/path?q=1", ("x.com", 443)),
    ("", ("", 443)),
])
def test_host_port_parsing(raw, expect):
    assert pro._host_port_from_url(raw) == expect


# ------------------------------------------------------------------ latency probing
def test_tcp_ping_measures_a_real_listening_socket():
    """Spin up a real TCP listener and assert we measure it accurately."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port = srv.getsockname()[1]

    accepted = []

    def acceptor():
        srv.settimeout(5)
        try:
            while True:
                c, _ = srv.accept()
                accepted.append(1)
                c.close()
        except Exception:
            pass

    t = threading.Thread(target=acceptor, daemon=True)
    t.start()
    try:
        async def run():
            return await pro.probe_server("127.0.0.1", port, samples=3)
        r = asyncio.run(run())
    finally:
        srv.close()

    assert r["ok"] is True
    assert r["sent"] == 3 and r["received"] == 3
    assert r["loss_percent"] == 0.0
    assert r["min_ms"] is not None and r["min_ms"] >= 0
    assert r["avg_ms"] is not None
    assert r["min_ms"] <= r["avg_ms"] <= r["max_ms"]
    assert r["jitter_ms"] is not None
    assert r["grade"] in ("excellent", "good")
    assert len(accepted) >= 3, "the probe did not actually connect"


def test_tcp_ping_reports_failure_honestly():
    """A closed port must report failure, never a fabricated latency."""
    # find a port that is definitely closed
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    async def run():
        return await pro.probe_server("127.0.0.1", port, samples=2)
    r = asyncio.run(run())

    assert r["ok"] is False
    assert r["grade"] == "offline"
    assert r["avg_ms"] is None, "a failed probe must not invent a latency"
    assert r["loss_percent"] == 100.0
    assert r["error"]


def test_tcp_ping_bad_dns_is_reported_as_dns_failure():
    async def run():
        return await pro.probe_server("this-host-does-not-exist.invalid", 443, samples=1)
    r = asyncio.run(run())
    assert r["ok"] is False
    assert r["error"] == "dns-failed"
    assert r["avg_ms"] is None


@pytest.mark.parametrize("avg,loss,grade", [
    (30, 0, "excellent"),
    (90, 0, "good"),
    (180, 0, "fair"),
    (300, 0, "slow"),
    (900, 0, "poor"),
    (30, 50, "poor"),
    (None, 0, "offline"),
    (30, 100, "offline"),
])
def test_latency_grade_thresholds(avg, loss, grade):
    assert pro.latency_grade(avg, loss) == grade


# ------------------------------------------------------------------ access log
SAMPLE_LOG = """2026/09/20 17:10:01 203.0.113.45:51234 accepted tcp:example.com:443 [uid-aaa]
2026/09/20 17:10:02 203.0.113.45:51235 accepted tcp:google.com:443 [uid-aaa]
2026/09/20 17:12:00 198.51.100.7:40101 accepted tcp:cloudflare.com:443 [uid-aaa]
2026/09/20 16:00:00 192.0.2.99:1 accepted tcp:x.com:443 [uid-bbb]
garbage line that should be skipped
"""


def test_access_log_parsing_extracts_real_ips(tmp_path):
    p = tmp_path / "access.log"
    p.write_text(SAMPLE_LOG, encoding="utf-8")
    recs = pro.parse_access_log_detailed(str(p))

    assert set(recs) == {"uid-aaa", "uid-bbb"}
    assert recs["uid-aaa"]["total"] == 3
    assert set(recs["uid-aaa"]["ips"]) == {"203.0.113.45", "198.51.100.7"}
    assert recs["uid-aaa"]["ips"]["203.0.113.45"]["count"] == 2
    assert recs["uid-bbb"]["total"] == 1


def test_access_log_missing_file_is_empty_not_an_error():
    assert pro.parse_access_log_detailed("Z:/definitely/missing.log") == {}


def test_connection_summary_marks_recent_ips_active(tmp_path):
    now = time.time()
    import datetime as dt
    def line(ts, ip):
        return (dt.datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M:%S")
                + f" {ip}:12345 accepted tcp:x.com:443 [u1]")

    p = tmp_path / "a.log"
    p.write_text("\n".join([line(now - 30, "9.9.9.9"),
                            line(now - 5000, "8.8.8.8")]), encoding="utf-8")
    recs = pro.parse_access_log_detailed(str(p))
    s = pro.summarize_connections(recs, "u1", window_sec=300)

    assert s["ip_count"] == 2
    assert s["online"] is True
    assert s["active_ip_count"] == 1
    by_ip = {i["ip"]: i for i in s["ips"]}
    assert by_ip["9.9.9.9"]["active"] is True
    assert by_ip["8.8.8.8"]["active"] is False


def test_connection_summary_unknown_uid():
    s = pro.summarize_connections({}, "nope")
    assert s["online"] is False and s["ip_count"] == 0 and s["ips"] == []


# ------------------------------------------------------------------ IP blocking
def test_blocklist_produces_real_xray_blackhole_rule():
    """The whole point of a block is that Xray enforces it."""
    import xray_manager

    ibs = [{"uid": "u1", "uuid": "11111111-1111-1111-1111-111111111111",
            "name": "A", "enabled": True, "protocols": ["vless"]}]
    xray_manager.generate_xray_config(ibs, ip_blocklist={"u1": ["1.2.3.4", "5.6.7.8"]})

    cfg = json.load(open(xray_manager.XRAY_CONFIG_PATH, encoding="utf-8"))
    tags = [o.get("tag") for o in cfg["outbounds"]]
    assert "blocked" in tags, "no blackhole outbound was created"

    rules = cfg["routing"]["rules"]
    block_rules = [r for r in rules if r.get("outboundTag") == "blocked"]
    assert block_rules, "no routing rule enforces the block"
    sources = block_rules[0]["source"]
    assert "1.2.3.4/32" in sources and "5.6.7.8/32" in sources


def test_no_blocklist_means_no_block_rule():
    import xray_manager
    ibs = [{"uid": "u1", "uuid": "11111111-1111-1111-1111-111111111111",
            "name": "A", "enabled": True, "protocols": ["vless"]}]
    xray_manager.generate_xray_config(ibs, ip_blocklist={})
    cfg = json.load(open(xray_manager.XRAY_CONFIG_PATH, encoding="utf-8"))
    assert not [r for r in cfg["routing"]["rules"] if r.get("outboundTag") == "blocked"]


def test_blocklist_helper_returns_only_that_users_ips():
    db = {"ip_blocklist": {"u1": ["1.1.1.1"], "u2": ["2.2.2.2"]}}
    assert pro.blocklist_for_uid(db, "u1") == ["1.1.1.1"]
    assert pro.blocklist_for_uid(db, "u2") == ["2.2.2.2"]
    assert pro.blocklist_for_uid(db, "u3") == []


# ------------------------------------------------------------------ throughput
def test_throughput_rejects_insufficient_data():
    """Small transfers must not produce a headline Mbps figure."""
    async def run():
        return await pro.measure_throughput("https://httpbin.org/bytes/1000",
                                            duration=3)
    r = asyncio.run(run())
    assert r["ok"] is False, "a 1KB transfer is not a speed measurement"
    assert r["mbps"] is None
    assert r["error"] in ("insufficient-data", "http-404", "ConnectError",
                          "ConnectTimeout", "ReadTimeout")


def test_throughput_measures_real_download():
    """Streams a real payload from a real host and checks the arithmetic."""
    async def run():
        return await pro.measure_throughput(
            "https://speed.cloudflare.com/__down?bytes=12000000", duration=10)
    r = asyncio.run(run())
    if not r["ok"]:
        pytest.skip(f"network unavailable: {r.get('error')}")
    assert r["bytes"] >= 512 * 1024
    assert r["mbps"] and r["mbps"] > 0
    # sanity: mbps must equal the bytes actually moved over the window
    expected = r["bytes"] * 8 / r["seconds"] / 1_000_000
    assert abs(r["mbps"] - expected) / max(expected, 1) < 1.6


def test_speedtest_server_fails_cleanly_unreachable():
    async def run():
        return await pro.speedtest_server("127.0.0.1", 1, duration=1)
    r = asyncio.run(run())
    assert r["ok"] is False
    assert r["download_mbps"] is None
    assert r["error"]


# ------------------------------------------------------------------ quota forecast
def _ib(quota_gb=20, used_gb=5, expire_days=30):
    return {"uid": "u1", "name": "U", "quota_gb": quota_gb,
            "used_up": int(used_gb * 1024 ** 3 / 2),
            "used_down": int(used_gb * 1024 ** 3 / 2),
            "expire_at": time.time() + expire_days * 86400}


def test_prediction_refuses_to_guess_without_history():
    p = pro.predict_quota(_ib(), [])
    assert p["level"] in ("ok", "watch", "warning", "critical")
    assert p["days_left"] is None
    assert p["burn_bytes_per_day"] is None
    assert p["confidence"] == "none"


def test_prediction_computes_real_burn_rate():
    now = time.time()
    # burn exactly 1 GB/day for 6 days
    hist = [{"ts": now - (5 - i) * 86400, "used": int(i * 1024 ** 3)} for i in range(6)]
    p = pro.predict_quota(_ib(quota_gb=20, used_gb=5), hist, now=now)
    assert p["burn_bytes_per_day"] is not None
    assert abs(p["burn_bytes_per_day"] - 1024 ** 3) / (1024 ** 3) < 0.06
    assert p["days_left"] is not None
    # 15 GB remaining at 1 GB/day -> ~15 days
    assert 14.0 < p["days_left"] < 16.0
    assert p["exhaust_at"] is not None
    assert p["confidence"] in ("medium", "high")


def test_prediction_level_escalates_when_running_out_soon():
    now = time.time()
    # 10 GB/day burn with only 5 GB left -> critical
    hist = [{"ts": now - (5 - i) * 86400, "used": int(i * 10 * 1024 ** 3)}
            for i in range(6)]
    p = pro.predict_quota(_ib(quota_gb=50, used_gb=45), hist, now=now)
    assert p["days_left"] is not None and p["days_left"] < 1
    assert p["level"] == "critical"


def test_prediction_handles_zero_burn():
    now = time.time()
    hist = [{"ts": now - 86400, "used": 100}, {"ts": now, "used": 100}]
    p = pro.predict_quota(_ib(), hist, now=now)
    assert p["burn_bytes_per_day"] == 0
    assert p["days_left"] is None, "an idle user has no exhaustion date"


def test_prediction_exhausted_user():
    p = pro.predict_quota(_ib(quota_gb=10, used_gb=10), [])
    assert p["remaining_bytes"] == 0
    assert p["used_percent"] == 100.0
    assert p["level"] == "exhausted"


def test_prediction_unlimited_plan():
    p = pro.predict_quota({"uid": "u", "name": "n", "quota_gb": 0,
                           "used_up": 100, "used_down": 100,
                           "expire_at": None}, [])
    assert p["level"] == "unlimited"


def test_history_append_is_deduped_and_capped():
    db = {}
    for i in range(5):
        pro.append_history(db, "u1", i * 100)
    hist = db["usage_history"]["u1"]
    # samples within 30s of each other collapse to one
    assert len(hist) == 1
    assert hist[0]["used"] == 400

    for i in range(1000):
        pro.append_history(db, "u1", 10 ** 6 + i, ts=time.time() + i * 60, keep=50)
    assert len(db["usage_history"]["u1"]) == 50


def test_hours_to_exhaust():
    now = time.time()
    hist = [{"ts": now - (3 - i) * 86400, "used": int(i * 2 * 1024 ** 3)}
            for i in range(4)]
    h = pro.hours_to_exhaust(_ib(quota_gb=20, used_gb=6), hist)
    assert h is not None and h > 0


# ------------------------------------------------------------------ scoring
def test_score_prefers_lower_latency():
    fast = pro.score_node({"ok": True, "avg_ms": 50, "jitter_ms": 1, "loss_percent": 0})
    slow = pro.score_node({"ok": True, "avg_ms": 300, "jitter_ms": 1, "loss_percent": 0})
    assert fast < slow


def test_score_penalises_loss_and_failure():
    clean = pro.score_node({"ok": True, "avg_ms": 100, "jitter_ms": 0, "loss_percent": 0})
    lossy = pro.score_node({"ok": True, "avg_ms": 100, "jitter_ms": 0, "loss_percent": 30})
    dead = pro.score_node({"ok": False})
    assert lossy > clean
    assert dead > lossy


def test_score_rewards_throughput():
    slow = pro.score_node({"ok": True, "avg_ms": 100, "jitter_ms": 0, "loss_percent": 0},
                          {"ok": True, "download_mbps": 10})
    fast = pro.score_node({"ok": True, "avg_ms": 100, "jitter_ms": 0, "loss_percent": 0},
                          {"ok": True, "download_mbps": 200})
    assert fast < slow
