import json
import os
import base64
import secrets
import subprocess
import asyncio
import logging

XRAY_CONFIG_PATH = os.environ.get("XRAY_CONFIG_PATH") or (
    "/usr/local/bin/config.json" if os.path.isdir("/usr/local/bin") else
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xray_config.json")
)
XRAY_BIN = os.environ.get("XRAY_BIN") or "/usr/local/bin/xray"

# Shadowsocks listens on its own TCP port (no WS/TLS transport available).
SS_PORT = int(os.environ.get("SS_PORT", "8388"))
SS_METHOD = os.environ.get("SS_METHOD", "2022-blake3-aes-128-gcm")

# ---- Shadowsocks-2022 key handling -----------------------------------------
# 2022-blake3-* is NOT a plain-text password cipher: Xray requires a base64
# *PSK* whose decoded length exactly matches the cipher's key size. Feeding it
# a random text password (e.g. token_hex(12)) makes the proxy fail with
# "missing psk" and — because that aborts the whole server — takes EVERY other
# protocol down with it. These helpers guarantee a byte-correct key.
#
# Per the Xray docs, the Go implementation always accepts a 32-byte key, so we
# mint 32 bytes for every SS2022 cipher (32 is also the only size that works
# for chacha20-poly1305).
SS2022_KEY_BYTES = {
    "2022-blake3-aes-128-gcm": 16,
    "2022-blake3-aes-256-gcm": 32,
    "2022-blake3-chacha20-poly1305": 32,
}
SS2022_METHODS = tuple(SS2022_KEY_BYTES)
# The Go implementation accepts 32-byte keys for all of the above, which keeps
# a single key valid across every SS2022 cipher we support.
SS2022_DEFAULT_BYTES = 32
# Legacy AEAD ciphers accept an arbitrary text password, so one fixed length
# works for all of them.
SS_LEGACY_KEY_CHARS = 24


def is_ss2022(method: str) -> bool:
    return (method or "") in SS2022_METHODS


def make_ss_key(method: str) -> str:
    """Return a cipher-correct Shadowsocks password for `method`.

    - 2022-blake3-* -> base64 of 32 random bytes (valid for every SS2022 cipher
      in the Go implementation, which is what the panel runs)
    - anything else -> a random 24-char text password (AEAD style)
    """
    if is_ss2022(method):
        return base64.b64encode(secrets.token_bytes(SS2022_DEFAULT_BYTES)).decode()
    return secrets.token_urlsafe(32)[:SS_LEGACY_KEY_CHARS]


def ss_key_is_valid(method: str, password) -> bool:
    """True when `password` can actually be used with `method`.

    SS2022 requires base64 of 32 bytes (the Go implementation accepts 32 for
    every 2022 cipher) — anything shorter or non-base64 makes xray abort.
    """
    if not password or not isinstance(password, str):
        return False
    if not is_ss2022(method):
        return True
    # Client passwords in multi-user mode are "ServerPSK:UserPSK".
    if ":" in password:
        server, _, user = password.partition(":")
        return _psk_ok(server) and _psk_ok(user)
    return _psk_ok(password)


def _psk_ok(psk: str) -> bool:
    try:
        raw = base64.b64decode(psk, validate=True)
    except Exception:
        return False
    # 32 is universally accepted; also allow the cipher's native size so
    # hand-written configs from the Xray docs keep validating.
    return len(raw) in (16, 32)


def repair_ss_key(ib: dict, method: str) -> str:
    """Ensure `ib` carries a usable SS key; returns the (possibly new) key.

    Note: this regenerates the key when it is unusable, which invalidates the
    old ss:// link for that user — but a broken key means no working link at
    all, so a fresh one is strictly better than a dead engine.
    """
    current = ib.get("ss_password")
    if ss_key_is_valid(method, current) and ":" not in (current or ""):
        return current
    key = make_ss_key(method)
    ib["ss_password"] = key
    return key


def make_server_psk() -> str:
    """Inbound-level server PSK (required by SS2022 multi-user mode)."""
    return base64.b64encode(secrets.token_bytes(SS2022_DEFAULT_BYTES)).decode()


def ss_client_password(server_psk, user_psk: str) -> str:
    """SS2022 clients authenticate as "ServerPSK:UserPSK" (Xray docs)."""
    if not server_psk:
        return user_psk
    return f"{server_psk}:{user_psk}"


# Persisted copy of the live server PSK so subscription/link builders emit
# exactly the creds xray is actually serving. Written by generate_xray_config.
_live_ss_server_psk = None


def current_ss_server_psk():
    """The server PSK in the currently generated config (or None)."""
    return _live_ss_server_psk


def _remember_ss_server_psk(psk):
    global _live_ss_server_psk
    _live_ss_server_psk = psk


# Access log: source of per-user IPs + last-connection times.
# Xray access lines look like: 2026/01/01 10:00:00 1.2.3.4:5678 accepted tcp:... [email]
XRAY_ACCESS_LOG = os.environ.get("XRAY_ACCESS_LOG") or (
    "/var/log/xray/access.log" if os.path.isdir("/var/log/xray") else
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xray_access.log")
)
ACCESS_LOG_MAX_BYTES = 5 * 1024 * 1024

xray_process = None
ip_cache = {}  # uid -> {"ips": [recent unique], "last": epoch|None}

def generate_xray_config(inbounds_data, log_level="warning", ip_blocklist=None):
    """Build the Xray config.

    `ip_blocklist` maps uid -> [ip, ...]. Blocked source addresses are enforced
    with a real Xray routing rule that rejects the traffic (blackhole), so a
    block applied from the panel takes effect on the next config reload — it is
    not a cosmetic flag.
    """
    clients_vless = []
    clients_vmess = []
    clients_trojan = []
    clients_ss = []
    used_ss_methods = set()

    for ib in inbounds_data:
        if not ib.get("enabled", True):
            continue

        uuid = ib["uuid"]
        uid = ib["uid"]
        protos = ib.get("protocols") or ["vless", "vmess"]

        if "vless" in protos:
            clients_vless.append({"id": uuid, "email": uid})
        if "vmess" in protos:
            clients_vmess.append({"id": uuid, "email": uid})
        if "trojan" in protos:
            clients_trojan.append({
                "password": ib.get("trojan_password") or uuid.replace("-", ""),
                "email": uid,
            })
        if "shadowsocks" in protos:
            # A 2022-blake3 client MUST carry a base64 PSK of the right length;
            # anything else makes Xray refuse to boot at all. Repair in place so
            # one legacy/garbage key can never take the whole engine down.
            method = ib.get("ss_method") or SS_METHOD
            key = repair_ss_key(ib, method)
            clients_ss.append({
                "password": key,
                "email": uid,
                "method": method,
            })
            used_ss_methods.add(method)

    # ---- real IP blocking -> routing rules ----
    blocked_ips = []
    try:
        for _uid, ips in (ip_blocklist or {}).items():
            for ip in (ips or []):
                ip = str(ip).strip()
                if ip and ip not in blocked_ips:
                    blocked_ips.append(ip)
    except Exception:
        blocked_ips = []

    if log_level not in ("debug", "info", "warning", "error", "none"):
        log_level = "warning"
    _rotate_access_log()

    # Xray refuses to start an inbound with no clients, and a single such
    # inbound aborts the ENTIRE server. Only build the ones we can serve.
    inbounds = [
        {
            "listen": "127.0.0.1",
            "port": 10085,
            "protocol": "dokodemo-door",
            "settings": {"address": "127.0.0.1"},
            "tag": "api"
        },
        {
            "listen": "127.0.0.1",
            "port": 10001,
            "protocol": "vless",
            "settings": {"clients": clients_vless, "decryption": "none"},
            "streamSettings": {"network": "ws", "wsSettings": {"path": "/vl-ws"}},
            "tag": "inbound-vless-ws"
        },
        {
            "listen": "127.0.0.1",
            "port": 10002,
            "protocol": "vmess",
            "settings": {"clients": clients_vmess},
            "streamSettings": {"network": "ws", "wsSettings": {"path": "/vm-ws"}},
            "tag": "inbound-vmess-ws"
        },
        {
            "listen": "127.0.0.1",
            "port": 10004,
            "protocol": "vless",
            "settings": {"clients": clients_vless, "decryption": "none"},
            "streamSettings": {"network": "xhttp", "xhttpSettings": {"path": "/vl-xhttp"}},
            "tag": "inbound-vless-xhttp"
        },
        {
            "listen": "127.0.0.1",
            "port": 10005,
            "protocol": "trojan",
            "settings": {"clients": clients_trojan},
            "streamSettings": {"network": "ws", "wsSettings": {"path": "/tr-ws"}},
            "tag": "inbound-trojan-ws"
        },
    ]
    if clients_ss:
        # One SS inbound carries ONE cipher, so group clients by method.
        # (Mixing methods in a single inbound is another fatal-start case.)
        by_method = {}
        for cl in clients_ss:
            by_method.setdefault(cl.get("method") or SS_METHOD, []).append(
                {"password": cl["password"], "email": cl["email"]})
        first = True
        for i, (method, cls) in enumerate(sorted(by_method.items())):
            settings = {
                "method": method,
                "network": "tcp,udp",
                "clients": cls,
            }
            if is_ss2022(method):
                # SS2022 multi-user mode REQUIRES an inbound-level server PSK
                # (xray aborts with "missing key" without it). Per the official
                # example the clients keep their own user PSK here, while the
                # *client* link uses "ServerPSK:UserPSK".
                server_psk = make_server_psk()
                settings["password"] = server_psk
                _remember_ss_server_psk(server_psk)
            inbounds.append({
                "listen": "0.0.0.0",
                "port": SS_PORT if first else SS_PORT + i,
                "protocol": "shadowsocks",
                "settings": settings,
                "tag": "inbound-ss" if first else f"inbound-ss-{i}",
            })
            first = False

    config = {
        "log": {"loglevel": log_level, "access": XRAY_ACCESS_LOG},
        "dns": {
            "servers": [
                "https+local://1.1.1.1/dns-query",
                "https+local://8.8.8.8/dns-query",
                "1.1.1.1",
                "8.8.8.8",
                "localhost"
            ],
            "queryStrategy": "UseIPv4"
        },
        "api": {
            "tag": "api",
            "services": ["StatsService"]
        },
        "stats": {},
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True
                }
            },
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True
            }
        },
        "inbounds": inbounds,
        "outbounds": [
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "blocked"},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": (
                # blocked source IPs are dropped before anything else
                ([{
                    "type": "field",
                    "source": [f"{ip}" if "/" in ip else f"{ip}/32" for ip in blocked_ips],
                    "outboundTag": "blocked",
                }] if blocked_ips else [])
                + [
                    {
                        "inboundTag": ["api"],
                        "outboundTag": "api",
                        "type": "field"
                    }
                ]
            )
        }
    }

    try:
        parent = os.path.dirname(XRAY_CONFIG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except OSError as e:
        logging.warning("Could not write xray config to %s: %s (continuing without xray)", XRAY_CONFIG_PATH, e)
    return config


def restart_xray():
    stop_xray()
    return start_xray()


def start_xray() -> dict:
    """Start xray if not running. Returns {started, already, mock}."""
    global xray_process
    if xray_process and xray_process.poll() is None:
        return {"started": False, "already": True, "mock": False}
    if not os.path.exists(XRAY_BIN):
        logging.warning("Xray binary not found. Running in mock/dev mode.")
        return {"started": False, "already": False, "mock": True}
    try:
        xray_process = subprocess.Popen([XRAY_BIN, "run", "-c", XRAY_CONFIG_PATH])
        print("✅ Xray started")
        return {"started": True, "already": False, "mock": False}
    except Exception as e:
        logging.error("Xray start failed: %s", e)
        return {"started": False, "already": False, "mock": False, "error": str(e)[:200]}


def stop_xray() -> dict:
    """Stop our xray child process. Returns {stopped}."""
    global xray_process
    stopped = False
    if xray_process and xray_process.poll() is None:
        xray_process.terminate()
        try:
            xray_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            xray_process.kill()
        stopped = True
    xray_process = None
    return {"stopped": stopped}


def validate_config() -> dict:
    """Really parse the live config file and sanity-check its structure."""
    errors = []
    try:
        with open(XRAY_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {"ok": False, "errors": ["config file not found: " + XRAY_CONFIG_PATH]}
    except json.JSONDecodeError as e:
        return {"ok": False, "errors": [f"invalid JSON: {e}"]}
    if not isinstance(cfg.get("inbounds"), list) or not cfg["inbounds"]:
        errors.append("no inbounds defined")
    if not isinstance(cfg.get("outbounds"), list) or not cfg["outbounds"]:
        errors.append("no outbounds defined")
    tags = set()
    for ib in cfg.get("inbounds", []):
        if not isinstance(ib, dict):
            errors.append("inbound entry is not an object")
            continue
        if not ib.get("tag"):
            errors.append("inbound without tag")
        elif ib["tag"] in tags:
            errors.append(f"duplicate tag: {ib['tag']}")
        else:
            tags.add(ib["tag"])
        settings = ib.get("settings") or {}
        proto = ib.get("protocol")
        cls = settings.get("clients", [])
        if proto == "shadowsocks":
            # SS clients carry password+email (no `id`). 2022-blake3 additionally
            # needs an inbound-level server PSK, and getting this wrong makes
            # xray refuse to start at all ("missing psk" / "missing key").
            method = settings.get("method") or ""
            users = settings.get("clients") or settings.get("users") or []
            if not users:
                errors.append(f"shadowsocks inbound {ib.get('tag')} has no clients")
            if is_ss2022(method):
                server = settings.get("password")
                if not server:
                    errors.append(
                        f"ss2022 inbound {ib.get('tag')} missing server psk")
                elif not _psk_ok(server):
                    errors.append(
                        f"ss2022 inbound {ib.get('tag')} has an invalid server psk")
            for u in users:
                pw = u.get("password") or ""
                if not pw:
                    errors.append(f"ss client without password in {ib.get('tag')}")
                elif is_ss2022(method) and not _psk_ok(pw.split(":")[-1]):
                    errors.append(
                        f"invalid ss2022 user psk for {u.get('email')}")
        elif proto == "trojan":
            # Trojan authenticates with a password, not a UUID.
            for cl in cls:
                if not cl.get("password"):
                    errors.append(f"trojan client without password in {ib.get('tag')}")
        else:
            for cl in cls:
                if not cl.get("id"):
                    errors.append(f"client without id in {ib.get('tag')}")
    n_clients = sum(len((ib.get("settings") or {}).get("clients", []))
                    for ib in cfg.get("inbounds", []) if isinstance(ib, dict))
    return {"ok": not errors, "errors": errors[:10], "inbounds": len(tags), "clients": n_clients}


def _rotate_access_log():
    try:
        parent = os.path.dirname(XRAY_ACCESS_LOG)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.isfile(XRAY_ACCESS_LOG) and os.path.getsize(XRAY_ACCESS_LOG) > ACCESS_LOG_MAX_BYTES:
            old = XRAY_ACCESS_LOG + ".old"
            try:
                os.remove(old)
            except OSError:
                pass
            os.replace(XRAY_ACCESS_LOG, old)
    except OSError:
        pass


_ACCESS_RE = None


def parse_access_log(max_lines: int = 3000) -> dict:
    """Tail the xray access log → {uid: {ips: [...], last: epoch}}.

    Xray access format: YYYY/MM/DD HH:MM:SS <ip>:<port> accepted ... [email]
    where email is the inbound uid we configured.
    """
    global ip_cache
    import re as _re
    import time as _t
    import datetime as _dt
    pat = _re.compile(r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+\[?([0-9a-fA-F.:]+)\]?(?::\d+)?\s+\S+\s+\S+\s+\[([^\]]+)\]")
    out = {}
    try:
        with open(XRAY_ACCESS_LOG, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 256 * 1024), os.SEEK_SET)
                chunk = f.read().decode("utf-8", errors="ignore")
            except OSError:
                chunk = ""
    except FileNotFoundError:
        ip_cache = {}
        return {}
    lines = chunk.splitlines()[-max_lines:]
    for line in lines:
        m = pat.search(line)
        if not m:
            continue
        ts_s, ip, email = m.groups()
        try:
            ts = _dt.datetime.strptime(ts_s, "%Y/%m/%d %H:%M:%S").timestamp()
        except ValueError:
            ts = _t.time()
        ip = ip.strip("[]")
        if ip.count(".") == 3 and ":" in ip:  # strip IPv4:port tail
            ip = ip.rsplit(":", 1)[0]
        rec = out.setdefault(email, {"ips": [], "last": 0})
        if ip and ip not in rec["ips"]:
            rec["ips"].append(ip)
            rec["ips"] = rec["ips"][-5:]
        if ts > rec["last"]:
            rec["last"] = ts
    ip_cache = out
    return out


def get_ip_info(uid: str) -> dict:
    rec = ip_cache.get(uid)
    if not rec:
        return {"ips": [], "last": None}
    return {"ips": list(rec.get("ips", [])), "last": rec.get("last") or None}

previous_stats = {}

async def get_xray_stats():
    """Query Xray stats API and return per-uid traffic deltas using JSON output."""
    global previous_stats
    if not os.path.exists(XRAY_BIN):
        return {}
        
    try:
        proc = await asyncio.create_subprocess_exec(
            XRAY_BIN, "api", "statsquery", "--server=127.0.0.1:10085",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode("utf-8")
        
        if not out.strip():
            return {}
        
        # Parse JSON output
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            # Fallback to regex if JSON fails (for older Xray versions)
            import re
            matches = re.findall(r'name:\s*"([^"]+)"\s*value:\s*(\d+)', out)
            if not matches:
                return {}
            data = {"stat": [{"name": m[0], "value": int(m[1])} for m in matches]}
        
        stat_list = data.get("stat", [])
        current_stats = {}
        for item in stat_list:
            name = item.get("name")
            value = item.get("value")
            if not name or value is None:
                continue
            parts = name.split(">>>")
            if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic":
                uid = parts[1]          # email = inbound uid
                direction = parts[3]    # uplink or downlink
                val = int(value)
                if uid not in current_stats:
                    current_stats[uid] = {"up": 0, "down": 0}
                if direction == "uplink":
                    current_stats[uid]["up"] += val
                elif direction == "downlink":
                    current_stats[uid]["down"] += val

        # Compute deltas
        deltas = {}
        for uid, stats in current_stats.items():
            prev = previous_stats.get(uid, {"up": 0, "down": 0})
            up_delta = stats["up"] - prev["up"]
            down_delta = stats["down"] - prev["down"]
            if up_delta < 0:
                up_delta = stats["up"]
            if down_delta < 0:
                down_delta = stats["down"]
            if up_delta > 0 or down_delta > 0:
                deltas[uid] = {"up": up_delta, "down": down_delta}
        
        previous_stats = current_stats
        return deltas
        
    except Exception as e:
        logging.error(f"Error querying Xray stats: {e}")
        return {}
