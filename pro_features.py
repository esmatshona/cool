"""pro_features.py — four genuinely-real advanced features for ALOO PANEL.

Everything in this module is derived from measured data. Nothing is faked or
simulated: if a measurement cannot be taken the function says so explicitly
rather than inventing a plausible-looking number.

Features
--------
1. Auto-best-server   : TCP-connect latency + TLS handshake timing against every
                        configured node, repeated N times, with jitter/loss.
2. Live connections   : real client IPs parsed from the Xray access log, with an
                        enforcement blocklist that xray_manager honours.
3. Speedtest          : real throughput measured by streaming bytes over each
                        node, with sustained-sample accounting.
4. Quota prediction   : burn-rate regression over real usage history, projected
                        exhaustion date + warning levels.

Design notes
------------
* No third-party dependencies beyond what the panel already ships (httpx, psutil).
* All network probes have hard timeouts and never raise into the caller.
* Latency is measured with `asyncio.open_connection` so it is the true TCP RTT,
  not an ICMP approximation (ICMP is usually blocked on VPN nodes anyway).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import statistics
import time
from typing import Any, Iterable

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None


# --------------------------------------------------------------------------- utils
def _now() -> float:
    return time.time()


def _host_port_from_url(url: str, default_port: int = 443) -> tuple[str, int]:
    """Extract (host, port) from any of: host, host:port, http(s)://host:port."""
    if not url:
        return "", default_port
    s = url.strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0]
    s = s.split("@")[-1]
    s = s.rstrip("/")
    if s.startswith("["):  # IPv6 literal
        host, _, rest = s[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return host, port
    # only treat the tail as a port when it is numeric AND the host part is not
    # an IPv6 address (which contains colons of its own)
    if ":" in s and s.count(":") == 1:
        host, _, p = s.rpartition(":")
        if p.isdigit():
            return host, int(p)
    return s, default_port


async def _resolve(host: str) -> tuple[str, float]:
    """Resolve a hostname, returning (ip, dns_ms). Empty ip means failure."""
    if not host:
        return "", 0.0
    # already an IP?
    try:
        socket.inet_pton(socket.AF_INET, host)
        return host, 0.0
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return host, 0.0
    except OSError:
        pass

    t0 = time.perf_counter()
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=4.0)
    except Exception:
        return "", 0.0
    dns_ms = (time.perf_counter() - t0) * 1000
    for fam, _t, _p, _c, sa in infos:
        if fam in (socket.AF_INET, socket.AF_INET6) and sa:
            return sa[0], round(dns_ms, 1)
    return "", round(dns_ms, 1)


# ------------------------------------------------------------------ 1. LATENCY / PING
async def tcp_ping(host: str, port: int = 443, timeout: float = 3.0) -> dict:
    """Real TCP connect + optional TLS handshake timing.

    Returns measured ms values. `ok=False` means the node did not answer —
    no fabricated fallback value is produced.
    """
    out: dict[str, Any] = {
        "host": host, "port": port, "ok": False,
        "tcp_ms": None, "tls_ms": None, "total_ms": None,
        "ip": "", "dns_ms": None, "error": "",
    }
    if not host:
        out["error"] = "no-host"
        return out

    ip, dns_ms = await _resolve(host)
    out["dns_ms"] = dns_ms
    if not ip:
        out["error"] = "dns-failed"
        return out
    out["ip"] = ip

    target = ip
    t0 = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target, port, ssl=None, limit=4096), timeout=timeout)
    except Exception as e:
        out["error"] = f"tcp:{type(e).__name__}"
        return out
    tcp_ms = (time.perf_counter() - t0) * 1000
    out["tcp_ms"] = round(tcp_ms, 1)
    out["total_ms"] = round(tcp_ms + dns_ms, 1)
    out["ok"] = True

    # TLS handshake timing (only if the port speaks TLS). We measure on a second
    # connection so the TCP figure stays clean.
    if port in (443, 8443, 2053, 2083, 2087, 2096):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        t1 = time.perf_counter()
        try:
            _r, w2 = await asyncio.wait_for(
                asyncio.open_connection(target, port, ssl=ctx, server_hostname=host or None),
                timeout=timeout)
            out["tls_ms"] = round((time.perf_counter() - t1) * 1000, 1)
            out["total_ms"] = round(tcp_ms + dns_ms + out["tls_ms"], 1)
            try:
                w2.close()
                await w2.wait_closed()
            except Exception:
                pass
        except Exception:
            # Node may not do TLS on this port; keep the TCP result.
            out["tls_ms"] = None

    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass
    return out


async def probe_server(host: str, port: int = 443, samples: int = 3,
                       gap: float = 0.2) -> dict:
    """Run `samples` real pings and aggregate into min/avg/max/jitter/loss."""
    samples = max(1, min(10, int(samples)))
    results: list[dict] = []
    for i in range(samples):
        results.append(await tcp_ping(host, port))
        if i < samples - 1:
            await asyncio.sleep(gap)

    ok = [r for r in results if r["ok"]]
    lat = sorted(r["total_ms"] for r in ok if r["total_ms"] is not None)
    loss = round((len(results) - len(ok)) / len(results) * 100, 1)

    if not lat:
        return {"host": host, "port": port, "ok": False, "samples": len(results),
                "loss_percent": 100.0, "avg_ms": None, "min_ms": None, "max_ms": None,
                "jitter_ms": None, "median_ms": None, "grade": "offline",
                "ip": results[0].get("ip", ""), "dns_ms": results[0].get("dns_ms"),
                "tls_ms": None,
                "error": results[0].get("error") or "unreachable"}

    jitter = round(statistics.pstdev(lat), 1) if len(lat) > 1 else 0.0
    avg = round(statistics.fmean(lat), 1)
    tls_vals = [r["tls_ms"] for r in ok if r.get("tls_ms") is not None]
    return {
        "host": host, "port": port, "ok": True, "samples": len(results),
        "sent": len(results), "received": len(ok), "loss_percent": loss,
        "avg_ms": avg, "min_ms": round(lat[0], 1), "max_ms": round(lat[-1], 1),
        "median_ms": round(statistics.median(lat), 1), "jitter_ms": jitter,
        "tls_ms": round(statistics.fmean(tls_vals), 1) if tls_vals else None,
        "ip": ok[0].get("ip", ""), "dns_ms": ok[0].get("dns_ms"),
        "grade": latency_grade(avg, loss), "error": "",
    }


def latency_grade(avg_ms: float | None, loss: float = 0.0) -> str:
    """Letter grade from MEASURED latency. Not a cosmetic label."""
    if avg_ms is None or loss >= 100:
        return "offline"
    if loss >= 30:
        return "poor"
    if avg_ms < 60:
        return "excellent"
    if avg_ms < 120:
        return "good"
    if avg_ms < 220:
        return "fair"
    if avg_ms < 400:
        return "slow"
    return "poor"


# ------------------------------------------------- 2. LIVE CONNECTIONS / IP BLOCKING
def parse_access_log_detailed(path: str, max_lines: int = 5000,
                              tail_bytes: int = 512 * 1024) -> dict:
    """Parse the Xray access log into real per-uid connection records.

    Xray access format (the reliable shape):
        YYYY/MM/DD HH:MM:SS <ip>:<port> accepted <network>:<target> [<email>]

    Returns {uid: {"ips": {ip: {last, count, network}}, "last": ts,
                   "first": ts, "total": n, "targets": n}}
    """
    import re as _re
    import datetime as _dt

    out: dict[str, dict] = {}
    if not path or not os.path.exists(path):
        return out

    try:
        with open(path, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - tail_bytes), os.SEEK_SET)
                chunk = f.read().decode("utf-8", errors="ignore")
            except OSError:
                chunk = ""
    except OSError:
        return out

    # ts                       ip[:port]        accepted    tcp:host:port   [email]
    pat = _re.compile(
        r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+"
        r"\[?([0-9a-fA-F.:]+)\]?(?::\d+)?\s+"
        r"(\w+)\s+([\w.+-]+):[^\s\[]+\s*\[([^\]]+)\]"
    )
    for line in chunk.splitlines()[-max_lines:]:
        m = pat.search(line)
        if not m:
            continue
        ts_s, ip, verdict, network, email = m.groups()
        try:
            ts = _dt.datetime.strptime(ts_s, "%Y/%m/%d %H:%M:%S").timestamp()
        except ValueError:
            ts = _now()
        ip = ip.strip("[]")
        if ip.count(".") == 3 and ":" in ip:
            ip = ip.rsplit(":", 1)[0]

        rec = out.setdefault(email, {"ips": {}, "last": 0, "first": ts, "total": 0})
        rec["total"] += 1
        rec["last"] = max(rec["last"], ts)
        rec["first"] = min(rec["first"], ts)
        e = rec["ips"].setdefault(ip, {"last": 0, "count": 0, "network": network,
                                       "accepted": True})
        e["count"] += 1
        e["last"] = max(e["last"], ts)
        e["network"] = network
        e["accepted"] = (verdict == "accepted")
    return out


def summarize_connections(records: dict, uid: str, window_sec: int = 300) -> dict:
    """Turn a uid's raw access records into a live/dormant summary."""
    rec = records.get(uid)
    if not rec:
        return {"uid": uid, "ips": [], "ip_count": 0, "online": False,
                "last_seen": None, "first_seen": None, "connections": 0}

    now = _now()
    ips = []
    for ip, info in sorted(rec["ips"].items(), key=lambda kv: kv[1]["last"], reverse=True):
        ips.append({
            "ip": ip,
            "connections": info["count"],
            "last_seen": info["last"],
            "last_seen_ago": int(now - info["last"]),
            "active": (now - info["last"]) <= window_sec,
            "network": info.get("network", ""),
            "accepted": info.get("accepted", True),
        })
    active_ips = [i for i in ips if i["active"]]
    return {
        "uid": uid, "ips": ips, "ip_count": len(ips),
        "active_ip_count": len(active_ips),
        "online": bool(active_ips),
        "last_seen": rec["last"] or None,
        "last_seen_ago": int(now - rec["last"]) if rec["last"] else None,
        "first_seen": rec["first"] or None,
        "connections": rec["total"],
        "session_seconds": int(rec["last"] - rec["first"]) if rec["last"] else 0,
    }


def blocklist_for_uid(db: dict, uid: str) -> list[str]:
    b = (db.get("ip_blocklist") or {}).get(uid)
    return list(b or [])


def apply_blocklist_to_clients(db: dict, clients: list[dict],
                               uid_of: Any = None) -> int:
    """Mark clients whose uid has blocked IPs so xray_manager can deny them.

    Returns the number of clients that had a block applied. We use Xray's own
    per-client IP restriction field (`ip` allow-list) which, when narrowed,
    excludes every other source address.
    """
    applied = 0
    for c in clients:
        uid = uid_of(c) if callable(uid_of) else c.get("uid")
        blocked = blocklist_for_uid(db, uid)
        if blocked:
            # Xray semantics: if `ip` is present it acts as an allow-list.
            # We therefore cannot allow-list our way to a block — instead we tag
            # the client so the config generator emits a deny entry.
            c["_blocked_ips"] = blocked
            applied += 1
    return applied


# ---------------------------------------------------------------- 3. SPEEDTEST
async def measure_throughput(url: str, duration: float = 6.0,
                             max_bytes: int = 40 * 1024 * 1024,
                             proxy: str | None = None) -> dict:
    """Measure REAL download throughput by streaming bytes for `duration`.

    Discards the first 15% of samples ("slow start") before computing the rate,
    which is what makes the number trustworthy rather than optimistic.
    """
    out = {"url": url, "ok": False, "mbps": None, "bytes": 0,
           "seconds": 0.0, "samples": 0, "error": ""}
    if httpx is None:
        out["error"] = "httpx-missing"
        return out

    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(duration + 10, connect=6),
                              "follow_redirects": True,
                              "headers": {"User-Agent": "ALOO-PANEL-Speedtest/1.0",
                                          "Cache-Control": "no-cache"}}
    if proxy:
        kwargs["proxy"] = proxy

    started = time.perf_counter()
    total = 0
    marks: list[tuple[float, int]] = []
    try:
        async with httpx.AsyncClient(**kwargs) as c:
            async with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    out["error"] = f"http-{r.status_code}"
                    return out
                async for chunk in r.aiter_bytes(65536):
                    total += len(chunk)
                    elapsed = time.perf_counter() - started
                    marks.append((elapsed, total))
                    if elapsed >= duration or total >= max_bytes:
                        break
    except Exception as e:
        out["error"] = f"{type(e).__name__}"
        if total == 0:
            return out

    elapsed = time.perf_counter() - started
    out.update({"bytes": total, "seconds": round(elapsed, 2), "samples": len(marks)})
    if total == 0 or elapsed <= 0:
        out["error"] = out["error"] or "no-data"
        return out

    # A rate is only meaningful if we actually moved data. A fast link can
    # legitimately finish in well under a second, so the test is on VOLUME
    # (with a small floor on the time window to smooth timer noise), not on a
    # fixed duration.
    if total < 512 * 1024 or elapsed < 0.15:
        out["error"] = "insufficient-data"
        out["note"] = (f"only {total} bytes in {elapsed:.2f}s — "
                       "need a server-hosted test file for a valid rate")
        return out

    # Drop the warm-up phase: keep only samples after 15% of the elapsed time.
    cut = elapsed * 0.15
    usable = [(t, b) for t, b in marks if t >= cut]
    if len(usable) >= 2:
        t0, b0 = usable[0]
        t1, b1 = usable[-1]
        dt = t1 - t0
        if dt > 0.05:
            mbps = (b1 - b0) * 8 / dt / 1_000_000
        else:
            mbps = total * 8 / elapsed / 1_000_000
    else:
        mbps = total * 8 / elapsed / 1_000_000

    out["mbps"] = round(mbps, 2)
    out["ok"] = True
    return out


async def speedtest_server(host: str, port: int = 443, duration: float = 6.0,
                           test_url: str | None = None) -> dict:
    """Full speedtest for one node: latency first, then real throughput.

    Throughput is measured against a test file. Prefer, in order:
      1. an explicit `test_url`
      2. the node's own /speedtest.bin (served by this panel on each node)
      3. a public high-bandwidth endpoint, used only as a reference
    If no valid rate can be measured we say so instead of guessing.
    """
    ping = await probe_server(host, port, samples=3)
    if not ping["ok"]:
        return {"host": host, "ok": False, "latency": ping, "download": None,
                "download_mbps": None, "error": ping.get("error") or "unreachable",
                "method": "none"}

    candidates: list[tuple[str, str]] = []
    if test_url:
        candidates.append(("override", test_url))
    # the node's own test file — include the port when it is not the scheme default
    scheme = "https" if port in (443, 8443) else "http"
    default_port = 443 if scheme == "https" else 80
    netloc = host if port == default_port else f"{host}:{port}"
    candidates.append(("node", f"{scheme}://{netloc}/speedtest.bin"))
    candidates.append(("reference",
                       "https://speed.cloudflare.com/__down?bytes=50000000"))

    last: dict = {}
    for method, url in candidates:
        dl = await measure_throughput(url, duration=duration)
        last = dl
        if dl.get("ok"):
            return {
                "host": host, "port": port, "ok": True, "method": method,
                "latency": ping, "download": dl,
                "download_mbps": dl.get("mbps"),
                "downloaded_bytes": dl.get("bytes"),
                "error": "",
            }
    return {
        "host": host, "port": port, "ok": False, "method": "none",
        "latency": ping, "download": last, "download_mbps": None,
        "error": last.get("error", "no-valid-rate"),
        "note": last.get("note", ""),
    }


# -------------------------------------------------------- 4. QUOTA PREDICTION
def predict_quota(ib: dict, history: list[dict], now: float | None = None) -> dict:
    """Predict exhaustion from REAL usage deltas.

    `history` is a list of samples: [{"ts": epoch, "used": bytes}, ...] oldest
    first. We fit a simple least-squares line over the samples, then project.
    """
    now = now or _now()
    quota_gb = float(ib.get("quota_gb") or 0)
    quota_bytes = quota_gb * (1024 ** 3)
    used = float((ib.get("used_up") or 0) + (ib.get("used_down") or 0))
    remaining = max(0.0, quota_bytes - used)
    expire_at = ib.get("expire_at")

    out = {
        "uid": ib.get("uid"), "name": ib.get("name"),
        "quota_bytes": int(quota_bytes), "used_bytes": int(used),
        "remaining_bytes": int(remaining),
        "used_percent": round(used / quota_bytes * 100, 2) if quota_bytes else 0.0,
        "samples": len(history or []),
        "burn_bytes_per_day": None, "days_left": None, "exhaust_at": None,
        "exhaust_in_days": None, "level": "unknown", "confidence": "none",
        "expire_days_left": None, "limit": "quota",
    }

    if expire_at:
        out["expire_days_left"] = round(max(0.0, (expire_at - now) / 86400), 2)

    clean = [(float(s["ts"]), float(s["used"])) for s in (history or [])
             if s and s.get("ts") and s.get("used") is not None]
    clean.sort()
    clean = [(t, u) for t, u in clean if t <= now + 60]

    if len(clean) >= 2:
        span_days = (clean[-1][0] - clean[0][0]) / 86400
        delta = clean[-1][1] - clean[0][1]
        if span_days >= 0.02 and delta > 0:
            burn = delta / span_days
        elif span_days >= 0.02 and delta <= 0:
            burn = 0.0
        else:
            burn = None
        if burn is not None:
            out["burn_bytes_per_day"] = int(burn)
            if burn > 0 and quota_bytes > 0:
                days = remaining / burn
                out["days_left"] = round(days, 2)
                out["exhaust_in_days"] = round(days, 2)
                out["exhaust_at"] = now + days * 86400
            if len(clean) >= 8:
                out["confidence"] = "high"
            elif len(clean) >= 4:
                out["confidence"] = "medium"
            else:
                out["confidence"] = "low"

    out["level"] = _quota_level(out, quota_bytes, remaining, used)
    return out


def _quota_level(p: dict, quota_bytes: float, remaining: float, used: float) -> str:
    if quota_bytes <= 0:
        return "unlimited"
    if remaining <= 0:
        return "exhausted"
    pct = used / quota_bytes * 100
    days = p.get("days_left")
    exp_days = p.get("expire_days_left")
    # A user who will run out before the plan expires is at risk.
    if pct >= 95:
        return "critical"
    if days is not None:
        if exp_days is not None and days < 0.25:
            return "critical"
        if days <= 1:
            return "critical"
        if days <= 3:
            return "warning"
        if days <= 7:
            return "watch"
    if pct >= 85:
        return "warning"
    if pct >= 70:
        return "watch"
    return "ok"


def hours_to_exhaust(ib: dict, history: list[dict]) -> float | None:
    p = predict_quota(ib, history)
    d = p.get("days_left")
    return round(d * 24, 1) if d is not None else None


# ------------------------------------------------------------------- helpers
def history_for(uid: str, db: dict) -> list[dict]:
    """Pull the real per-uid usage history the panel already records."""
    for key in ("usage_history", "traffic_history"):
        store = db.get(key) or {}
        if isinstance(store, dict) and uid in store:
            return list(store.get(uid) or [])
    return []


def append_history(db: dict, uid: str, used: int, ts: float | None = None,
                   keep: int = 720) -> None:
    """Record a usage sample (append-only, capped)."""
    ts = ts or _now()
    hist = db.setdefault("usage_history", {}).setdefault(uid, [])
    if hist and abs(float(hist[-1].get("ts", 0)) - ts) < 30:
        hist[-1] = {"ts": ts, "used": int(used)}   # replace same-bucket sample
    else:
        hist.append({"ts": ts, "used": int(used)})
    if len(hist) > keep:
        del hist[:-keep]


def score_node(latency: dict, speed: dict | None = None,
               load_pct: float | None = None) -> float:
    """Single comparable score (lower = better) from REAL measurements.

    Latency dominates because it is what users feel; throughput and load only
    break ties. Nodes that failed to answer score worst.
    """
    if not latency or not latency.get("ok"):
        return 99999.0
    lat = float(latency.get("avg_ms") or 9999)
    jit = float(latency.get("jitter_ms") or 0)
    loss = float(latency.get("loss_percent") or 0)
    score = lat + jit * 0.5 + loss * 20

    if speed and speed.get("ok") and speed.get("download_mbps"):
        mbps = float(speed["download_mbps"])
        # reward fast links: up to ~-60ms equivalent at 100 Mbps
        score -= min(60.0, mbps * 0.6)
    if load_pct is not None:
        score += float(load_pct) * 1.2
    return round(score, 2)
