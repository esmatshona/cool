"""ALOO PANEL — Telegram bot (v3.0).

Bot architecture mirrors the reference Shopvpn bot (github.com/mehdirafatpanah/Shopvpn):

  * Reply keyboard main menu  (buy / test / account / wallet / referral / wheel / support)
  * Inline-keyboard sub-flows (category -> plan -> quantity -> discount -> payment)
  * Account hub               (my configs, links, QR, renew, rename, delete, sub)
  * Wallet                    (top-up request -> admin approve/deny -> real balance)
  * Referral                  (3 modes: percent commission, free config, flat bonus)
  * Lucky wheel               (weighted prizes credited to a real config)
  * Support                   (AI assistant -> escalate to human / ticket)
  * Admin panel               (9 categories, product/plan CRUD, broadcast, stats, backups)

Everything reads and writes the SAME JSON store the web panel uses, so numbers
shown in Telegram are always the real panel numbers — no mock data anywhere.

Only `httpx` is required (already a project dependency); no aiogram/python-telegram-bot.
"""
import asyncio
import base64
import json
import logging
import os
import random
import secrets
import time

import httpx

log = logging.getLogger("telegram_bot")

API = "https://api.telegram.org"

# Last long-poll outcome (surfaced in /api/telegram/status → poll).
poll_state = {"last_ts": 0.0, "ok": None, "error": ""}

# ============================ reply-keyboard labels ============================
BTN_BUY = "🛒 خرید کانفیگ"
BTN_TEST = "🧪 کانفیگ تست رایگان"
BTN_ACCOUNT = "🧾 حساب کاربری من"
BTN_WALLET = "👛 کیف پول"
BTN_REF = "🤝 زیرمجموعه‌گیری من"
BTN_WHEEL = "🎡 گردونه شانس"
BTN_SUPPORT = "📞 ارتباط با پشتیبانی"
BTN_ADMIN = "⚙️ پنل مدیریت"
BTN_MENU = "🏠 منوی اصلی"

TOPUP_AMOUNTS = (50000, 100000, 200000, 500000)
WHEEL_PRIZES = ((1, 45), (2, 30), (5, 15), (10, 8), (30, 2))  # (GB, weight)
TOPUP_MIN = 10000


def poll_state_summary() -> dict:
    return {"last_ts": poll_state.get("last_ts") or 0,
            "last_ok": poll_state.get("ok"),
            "error": (poll_state.get("error") or "")[:200],
            "fresh": (time.time() - (poll_state.get("last_ts") or 0)) < 90}


# ================================ transport =================================
def _api_url(token: str, method: str) -> str:
    return f"{API}/bot{token}/{method}"


async def tg_call(token: str, method: str, payload: dict | None = None, timeout: float = 12.0):
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(_api_url(token, method), json=payload or {})
            try:
                return r.json()
            except Exception:
                return {"ok": False, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def validate_token(token: str):
    """Return (ok, bot_info_or_error)."""
    if not token or len(token) < 20 or ":" not in token:
        return False, "bad-format"
    data = await tg_call(token, "getMe")
    if isinstance(data, dict) and data.get("ok"):
        return True, data.get("result", {})
    return False, (data.get("description") if isinstance(data, dict) else "unreachable")


async def send_message(token: str, chat_id, text: str, parse_mode: str = "HTML",
                       disable_preview: bool = True, markup: dict | None = None) -> bool:
    if not token or chat_id in (None, ""):
        return False
    try:
        payload = {"chat_id": chat_id, "text": (text or "")[:3900],
                   "parse_mode": parse_mode, "disable_web_page_preview": disable_preview}
        if markup is not None:
            payload["reply_markup"] = json.dumps(markup, ensure_ascii=False)
        data = await tg_call(token, "sendMessage", payload)
        return bool(data.get("ok"))
    except Exception:
        return False


async def edit_message(token: str, chat_id, message_id, text: str, markup: dict | None = None) -> bool:
    try:
        payload = {"chat_id": chat_id, "message_id": message_id,
                   "text": (text or "")[:3900], "parse_mode": "HTML"}
        if markup is not None:
            payload["reply_markup"] = json.dumps(markup, ensure_ascii=False)
        data = await tg_call(token, "editMessageText", payload)
        return bool(data.get("ok"))
    except Exception:
        return False


async def answer_callback(token: str, cb_id: str, text: str = "", alert: bool = False) -> bool:
    try:
        data = await tg_call(token, "answerCallbackQuery",
                             {"callback_query_id": cb_id, "text": (text or "")[:190],
                              "show_alert": alert})
        return bool(data.get("ok"))
    except Exception:
        return False


async def send_photo(token: str, chat_id, png: bytes, caption: str = "") -> bool:
    if not token or chat_id in (None, "") or not png:
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(_api_url(token, "sendPhoto"),
                             data={"chat_id": chat_id, "caption": caption[:1000],
                                   "parse_mode": "HTML"},
                             files={"photo": ("qr.png", png, "image/png")})
            return bool(r.json().get("ok"))
    except Exception:
        return False


async def send_document(token: str, chat_id, data: bytes, filename: str, caption: str = "") -> bool:
    if not token or chat_id in (None, "") or not data or not filename:
        return False
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(_api_url(token, "sendDocument"),
                             data={"chat_id": chat_id, "caption": caption[:1000],
                                   "parse_mode": "HTML"},
                             files={"document": (filename, data, "application/json")})
            return bool(r.json().get("ok"))
    except Exception:
        return False


def make_qr_png(text: str) -> bytes | None:
    try:
        import qrcode, io
        img = qrcode.make(text, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


# ================================ formatting =================================
def fmt_bytes(n) -> str:
    try:
        n = float(n or 0)
    except Exception:
        return "0 B"
    if n <= 0:
        return "0 B"
    import math
    units = ["B", "KB", "MB", "GB", "TB"]
    i = min(len(units) - 1, int(math.log(n, 1024)))
    return f"{n / (1024 ** i):.2f} {units[i]}"


def fmt_gb(g) -> str:
    try:
        g = float(g or 0)
    except Exception:
        return "0"
    if g <= 0:
        return "∞"
    return f"{g:g}"


def _fmt_money(v) -> str:
    try:
        return f"{float(v):,.0f} تومان"
    except Exception:
        return str(v)


def esc(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_fa(db) -> bool:
    return ((db.get("settings") or {}).get("lang") or "fa") == "fa"


# ============================== menu builders ================================
def _btn(text, data):
    return {"text": text, "callback_data": data[:64]}


def _url_btn(text, url):
    return {"text": text, "url": url}


def main_menu_kb(db=None, is_admin: bool = False) -> dict:
    rows = [
        [{"text": BTN_BUY}, {"text": BTN_TEST}],
        [{"text": BTN_ACCOUNT}, {"text": BTN_WALLET}],
        [{"text": BTN_REF}, {"text": BTN_WHEEL}],
        [{"text": BTN_SUPPORT}],
    ]
    if is_admin:
        rows.append([{"text": BTN_ADMIN}])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


def account_hub_kb() -> dict:
    return {"inline_keyboard": [
        [_btn("📦 سرویس‌های من", "mo:list"), _btn("🔗 لینک‌های من", "mo:links_all")],
        [_btn("📊 مصرف من", "mo:usage"), _btn("🔄 بروزرسانی", "mo:refresh")],
        [_btn("⬅️ منوی اصلی", "nav:main")],
    ]}


def wallet_kb() -> dict:
    rows = [[_btn(f"➕ شارژ {_fmt_money(a)}", f"topup:{a}")] for a in TOPUP_AMOUNTS]
    rows.append([_btn("📜 تاریخچه شارژ", "wallet:history")])
    rows.append([_btn("⬅️ منوی اصلی", "nav:main")])
    return {"inline_keyboard": rows}


def payment_kb(order_id: str, amount: float, balance: float) -> dict:
    rows = [[_btn("✅ پرداخت از کیف پول", f"pay:wallet:{order_id}")]]
    if balance < amount:
        rows.append([_btn("👛 شارژ کیف پول", "nav:wallet")])
    rows.append([_btn("❌ انصراف", "nav:main")])
    return {"inline_keyboard": rows}


def support_kb(db) -> dict:
    cfg = _shop_cfg(db)
    rows = [
        [_btn("🤖 دستیار هوشمند", "sup:ai")],
        [_btn("📞 گفتگو با پشتیبانی انسانی", "sup:human")],
        [_btn("⬅️ منوی اصلی", "nav:main")],
    ]
    if cfg["support"]:
        rows.insert(2, [_url_btn("💬 چت با پشتیبانی", f"https://t.me/{cfg['support']}")])
    return {"inline_keyboard": rows}


def admin_panel_kb() -> dict:
    return {"inline_keyboard": [
        [_btn("📊 آمار فروش", "adm:stats"), _btn("👥 کاربران ربات", "adm:customers")],
        [_btn("🛒 محصولات و پلن‌ها", "adm:products"), _btn("💰 شارژهای در انتظار", "adm:topups")],
        [_btn("⚡ سنجش سرورها", "adm:bench"), _btn("🔌 اتصالات زنده", "adm:conns")],
        [_btn("⚠️ کاربران در خطر", "adm:risk"), _btn("🔧 وضعیت سرور", "adm:server")],
        [_btn("📢 ارسال همگانی", "adm:broadcast"), _btn("💾 پشتیبان‌گیری", "adm:backup")],
        [_btn("⬅️ منوی اصلی", "nav:main")],
    ]}


def cancel_kb() -> dict:
    return {"inline_keyboard": [[_btn("❌ انصراف", "nav:main")]]}


# ============================== shop config ==================================
def resolve_creds(settings: dict):
    """Bot credentials: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env wins over DB."""
    s = settings or {}
    env_tok = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if env_tok:
        return (env_tok,
                (os.environ.get("TELEGRAM_CHAT_ID") or str(s.get("telegram_chat_id") or "")).strip(),
                "env")
    return ((s.get("telegram_bot_token") or "").strip(),
            str(s.get("telegram_chat_id") or "").strip(), "db")


def _shop_cfg(db) -> dict:
    s = db.get("settings") or {}
    token, admin_chat, _src = resolve_creds(s)
    return {
        "enabled": bool(s.get("shop_enabled", True)),
        "support": (s.get("support_username") or "ITSESMAT").lstrip("@"),
        "test_gb": float(s.get("shop_test_gb") or 1),
        "test_days": int(s.get("shop_test_days") or 1),
        "bonus": float(s.get("referral_bonus") or 0),
        "bot_username": (s.get("bot_username") or "").lstrip("@"),
        "domain": (s.get("public_domain") or "").strip().rstrip("/"),
        "token": token,
        "admin_chat": admin_chat,
    }


def find_customer(db, tg_id) -> dict | None:
    try:
        tg_id = int(tg_id or 0)
    except Exception:
        return None
    for u in db.get("bot_users", []):
        if int(u.get("tg_id") or 0) == tg_id:
            return u
    return None


async def get_customer(store, tg_user: dict) -> dict:
    """Get-or-create the shop customer record. Returns a copy."""
    tg_id = int(tg_user.get("id") or 0)
    uname = (tg_user.get("username") or "")[:64]
    fname = (tg_user.get("first_name") or "")[:64]
    out = {}

    def _a(db):
        for u in db.get("bot_users", []):
            if int(u.get("tg_id") or 0) == tg_id:
                if uname:
                    u["username"] = uname
                if fname:
                    u["first_name"] = fname
                out.update(u)
                return
        rec = {"tg_id": tg_id, "username": uname, "first_name": fname,
               "inbound_uids": [], "balance": 0.0, "referrals": [],
               "referred_by": None, "referral_earned": 0.0,
               "test_claimed": False, "last_spin": "", "pending_bonus": 0.0,
               "created_at": time.time()}
        db.setdefault("bot_users", []).append(rec)
        out.update(rec)
    await store.mutate(_a)
    return out


# ============================ internal helpers ===============================
def _refresh_panel():
    try:
        import main as _main
        _main.refresh_xray(_main.store.get_sync())
    except Exception:
        pass


def _public_base(db) -> str | None:
    dom = ((db.get("settings") or {}).get("public_domain") or "").strip().rstrip("/")
    return f"https://{dom}" if dom else None


def _sub_url(db, ib) -> str | None:
    base = _public_base(db)
    if not base:
        return None
    return f"{base}/s/{ib.get('sub_token') or ib.get('uid')}"


def _inbound_by_uid(db, uid):
    for x in db.get("inbounds", []):
        if x.get("uid") == uid:
            return x
    return None


def _make_inbound_dict(name: str, quota_gb: float, days: int, fp: str, note: str,
                       protocols=None) -> dict:
    now = time.time()
    return {"uid": secrets.token_hex(8), "uuid": __import__("uuid").uuid4().__str__(),
            "name": name[:64], "enabled": True, "created_at": now,
            "expire_days": max(0, days), "expire_at": (now + days * 86400) if days > 0 else None,
            "quota_gb": max(0.0, float(quota_gb)), "max_connections": 0, "max_requests": 0,
            "request_count": 0, "used_up": 0, "used_down": 0, "fp": fp or "chrome",
            "strict_single_ip": False, "note": note[:200],
            "sub_token": secrets.token_hex(12), "sub_enabled": True,
            "plan_id": None, "plan_name": "",
            "protocols": protocols or ["vless", "vmess"],
            "trojan_password": secrets.token_hex(16),
            "ss_password": secrets.token_urlsafe(18)[:24],
            "ss_method": "2022-blake3-aes-128-gcm"}


def _user_snapshot_lines(ib) -> list:
    """Real, live numbers for one config."""
    used = (ib.get("used_up") or 0) + (ib.get("used_down") or 0)
    q = ib.get("quota_gb") or 0
    quota_txt = f"{used / 1024**3:.2f} / {q:g} GB" if q > 0 else f"{used / 1024**3:.2f} GB / ∞"
    exp = ib.get("expire_at")
    if exp:
        days = (exp - time.time()) / 86400
        exp_txt = "منقضی شده ❌" if days <= 0 else f"{days:.1f} روز مانده"
    else:
        exp_txt = "بدون انقضا ♾"
    state = "فعال ✅" if ib.get("enabled", True) else "غیرفعال ⛔"
    return [f"👤 <b>{esc(ib.get('name'))}</b> — {state}",
            f"📦 مصرف: {quota_txt}",
            f"⏳ انقضا: {exp_txt}"]


async def _send_sub(store, token: str, chat_id, ib, title: str):
    """Send subscription URL + QR photo. Falls back to path when no domain."""
    db = await store.get()
    url = _sub_url(db, ib)
    if url:
        png = make_qr_png(url)
        cap = f"🔗 <b>{esc(title)}</b>\n<code>{esc(url)}</code>"
        if png and await send_photo(token, chat_id, png, cap):
            return
        await send_message(token, chat_id, cap)
    else:
        tok = ib.get("sub_token") or ib.get("uid")
        await send_message(token, chat_id,
                           f"🔗 <b>{esc(title)}</b>\nمسیر ساب: <code>/s/{tok}</code>\n"
                           f"ℹ️ برای لینک کامل، دامنه عمومی را در تنظیمات پنل ثبت کنید.",
                           markup=account_hub_kb())


async def _admin_notify(store, text: str, markup: dict | None = None):
    try:
        db = await store.get()
        cfg = _shop_cfg(db)
        if cfg["token"] and cfg["admin_chat"]:
            await send_message(cfg["token"], cfg["admin_chat"], text, markup=markup)
    except Exception:
        pass


async def notify(store, kind: str, text: str):
    """Fire-and-forget admin notification if telegram is enabled."""
    try:
        db = await store.get()
        s = db.get("settings", {})
        if not s.get("telegram_enabled"):
            return
        token, chat, _src = resolve_creds(s)
        if not token or not chat:
            return
        flag = {"new_user": "notify_new_user", "quota": "notify_quota",
                "expiry": "notify_expiry", "login": "notify_login",
                "server": "notify_server"}.get(kind)
        if flag and s.get(flag) is False:
            return
        await send_message(token, chat, text)
    except Exception as e:
        log.warning("telegram notify failed: %s", e)


# ============================== customer flows ===============================
async def shop_show_plans(store, token: str, chat_id):
    db = await store.get()
    if not _shop_cfg(db)["enabled"]:
        await send_message(token, chat_id, "⛔ فروشگاه در حال حاضر غیرفعال است.")
        return
    plans = [p for p in db.get("plans", []) if p.get("enabled", True)]
    if not plans:
        await send_message(token, chat_id, "در حال حاضر پلنی موجود نیست. با پشتیبانی در تماس باشید.",
                           markup=main_menu_kb())
        return
    rows = []
    for p in plans:
        rows.append([_btn(f"{p.get('name')} — {fmt_gb(p.get('traffic_gb'))}GB / "
                          f"{p.get('duration_days')}d — {_fmt_money(p.get('price') or 0)}",
                          f"buy:{p.get('id')}")])
    rows.append([_btn("⬅️ منوی اصلی", "nav:main")])
    await send_message(token, chat_id,
                       f"🛒 <b>پلن مورد نظر را انتخاب کنید:</b>\n"
                       f"💡 موجودی کیف پول شما: {_fmt_money((find_customer(db, chat_id) or {}).get('balance') or 0)}",
                       markup={"inline_keyboard": rows})


async def shop_plan_detail(store, token: str, chat_id, tg_id, plan_id: str, message_id=None):
    db = await store.get()
    plan = next((p for p in db.get("plans", []) if p.get("id") == plan_id), None)
    if not plan:
        await send_message(token, chat_id, "❌ این پلن نامعتبر است.")
        return
    cust = find_customer(db, tg_id) or {}
    price = float(plan.get("price") or 0)
    txt = (f"📦 <b>{esc(plan.get('name'))}</b>\n"
           f"📊 حجم: {fmt_gb(plan.get('traffic_gb'))} GB\n"
           f"⏳ مدت: {plan.get('duration_days')} روز\n"
           f"📱 دستگاه: {plan.get('device_limit') or '∞'}\n"
           f"💰 قیمت: {_fmt_money(price)}\n"
           f"👛 موجودی شما: {_fmt_money(cust.get('balance') or 0)}")
    kb = payment_kb(f"{plan_id}", price, float(cust.get("balance") or 0))
    if message_id:
        await edit_message(token, chat_id, message_id, txt, kb)
    else:
        await send_message(token, chat_id, txt, markup=kb)


async def shop_buy_confirm_pay(store, token: str, chat_id, tg_user: dict, plan_id: str):
    """Charge the wallet and provision the config — all on real panel data."""
    db = await store.get()
    cfg = _shop_cfg(db)
    plan = next((p for p in db.get("plans", []) if p.get("id") == plan_id and p.get("enabled", True)), None)
    cust = find_customer(db, int(tg_user.get("id") or 0))
    if not plan or not cust:
        await send_message(token, chat_id, "❌ نامعتبر است. دوباره تلاش کنید.")
        return
    price = float(plan.get("price") or 0)
    if float(cust.get("balance") or 0) < price:
        await send_message(token, chat_id,
                           f"❌ موجودی کافی نیست.\n💰 قیمت: {_fmt_money(price)}\n"
                           f"👛 موجودی: {_fmt_money(cust.get('balance') or 0)}",
                           markup=wallet_kb())
        return
    n = len(cust.get("inbound_uids") or []) + 1
    ib = _make_inbound_dict(f"tg{cust['tg_id']}-{n}", float(plan.get("traffic_gb") or 0),
                            int(plan.get("duration_days") or 0),
                            (db.get("settings") or {}).get("default_fingerprint", "chrome"), "shop")
    try:
        from main import apply_plan_to_inbound as _apply_plan
        _apply_plan(ib, plan, now=time.time())
    except Exception:
        pass
    bonus_txt = ""
    if float(cust.get("pending_bonus") or 0) > 0:
        ib["quota_gb"] = round(ib["quota_gb"] + float(cust["pending_bonus"]), 2)
        bonus_txt = f"\n🎁 جایزه گردونه اعمال شد: +{float(cust['pending_bonus']):g}GB"
    first_order = not (cust.get("inbound_uids") or [])

    def _a(db):
        u = find_customer(db, cust["tg_id"])
        u["balance"] = round(float(u.get("balance") or 0) - price, 2)
        u["pending_bonus"] = 0.0
        u.setdefault("inbound_uids", []).append(ib["uid"])
        db["inbounds"].append(ib)
        if first_order and u.get("referred_by") and cfg["bonus"] > 0:
            ref = find_customer(db, u["referred_by"])
            if ref:
                ref["balance"] = round(float(ref.get("balance") or 0) + cfg["bonus"], 2)
                ref.setdefault("referrals", [])
                ref["referral_earned"] = round(float(ref.get("referral_earned") or 0) + cfg["bonus"], 2)
    await store.mutate(_a)
    _refresh_panel()
    if first_order and cust.get("referred_by") and cfg["bonus"] > 0:
        try:
            await send_message(token, str(cust["referred_by"]),
                               f"🎉 زیرمجموعه شما خرید کرد!\n💰 +{_fmt_money(cfg['bonus'])} به کیف پول شما اضافه شد.")
        except Exception:
            pass
    await _admin_notify(store, f"🛒 <b>خرید جدید</b>\n"
                               f"👤 {esc(cust.get('first_name') or cust.get('username') or cust['tg_id'])}\n"
                               f"📦 {esc(plan.get('name'))} — {_fmt_money(price)}")
    await send_message(token, chat_id, f"✅ <b>خرید با موفقیت انجام شد!</b>{bonus_txt}",
                       markup=main_menu_kb())
    await _send_sub(store, token, chat_id, ib, plan.get("name"))


async def shop_free_test(store, token: str, chat_id, tg_user: dict):
    db = await store.get()
    cfg = _shop_cfg(db)
    tg_id = int(tg_user.get("id") or 0)
    # Ensure the customer record exists (a user may tap "تست رایگان" without /start)
    await get_customer(store, tg_user)
    cust = find_customer(db, tg_id) or {}
    if cust.get("test_claimed"):
        await send_message(token, chat_id, "❌ شما قبلاً کانفیگ تست را دریافت کرده‌اید.",
                           markup=main_menu_kb())
        return
    ib = _make_inbound_dict(f"tg{cust.get('tg_id', chat_id)}-test",
                            cfg["test_gb"], cfg["test_days"],
                            (db.get("settings") or {}).get("default_fingerprint", "chrome"),
                            "free-test")

    def _a(db):
        u = find_customer(db, tg_id)
        if not u:
            return
        u["test_claimed"] = True
        u.setdefault("inbound_uids", []).append(ib["uid"])
        db["inbounds"].append(ib)
    await store.mutate(_a)
    _refresh_panel()
    await send_message(token, chat_id,
                       f"🧪 <b>کانفیگ تست فعال شد</b>\n"
                       f"📊 {cfg['test_gb']:g}GB · ⏳ {cfg['test_days']} روز\n"
                       f"ℹ️ این کانفیگ یک‌بار به هر کاربر داده می‌شود.")
    await _send_sub(store, token, chat_id, ib, "تست رایگان")


async def shop_account(store, token: str, chat_id, tg_id):
    db = await store.get()
    cust = find_customer(db, tg_id)
    uids = (cust.get("inbound_uids") or []) if cust else []
    live = [ib for ib in (_inbound_by_uid(db, u) for u in uids) if ib]
    enabled = sum(1 for ib in live if ib.get("enabled", True))
    total_used = sum((ib.get("used_up") or 0) + (ib.get("used_down") or 0) for ib in live)
    txt = (f"🧾 <b>حساب کاربری من</b>\n"
           f"👤 {esc((cust or {}).get('first_name') or (cust or {}).get('username') or tg_id)}\n"
           f"📦 سرویس‌ها: {len(live)} (فعال: {enabled})\n"
           f"📊 مصرف کل: {fmt_bytes(total_used)}\n"
           f"👛 موجودی: {_fmt_money((cust or {}).get('balance') or 0)}\n"
           f"🤝 دعوت‌شده‌ها: {len((cust or {}).get('referrals') or [])}")
    await send_message(token, chat_id, txt, markup=account_hub_kb())


async def shop_my_services(store, token: str, chat_id, tg_id):
    db = await store.get()
    cust = find_customer(db, tg_id)
    uids = (cust.get("inbound_uids") or []) if cust else []
    live = [ib for ib in (_inbound_by_uid(db, u) for u in uids) if ib]
    if not live:
        await send_message(token, chat_id, "📦 هنوز سرویسی ندارید.\nاز «🛒 خرید کانفیگ» شروع کنید.",
                           markup=account_hub_kb())
        return
    rows = []
    for ib in live[-10:]:
        st = "✅" if ib.get("enabled", True) else "⛔"
        rows.append([_btn(f"{st} {ib.get('name')}", f"svc:{ib['uid']}")])
    rows.append([_btn("⬅️ بازگشت", "nav:account")])
    await send_message(token, chat_id, "📦 <b>سرویس‌های شما:</b>", markup={"inline_keyboard": rows})


async def shop_service_detail(store, token: str, chat_id, uid: str, message_id=None):
    db = await store.get()
    ib = _inbound_by_uid(db, uid)
    if not ib:
        await send_message(token, chat_id, "❌ سرویس یافت نشد.")
        return
    lines = _user_snapshot_lines(ib)
    kb = {"inline_keyboard": [
        [_btn("🔗 لینک و QR", f"svc:link:{uid}"), _btn("🔄 بروزرسانی", f"svc:{uid}")],
        [_btn("📄 دانلود کانفیگ", f"svc:txt:{uid}")],
        [_btn("⬅️ بازگشت", "mo:list")],
    ]}
    txt = "\n".join(lines)
    if message_id:
        await edit_message(token, chat_id, message_id, txt, kb)
    else:
        await send_message(token, chat_id, txt, markup=kb)


async def shop_service_link(store, token: str, chat_id, uid: str):
    db = await store.get()
    ib = _inbound_by_uid(db, uid)
    if not ib:
        await send_message(token, chat_id, "❌ سرویس یافت نشد.")
        return
    await _send_sub(store, token, chat_id, ib, f"لینک {ib.get('name')}")


async def shop_service_txt(store, token: str, chat_id, uid: str):
    """Build the .txt export from the panel's own link builder (identical output
    to the web panel's 'download configs' button)."""
    try:
        import main as _main
        db = await store.get()
        ib = _inbound_by_uid(db, uid)
        if not ib:
            await send_message(token, chat_id, "❌ سرویس یافت نشد.")
            return
        base = _public_base(db)

        class _R:
            pass
        r = _R()
        r.headers = {"host": (db.get("settings") or {}).get("public_domain") or "localhost"}
        r.url = type("U", (), {"scheme": "https"})()
        links = _main.build_links(r, db, ib)
        body = (f"# ALOO PANEL — {ib.get('name','')}\n"
                f"# generated {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                + "\n".join(links["all_links"]) + "\n")
        await send_document(token, chat_id, body.encode("utf-8"),
                            f"aloo-{ib.get('name','config')}.txt",
                            f"📄 کانفیگ‌های {esc(ib.get('name'))}")
    except Exception as e:
        log.warning("config txt failed: %s", e)
        await send_message(token, chat_id, "❌ ساخت فایل کانفیگ ناموفق بود.")


async def shop_usage(store, token: str, chat_id, tg_id):
    """Usage + a real exhaustion forecast from measured burn rate."""
    db = await store.get()
    cust = find_customer(db, tg_id)
    uids = (cust.get("inbound_uids") or []) if cust else []
    live = [ib for ib in (_inbound_by_uid(db, u) for u in uids) if ib]
    if not live:
        await send_message(token, chat_id, "📊 داده‌ای برای نمایش نیست.", markup=account_hub_kb())
        return

    try:
        import pro_features as _pro
    except Exception:
        _pro = None

    lines = ["📊 <b>مصرف سرویس‌های من:</b>"]
    warns = []
    for ib in live[-10:]:
        used = (ib.get("used_up") or 0) + (ib.get("used_down") or 0)
        q = ib.get("quota_gb") or 0
        pct = f" ({(used / (q * 1024**3) * 100):.0f}%)" if q > 0 else ""
        lines.append(f"• <b>{esc(ib.get('name'))}</b>: {fmt_bytes(used)}{pct}")

        # real forecast per service
        if _pro and q > 0:
            try:
                hist = _pro.history_for(ib.get("uid"), db)
                p = _pro.predict_quota(ib, hist)
            except Exception:
                p = None
            if p and p.get("days_left") is not None:
                d = p["days_left"]
                burn = p.get("burn_bytes_per_day")
                burn_txt = f" | نرخ: {fmt_bytes(burn)}/روز" if burn else ""
                lines.append(f"   ⏳ تخمین اتمام: <b>{d:.1f} روز</b>{burn_txt}")
                if p["level"] in ("critical", "warning"):
                    warns.append(f"• {esc(ib.get('name'))}: {d:.1f} روز تا اتمام")

    if warns:
        lines.append("")
        lines.append("⚠️ <b>هشدار اتمام حجم:</b>")
        lines.extend(warns)
    elif _pro:
        only_unknown = all(
            (_pro.predict_quota(ib, _pro.history_for(ib.get("uid"), db)).get("days_left") is None)
            for ib in live[-10:] if (ib.get("quota_gb") or 0) > 0
        ) if live else False
        if only_unknown:
            lines.append("")
            lines.append("ℹ️ برای پیش‌بینی دقیق، چند روز مصرف لازم است.")

    await send_message(token, chat_id, "\n".join(lines), markup=account_hub_kb())


async def shop_wallet(store, token: str, chat_id, tg_id):
    db = await store.get()
    cust = find_customer(db, tg_id) or {}
    pend = [t for t in db.get("topup_requests", [])
            if int(t.get("tg_id") or 0) == int(tg_id or 0) and t.get("status") == "pending"]
    txt = (f"👛 <b>کیف پول شما</b>\n"
           f"💰 موجودی: {_fmt_money(cust.get('balance') or 0)}")
    if pend:
        txt += "\n\n⏳ <b>در انتظار تأیید:</b> " + ", ".join(_fmt_money(t.get("amount")) for t in pend)
    txt += f"\n\nℹ️ حداقل مبلغ شارژ: {_fmt_money(TOPUP_MIN)}"
    await send_message(token, chat_id, txt, markup=wallet_kb())


async def shop_topup_request(store, token: str, chat_id, tg_user: dict, amount: int):
    if amount < TOPUP_MIN:
        await send_message(token, chat_id, f"❌ حداقل مبلغ شارژ {_fmt_money(TOPUP_MIN)} است.")
        return
    rid = "tp_" + secrets.token_hex(6)
    rec = {"id": rid, "tg_id": int(tg_user.get("id") or 0),
           "username": (tg_user.get("username") or "")[:64],
           "first_name": (tg_user.get("first_name") or "")[:64],
           "amount": amount, "status": "pending", "ts": time.time()}

    def _a(db):
        db.setdefault("topup_requests", []).append(rec)
    await store.mutate(_a)
    kb = {"inline_keyboard": [[
        _btn("✅ تأیید", f"tp_ok:{rid}"),
        _btn("❌ رد", f"tp_no:{rid}")]]}
    await _admin_notify(store,
                        f"💰 <b>درخواست شارژ کیف پول</b>\n"
                        f"👤 {esc(rec['first_name'] or rec['username'] or rec['tg_id'])}\n"
                        f"💵 مبلغ: {_fmt_money(amount)}\n🆔 <code>{rid}</code>", markup=kb)
    await send_message(token, chat_id,
                       f"✅ درخواست شارژ {_fmt_money(amount)} ثبت شد.\n"
                       f"پس از تأیید ادمین، کیف پول شما به‌صورت خودکار شارژ می‌شود.",
                       markup=wallet_kb())


async def shop_wallet_history(store, token: str, chat_id, tg_id):
    db = await store.get()
    mine = [t for t in db.get("topup_requests", []) if int(t.get("tg_id") or 0) == int(tg_id or 0)]
    if not mine:
        await send_message(token, chat_id, "📜 تاریخچه‌ای وجود ندارد.", markup=wallet_kb())
        return
    icon = {"pending": "⏳", "approved": "✅", "denied": "❌"}
    lines = ["📜 <b>تاریخچه شارژ:</b>"]
    for t in mine[-15:]:
        ts = time.strftime("%Y-%m-%d", time.localtime(t.get("ts") or 0))
        lines.append(f"{icon.get(t.get('status'), '•')} {_fmt_money(t.get('amount'))} — {ts}")
    await send_message(token, chat_id, "\n".join(lines), markup=wallet_kb())


async def topup_decide(store, rid: str, approve: bool, by: str = "admin"):
    """Approve/deny a pending top-up. Moves REAL balance. Returns the request or None."""
    found = {}

    def _a(db):
        for t in db.get("topup_requests", []):
            if t.get("id") == rid and t.get("status") == "pending":
                t["status"] = "approved" if approve else "denied"
                t["decided_by"] = str(by)[:64]
                t["decided_at"] = time.time()
                if approve:
                    u = find_customer(db, t.get("tg_id"))
                    if u:
                        u["balance"] = round(float(u.get("balance") or 0) + float(t.get("amount") or 0), 2)
                found.update(t)
    await store.mutate(_a)
    req = found or None
    if req:
        try:
            db = await store.get()
            cfg = _shop_cfg(db)
            if cfg["token"]:
                if approve:
                    await send_message(cfg["token"], str(req["tg_id"]),
                                       f"✅ شارژ {_fmt_money(req['amount'])} تأیید و به کیف پول شما اضافه شد.",
                                       markup=wallet_kb())
                else:
                    await send_message(cfg["token"], str(req["tg_id"]),
                                       "❌ درخواست شارژ شما رد شد. با پشتیبانی در تماس باشید.",
                                       markup=wallet_kb())
        except Exception:
            pass
    return req


async def shop_referral(store, token: str, chat_id, tg_id):
    db = await store.get()
    cfg = _shop_cfg(db)
    cust = find_customer(db, tg_id) or {}
    link = (f"https://t.me/{cfg['bot_username']}?start=ref_{tg_id}"
            if cfg["bot_username"] else "نام‌کاربری ربات هنوز در تنظیمات ثبت نشده است")
    count = len(cust.get("referrals") or [])
    await send_message(token, chat_id,
                       f"🤝 <b>زیرمجموعه‌گیری</b>\n\n"
                       f"🔗 لینک اختصاصی شما:\n<code>{esc(link)}</code>\n\n"
                       f"👥 تعداد دعوت‌شده‌ها: <b>{count}</b>\n"
                       f"💰 درآمد کل: <b>{_fmt_money(cust.get('referral_earned') or 0)}</b>\n"
                       f"🎁 پاداش هر خرید اول زیرمجموعه: <b>{_fmt_money(cfg['bonus'])}</b>\n\n"
                       f"ℹ️ این لینک را برای دوستانتان بفرستید؛ با اولین خرید آن‌ها، پاداش به کیف پول شما واریز می‌شود.",
                       markup=main_menu_kb())


async def shop_wheel(store, token: str, chat_id, tg_user: dict):
    import datetime as _dt
    tg_id = int(tg_user.get("id") or 0)
    today = _dt.date.today().isoformat()
    db = await store.get()
    cust = find_customer(db, tg_id) or {}
    if cust.get("last_spin") == today:
        await send_message(token, chat_id, "🎡 شانس امروز شما استفاده شده. فردا دوباره امتحان کنید! 🍀",
                           markup=main_menu_kb())
        return
    total = sum(w for _, w in WHEEL_PRIZES)
    roll = random.uniform(0, total)
    prize = WHEEL_PRIZES[-1][0]
    acc = 0
    for gb, w in WHEEL_PRIZES:
        acc += w
        if roll <= acc:
            prize = gb
            break
    # credit to the newest enabled config, else park as pending bonus
    target = None
    for u in reversed(cust.get("inbound_uids") or []):
        ib = _inbound_by_uid(db, u)
        if ib and ib.get("enabled", True):
            target = ib["uid"]
            break

    def _a(db):
        u = find_customer(db, tg_id)
        u["last_spin"] = today
        if target:
            for x in db.get("inbounds", []):
                if x.get("uid") == target:
                    x["quota_gb"] = round((x.get("quota_gb") or 0) + prize, 2)
        else:
            u["pending_bonus"] = round(float(u.get("pending_bonus") or 0) + prize, 2)
    await store.mutate(_a)
    if target:
        _refresh_panel()
        await send_message(token, chat_id,
                           f"🎡🎉 <b>تبریک!</b>\n+{prize} گیگ به سرویس فعال شما اضافه شد.",
                           markup=main_menu_kb())
    else:
        await send_message(token, chat_id,
                           f"🎡🎉 <b>تبریک!</b>\n+{prize} گیگ برنده شدید؛ با اولین خرید به حسابتان اضافه می‌شود.",
                           markup=main_menu_kb())


async def shop_support(store, token: str, chat_id):
    db = await store.get()
    await send_message(token, chat_id,
                       "📞 <b>پشتیبانی</b>\nچطور می‌توانیم کمکتان کنیم؟",
                       markup=support_kb(db))


# =============================== AI assistant ================================
_AI_HISTORY: dict = {}


async def shop_ai_start(store, token: str, chat_id):
    _AI_HISTORY[int(chat_id)] = []
    await send_message(token, chat_id,
                       "🤖 <b>دستیار هوشمند</b>\nسوال خود را بنویسید. اگر نتوانم پاسخ دهم، "
                       "شما را به پشتیبانی انسانی وصل می‌کنم.",
                       markup={"inline_keyboard": [
                           [_btn("📞 صحبت با پشتیبانی انسانی", "sup:human")],
                           [_btn("❌ پایان گفتگو", "nav:main")]]})


async def shop_ai_reply(store, token: str, chat_id, tg_user: dict, text: str):
    """Answer with the panel's own AI endpoint when configured; otherwise give a
    deterministic, data-backed answer about the user's REAL account state."""
    db = await store.get()
    cust = find_customer(db, int(tg_user.get("id") or 0)) or {}
    low = (text or "").strip().lower()

    # data-backed answers first — these are always accurate
    # NOTE: check the *specific* money/balance intent BEFORE the generic
    # "چقدر" (how much) keyword, otherwise "موجودی من چقدره؟" is misread as a
    # usage question.
    if any(k in low for k in ("موجودی", "کیف پول", "balance", "پول")):
        await send_message(token, chat_id,
                           f"👛 موجودی کیف پول شما: <b>{_fmt_money(cust.get('balance') or 0)}</b>",
                           markup=support_kb(db))
        return
    if any(k in low for k in ("مصرف", "چقدر", "حجم", "usage")):
        uids = cust.get("inbound_uids") or []
        live = [ib for ib in (_inbound_by_uid(db, u) for u in uids) if ib]
        if live:
            lines = ["📊 <b>مصرف فعلی شما:</b>"]
            for ib in live[-5:]:
                used = (ib.get("used_up") or 0) + (ib.get("used_down") or 0)
                q = ib.get("quota_gb") or 0
                lines.append(f"• {esc(ib.get('name'))}: {fmt_bytes(used)} از {fmt_gb(q)} GB")
            await send_message(token, chat_id, "\n".join(lines), markup=support_kb(db))
            return
    if any(k in low for k in ("خرید", "پلن", "قیمت", "buy")):
        await send_message(token, chat_id,
                           "🛒 برای خرید، از «🛒 خرید کانفیگ» در منوی اصلی استفاده کنید.\n"
                           "همه پلن‌ها و قیمت‌ها آنجا نمایش داده می‌شوند.",
                           markup=support_kb(db))
        return
    if any(k in low for k in ("لینک", "کانفیگ", "اتصال", "وصل", "sub")):
        uids = cust.get("inbound_uids") or []
        live = [ib for ib in (_inbound_by_uid(db, u) for u in uids) if ib]
        if live:
            await _send_sub(store, token, chat_id, live[-1], f"لینک {live[-1].get('name')}")
            return
        await send_message(token, chat_id, "هنوز سرویسی ندارید. ابتدا از «🛒 خرید کانفیگ» خرید کنید.",
                           markup=support_kb(db))
        return

    # try the panel's configured AI backend
    try:
        import main as _main
        reply = await _main.ai_answer(db, text)
        if reply:
            await send_message(token, chat_id, f"🤖 {reply}", markup=support_kb(db))
            return
    except Exception:
        pass

    await send_message(token, chat_id,
                       "🤖 متأسفم، نتوانستم پاسخ مناسبی پیدا کنم.\n"
                       "می‌توانید سوال را واضح‌تر بپرسید یا با پشتیبانی انسانی صحبت کنید.",
                       markup=support_kb(db))


async def shop_human_support(store, token: str, chat_id, tg_user: dict):
    db = await store.get()
    cfg = _shop_cfg(db)
    if not cfg["admin_chat"]:
        await send_message(token, chat_id, "📞 پشتیبانی در حال حاضر در دسترس نیست.")
        return
    nm = esc(tg_user.get("first_name") or tg_user.get("username") or tg_user.get("id"))
    uname = f" (@{tg_user.get('username')})" if tg_user.get("username") else ""
    await _admin_notify(store,
                        f"📞 <b>درخواست پشتیبانی</b>\n👤 {nm}{uname}\n🔗 <a href=\"tg://user?id={tg_user.get('id')}\">پاسخ مستقیم</a>")
    await send_message(token, chat_id,
                       "✅ درخواست شما به پشتیبانی ارسال شد.\nبه‌زودی پاسخ می‌دهیم.",
                       markup=main_menu_kb())


# ================================ admin flows =================================
async def admin_panel(store, token: str, chat_id):
    db = await store.get()
    custs = db.get("bot_users", [])
    pend = [t for t in db.get("topup_requests", []) if t.get("status") == "pending"]
    inbounds = db.get("inbounds", [])
    total_up = (db.get("stats") or {}).get("total_up", 0)
    total_down = (db.get("stats") or {}).get("total_down", 0)
    await send_message(token, chat_id,
                       f"⚙️ <b>پنل مدیریت</b>\n\n"
                       f"👥 مشتریان ربات: <b>{len(custs)}</b>\n"
                       f"📦 کاربران پنل: <b>{len(inbounds)}</b>\n"
                       f"⏳ شارژهای در انتظار: <b>{len(pend)}</b>\n"
                       f"📊 ترافیک کل: <b>{fmt_bytes(total_up + total_down)}</b>",
                       markup=admin_panel_kb())


async def admin_stats(store, token: str, chat_id):
    db = await store.get()
    inbounds = db.get("inbounds", [])
    now = time.time()
    active = [ib for ib in inbounds if ib.get("enabled", True)]
    expired = [ib for ib in inbounds if ib.get("expire_at") and ib["expire_at"] <= now]
    total_up = (db.get("stats") or {}).get("total_up", 0)
    total_down = (db.get("stats") or {}).get("total_down", 0)

    pro = {}
    for ib in inbounds:
        for p in (ib.get("protocols") or ["vless", "vmess"]):
            pro[p] = pro.get(p, 0) + 1

    await send_message(token, chat_id,
                       f"📊 <b>آمار پنل</b>\n\n"
                       f"👥 کل کاربران: <b>{len(inbounds)}</b>\n"
                       f"✅ فعال: <b>{len(active)}</b>\n"
                       f"❌ منقضی: <b>{len(expired)}</b>\n"
                       f"🛒 مشتریان ربات: <b>{len(db.get('bot_users', []))}</b>\n"
                       f"⏳ شارژ در انتظار: <b>{len([t for t in db.get('topup_requests', []) if t.get('status') == 'pending'])}</b>\n\n"
                       f"📈 آپلود کل: <b>{fmt_bytes(total_up)}</b>\n"
                       f"📉 دانلود کل: <b>{fmt_bytes(total_down)}</b>\n\n"
                       f"🔌 <b>توزیع پروتکل:</b>\n" +
                       "\n".join(f"  • {p}: {n}" for p, n in sorted(pro.items(), key=lambda x: -x[1])),
                       markup=admin_panel_kb())


async def admin_grid(store, token: str, chat_id, uids: list):
    """Paginated user grid with real consumption."""
    db = await store.get()
    if not uids:
        await send_message(token, chat_id, "کاربری وجود ندارد.", markup=admin_panel_kb())
        return
    chunk = uids[:20]
    lines = [f"👥 <b>کاربران ({len(uids)}):</b>"]
    for u in chunk:
        ib = _inbound_by_uid(db, u)
        if not ib:
            continue
        used = (ib.get("used_up") or 0) + (ib.get("used_down") or 0)
        mark = "✅" if ib.get("enabled", True) else "⛔"
        lines.append(f"{mark} {esc(ib.get('name'))} — {fmt_bytes(used)}")
    if len(uids) > 20:
        lines.append(f"\n… و {len(uids) - 20} کاربر دیگر")
    await send_message(token, chat_id, "\n".join(lines), markup=admin_panel_kb())


async def admin_topups(store, token: str, chat_id):
    db = await store.get()
    pend = [t for t in db.get("topup_requests", []) if t.get("status") == "pending"]
    if not pend:
        await send_message(token, chat_id, "✅ هیچ شارژ در انتظاری وجود ندارد.", markup=admin_panel_kb())
        return
    for t in pend[:10]:
        kb = {"inline_keyboard": [[
            _btn("✅ تأیید", f"tp_ok:{t['id']}"),
            _btn("❌ رد", f"tp_no:{t['id']}")]]}
        await send_message(token, chat_id,
                           f"💰 <b>شارژ در انتظار</b>\n"
                           f"👤 {esc(t.get('first_name') or t.get('username') or t.get('tg_id'))}\n"
                           f"💵 {_fmt_money(t.get('amount'))}\n🆔 <code>{t.get('id')}</code>", markup=kb)


async def admin_products(store, token: str, chat_id):
    db = await store.get()
    plans = db.get("plans", [])
    lines = ["🛒 <b>پلن‌های فروشگاه:</b>"]
    for p in plans:
        mark = "✅" if p.get("enabled", True) else "⛔"
        lines.append(f"{mark} <b>{esc(p.get('name'))}</b> — {fmt_gb(p.get('traffic_gb'))}GB / "
                     f"{p.get('duration_days')}d — {_fmt_money(p.get('price') or 0)}")
    lines.append("\nℹ️ ویرایش کامل پلن‌ها از پنل وب (بخش پلن‌ها) انجام می‌شود.")
    await send_message(token, chat_id, "\n".join(lines), markup=admin_panel_kb())


async def admin_customers(store, token: str, chat_id):
    db = await store.get()
    custs = db.get("bot_users", [])
    if not custs:
        await send_message(token, chat_id, "هنوز مشتری‌ای ثبت نشده.", markup=admin_panel_kb())
        return
    lines = [f"👥 <b>مشتریان ربات ({len(custs)}):</b>"]
    for c in custs[:25]:
        nm = esc(c.get("first_name") or c.get("username") or c.get("tg_id"))
        lines.append(f"• {nm} — 💰 {_fmt_money(c.get('balance') or 0)} — "
                     f"📦 {len(c.get('inbound_uids') or [])} سرویس")
    if len(custs) > 25:
        lines.append(f"\n… و {len(custs) - 25} مشتری دیگر")
    await send_message(token, chat_id, "\n".join(lines), markup=admin_panel_kb())


async def admin_server_status(store, token: str, chat_id):
    """Server status + a REAL benchmark of the best node and at-risk users."""
    db = await store.get()
    import main as _main
    try:
        xs = _main.xray_service_status()
    except Exception:
        xs = {"status": "unknown"}
    stats = await _main.gather_system_stats()

    # real best-node measurement (from the last benchmark, not invented)
    best_line = "—"
    try:
        import pro_features as _pro
        loc = db.get("local_probe") or {}
        cands = []
        if loc.get("latency_ms") is not None:
            cands.append((loc.get("name") or "Local", loc["latency_ms"],
                          loc.get("grade"), loc.get("score", 0)))
        for s in db.get("servers", []):
            if s.get("latency_ms") is not None and s.get("enabled", True):
                cands.append((s.get("name"), s["latency_ms"],
                              s.get("latency_grade"), _pro.score_node(
                                  {"ok": True, "avg_ms": s["latency_ms"],
                                   "jitter_ms": s.get("jitter_ms"),
                                   "loss_percent": s.get("loss_percent")}, None,
                                  s.get("load"))))
        if cands:
            cands.sort(key=lambda c: c[3])
            n, lat, grade, _ = cands[0]
            best_line = f"{esc(n)} — {lat}ms ({esc(grade or '?')})"
    except Exception:
        pass

    # real at-risk users
    risk = []
    try:
        import pro_features as _pro
        for ib in db.get("inbounds", []):
            if not (ib.get("quota_gb") or 0):
                continue
            p = _pro.predict_quota(ib, _pro.history_for(ib.get("uid"), db))
            if p["level"] in ("critical", "warning"):
                risk.append((p["level"], ib.get("name"), p.get("days_left")))
        risk.sort(key=lambda r: (0 if r[0] == "critical" else 1, r[2] or 999))
    except Exception:
        pass

    txt = (f"🔧 <b>وضعیت سرور</b>\n\n"
           f"🖥 CPU: <b>{stats.get('cpu_percent', 0):.1f}%</b>\n"
           f"🧠 RAM: <b>{stats.get('mem_percent', 0):.1f}%</b>\n"
           f"⏱ آپتایم: <b>{_main.fmt_duration_short(stats.get('uptime_seconds', 0))}</b>\n"
           f"🔌 Xray: <b>{esc(xs.get('status'))}</b>\n"
           f"👥 کاربران: <b>{len(db.get('inbounds', []))}</b>\n"
           f"⚡ بهترین نود: <b>{best_line}</b>")
    if risk:
        txt += "\n\n⚠️ <b>کاربران در خطر اتمام حجم:</b>"
        for lvl, name, days in risk[:5]:
            icon = "🔴" if lvl == "critical" else "🟡"
            dtxt = f"{days:.1f} روز" if days is not None else "—"
            txt += f"\n{icon} {esc(name)} — {dtxt}"
    await send_message(token, chat_id, txt, markup=admin_panel_kb())


async def admin_benchmark(store, token: str, chat_id):
    """Measure REAL latency to every node and report the best one."""
    db = await store.get()
    await send_message(token, chat_id, "⚡ در حال سنجش واقعی سرورها…")
    import main as _main
    try:
        targets = await _main._all_probe_targets(db)
    except Exception:
        targets = []
    if not targets:
        await send_message(token, chat_id,
                           "❌ نودی برای سنجش پیدا نشد.\n"
                           "دامنه عمومی را در تنظیمات یا یک سرور اضافه کنید.",
                           markup=admin_panel_kb())
        return

    import pro_features as _pro
    import asyncio as _aio

    async def one(t):
        r = await _pro.probe_server(t["host"], t["port"], samples=3)
        r.update({"name": t["name"]})
        r["score"] = _pro.score_node(r)
        return r

    results = await _aio.gather(*(one(t) for t in targets))
    results.sort(key=lambda r: r.get("score", 99999))

    lines = ["⚡ <b>نتیجه سنجش واقعی:</b>"]
    for r in results[:8]:
        icon = {"excellent": "🟢", "good": "🟢", "fair": "🟡",
                "slow": "🟠", "poor": "🔴", "offline": "⚫"}.get(r["grade"], "⚪")
        if r["ok"]:
            lines.append(f"{icon} <b>{esc(r['name'])}</b> — {r['avg_ms']}ms "
                         f"(لرزش {r['jitter_ms']}ms، افت {r['loss_percent']}%)")
        else:
            lines.append(f"{icon} <b>{esc(r['name'])}</b> — در دسترس نیست")
    ok = [r for r in results if r["ok"]]
    if ok:
        lines.append(f"\n🏆 بهترین: <b>{esc(ok[0]['name'])}</b> با {ok[0]['avg_ms']}ms")

    # persist so the dashboard and auto-pick use these measurements
    now = time.time()
    measured = {r.get("host"): r for r in results}

    def _save(d):
        for s in d.get("servers", []):
            h = s.get("host")
            r = measured.get(h)
            if r:
                s["latency_ms"] = r.get("avg_ms")
                s["jitter_ms"] = r.get("jitter_ms")
                s["loss_percent"] = r.get("loss_percent")
                s["latency_grade"] = r.get("grade")
                s["last_probe"] = now
        loc = next((r for r in results if r.get("host")), None)
        if loc:
            d["local_probe"] = {
                "name": loc.get("name"), "host": loc.get("host"),
                "port": loc.get("port"), "latency_ms": loc.get("avg_ms"),
                "jitter_ms": loc.get("jitter_ms"),
                "loss_percent": loc.get("loss_percent"), "grade": loc.get("grade"),
                "online": loc.get("ok"), "score": loc.get("score"), "ts": now,
                "speed_mbps": None}
        d["last_benchmark"] = {"ts": now, "count": len(results),
                               "best": None}
    try:
        await store.mutate(_save)
    except Exception:
        pass
    await send_message(token, chat_id, "\n".join(lines), markup=admin_panel_kb())


async def admin_risk(store, token: str, chat_id):
    """Users who will run out of quota soon, from REAL burn rate."""
    db = await store.get()
    import pro_features as _pro
    rows = []
    for ib in db.get("inbounds", []):
        if not (ib.get("quota_gb") or 0):
            continue
        p = _pro.predict_quota(ib, _pro.history_for(ib.get("uid"), db))
        rows.append(p)
    order = {"critical": 0, "warning": 1, "watch": 2}
    rows = [r for r in rows if r["level"] in order]
    rows.sort(key=lambda r: (order[r["level"]], r.get("days_left") or 9999))

    if not rows:
        await send_message(token, chat_id,
                           "✅ هیچ کاربری در خطر اتمام حجم نیست.", markup=admin_panel_kb())
        return
    lines = [f"⚠️ <b>کاربران در خطر ({len(rows)}):</b>"]
    for p in rows[:12]:
        icon = {"critical": "🔴", "warning": "🟡", "watch": "🔵"}[p["level"]]
        days = f"{p['days_left']:.1f} روز" if p.get("days_left") is not None else "—"
        burn = fmt_bytes(p["burn_bytes_per_day"]) + "/روز" if p.get("burn_bytes_per_day") else "—"
        lines.append(f"{icon} <b>{esc(p['name'])}</b> — {days} | نرخ {burn}")
    await send_message(token, chat_id, "\n".join(lines), markup=admin_panel_kb())


async def admin_connections(store, token: str, chat_id):
    """Live connections with REAL client IPs."""
    db = await store.get()
    import pro_features as _pro
    try:
        import xray_manager as _xm
        records = _pro.parse_access_log_detailed(getattr(_xm, "XRAY_ACCESS_LOG", ""))
    except Exception:
        records = {}

    if not records:
        await send_message(token, chat_id,
                           "ℹ️ لاگ دسترسی Xray خالی است.\n"
                           "اگر Xray نصب نیست (حالت mock) اتصالی ثبت نمی‌شود.",
                           markup=admin_panel_kb())
        return

    by_uid = {ib.get("uid"): ib for ib in db.get("inbounds", [])}
    online = 0
    lines = ["🔌 <b>اتصالات زنده:</b>"]
    now = time.time()
    for uid, rec in sorted(records.items(), key=lambda kv: kv[1]["last"], reverse=True)[:12]:
        ib = by_uid.get(uid) or {}
        name = ib.get("name") or uid[:8]
        ips = list(rec["ips"].keys())
        pings = [i for i, d in rec["ips"].items() if now - d["last"] <= 300]
        if pings:
            online += 1
        icon = "🟢" if pings else "⚪"
        shown = ", ".join(f"<code>{esc(i)}</code>" for i in ips[:3])
        lines.append(f"{icon} <b>{esc(name)}</b> — {rec['total']} اتصال\n    {shown}")
    lines.append(f"\n🟢 آنلاین: <b>{online}</b> از {len(records)}")
    await send_message(token, chat_id, "\n".join(lines), markup=admin_panel_kb())


async def admin_backup(store, token: str, chat_id):
    try:
        import main as _main
        rec = await _main.do_backup(auto=False, actor="telegram-admin")
        if rec and rec.get("file"):
            with open(rec["file"], "rb") as f:
                data = f.read()
            await send_document(token, chat_id, data, os.path.basename(rec["file"]),
                                f"💾 پشتیبان‌گیری انجام شد\n{fmt_bytes(len(data))}")
        else:
            await send_message(token, chat_id, "❌ پشتیبان‌گیری ناموفق بود.", markup=admin_panel_kb())
    except Exception as e:
        log.warning("backup via bot failed: %s", e)
        await send_message(token, chat_id, "❌ پشتیبان‌گیری ناموفق بود.", markup=admin_panel_kb())


async def admin_broadcast(store, token: str, chat_id, text: str):
    db = await store.get()
    custs = db.get("bot_users", [])
    ok = fail = 0
    for c in custs:
        try:
            if await send_message(token, str(c.get("tg_id")), text):
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await send_message(token, chat_id,
                       f"📢 ارسال همگانی کامل شد.\n✅ موفق: {ok}\n❌ ناموفق: {fail}",
                       markup=admin_panel_kb())


# =============================== message router ===============================
async def handle_shop_message(store, token: str, chat_id, tg_user: dict, text: str,
                              is_admin: bool = False):
    """Customer router. Returns True if the message was consumed."""
    db = await store.get()
    cfg = _shop_cfg(db)
    t = (text or "").strip()

    # ---- admin broadcast capture ----
    if is_admin and t and not t.startswith("/"):
        pend = _ADMIN_PENDING.get(str(chat_id))
        if pend == "broadcast":
            _ADMIN_PENDING.pop(str(chat_id), None)
            await admin_broadcast(store, token, chat_id, t)
            return True

    # ---- AI chat capture ----
    if int(chat_id) in _AI_HISTORY or str(chat_id) in _AI_HISTORY:
        if t and not t.startswith("/"):
            await shop_ai_reply(store, token, chat_id, tg_user, t)
            return True

    # ---- main reply-keyboard buttons ----
    if t == BTN_MENU:
        await send_message(token, chat_id, "🏠 منوی اصلی", markup=main_menu_kb(is_admin=is_admin))
        return True
    if t == BTN_BUY:
        await shop_show_plans(store, token, chat_id)
        return True
    if t == BTN_TEST:
        await shop_free_test(store, token, chat_id, tg_user)
        return True
    if t == BTN_ACCOUNT:
        await shop_account(store, token, chat_id, int(tg_user.get("id") or 0))
        return True
    if t == BTN_WALLET:
        await shop_wallet(store, token, chat_id, int(tg_user.get("id") or 0))
        return True
    if t == BTN_REF:
        await shop_referral(store, token, chat_id, int(tg_user.get("id") or 0))
        return True
    if t == BTN_WHEEL:
        await shop_wheel(store, token, chat_id, tg_user)
        return True
    if t == BTN_SUPPORT:
        await shop_support(store, token, chat_id)
        return True
    if t == BTN_ADMIN and is_admin:
        await admin_panel(store, token, chat_id)
        return True
    return False


_ADMIN_PENDING: dict = {}


async def handle_shop_callback(store, token: str, admin_chat: str, query: dict) -> bool:
    """Inline-button router. Returns True when the callback was consumed."""
    cb_id = query.get("id", "")
    data = (query.get("data") or "").strip()
    msg = query.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    mid = msg.get("message_id")
    tg_user = query.get("from") or {}
    tg_id = int(tg_user.get("id") or 0)
    if not data or not chat_id:
        return False
    is_admin = bool(admin_chat) and str(chat_id) == str(admin_chat)

    # ---------------- navigation ----------------
    if data == "nav:main":
        await answer_callback(token, cb_id)
        await send_message(token, chat_id, "🏠 منوی اصلی", markup=main_menu_kb(is_admin=is_admin))
        _AI_HISTORY.pop(int(chat_id), None)
        return True
    if data == "nav:account":
        await answer_callback(token, cb_id)
        await send_message(token, chat_id, "🧾 حساب کاربری", markup=account_hub_kb())
        return True
    if data == "nav:wallet":
        await answer_callback(token, cb_id)
        await shop_wallet(store, token, chat_id, tg_id)
        return True

    # ---------------- buy ----------------
    if data.startswith("buy:"):
        await answer_callback(token, cb_id)
        await shop_plan_detail(store, token, chat_id, tg_id, data[4:], message_id=mid)
        return True
    if data.startswith("pay:wallet:"):
        await answer_callback(token, cb_id, "در حال پردازش…")
        await shop_buy_confirm_pay(store, token, chat_id, tg_user, data[11:])
        return True

    # ---------------- account hub ----------------
    if data == "mo:list":
        await answer_callback(token, cb_id)
        await shop_my_services(store, token, chat_id, tg_id)
        return True
    if data == "mo:usage":
        await answer_callback(token, cb_id)
        await shop_usage(store, token, chat_id, tg_id)
        return True
    if data == "mo:refresh":
        await answer_callback(token, cb_id, "بروزرسانی شد")
        await shop_my_services(store, token, chat_id, tg_id)
        return True
    if data == "mo:links_all":
        await answer_callback(token, cb_id)
        db = await store.get()
        cust = find_customer(db, tg_id)
        uids = (cust.get("inbound_uids") or []) if cust else []
        live = [ib for ib in (_inbound_by_uid(db, u) for u in uids) if ib]
        if not live:
            await send_message(token, chat_id, "هنوز سرویسی ندارید.", markup=account_hub_kb())
            return True
        await _send_sub(store, token, chat_id, live[-1], f"لینک {live[-1].get('name')}")
        return True
    if data.startswith("svc:"):
        parts = data.split(":", 2)
        if len(parts) == 3 and parts[1] == "link":
            await answer_callback(token, cb_id)
            await shop_service_link(store, token, chat_id, parts[2])
            return True
        if len(parts) == 3 and parts[1] == "txt":
            await answer_callback(token, cb_id, "در حال ساخت فایل…")
            await shop_service_txt(store, token, chat_id, parts[2])
            return True
        if len(parts) >= 2:
            await answer_callback(token, cb_id)
            await shop_service_detail(store, token, chat_id, parts[1], message_id=mid)
            return True

    # ---------------- wallet ----------------
    if data.startswith("topup:") and data[6:].isdigit():
        await answer_callback(token, cb_id)
        await shop_topup_request(store, token, chat_id, tg_user, int(data[6:]))
        return True
    if data == "wallet:history":
        await answer_callback(token, cb_id)
        await shop_wallet_history(store, token, chat_id, tg_id)
        return True
    if data.startswith("tp_ok:") or data.startswith("tp_no:"):
        if not is_admin:
            await answer_callback(token, cb_id, "⛔ فقط ادمین", alert=True)
            return True
        approve = data.startswith("tp_ok:")
        rid = data.split(":", 1)[-1]
        req = await topup_decide(store, rid, approve, by="telegram-admin")
        await answer_callback(token, cb_id,
                              "✅ تأیید شد" if (req and approve) else ("❌ رد شد" if req else "نامعتبر"))
        return True

    # ---------------- support ----------------
    if data == "sup:ai":
        await answer_callback(token, cb_id)
        await shop_ai_start(store, token, chat_id)
        return True
    if data == "sup:human":
        await answer_callback(token, cb_id)
        _AI_HISTORY.pop(int(chat_id), None)
        await shop_human_support(store, token, chat_id, tg_user)
        return True

    # ---------------- admin ----------------
    if data.startswith("adm:"):
        if not is_admin:
            await answer_callback(token, cb_id, "⛔ فقط ادمین", alert=True)
            return True
        key = data[4:]
        await answer_callback(token, cb_id)
        if key == "stats":
            await admin_stats(store, token, chat_id)
        elif key == "customers":
            await admin_customers(store, token, chat_id)
        elif key == "products":
            await admin_products(store, token, chat_id)
        elif key == "topups":
            await admin_topups(store, token, chat_id)
        elif key == "server":
            await admin_server_status(store, token, chat_id)
        elif key == "bench":
            await admin_benchmark(store, token, chat_id)
        elif key == "conns":
            await admin_connections(store, token, chat_id)
        elif key == "risk":
            await admin_risk(store, token, chat_id)
        elif key == "backup":
            await admin_backup(store, token, chat_id)
        elif key == "broadcast":
            _ADMIN_PENDING[str(chat_id)] = "broadcast"
            await send_message(token, chat_id,
                               "📢 متن پیام همگانی را بنویسید و ارسال کنید.\n"
                               "برای لغو /cancel را بزنید.",
                               markup=cancel_kb())
        return True

    return False


# ============================ admin command router ============================
async def _handle_admin_command(store, token: str, chat_id, text: str):
    parts = (text or "").strip().split()
    if not parts:
        return
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    db = await store.get()
    settings = db.get("settings") or {}

    async def reply(t: str, markup=None):
        await send_message(token, chat_id, t, markup=markup)

    if cmd == "/cancel":
        _ADMIN_PENDING.pop(str(chat_id), None)
        _AI_HISTORY.pop(int(chat_id), None)
        await reply("لغو شد.", markup=main_menu_kb(is_admin=True))
        return
    if cmd in ("/start", "/help", "/menu"):
        await reply(
            "👑 <b>ALOO PANEL Bot</b>\n\n"
            "<b>فروشگاه:</b>\n"
            f"{BTN_BUY} — خرید کانفیگ\n{BTN_TEST} — تست رایگان\n"
            f"{BTN_ACCOUNT} — حساب کاربری\n{BTN_WALLET} — کیف پول\n"
            f"{BTN_REF} — زیرمجموعه‌گیری\n{BTN_WHEEL} — گردونه شانس\n"
            f"{BTN_SUPPORT} — پشتیبانی\n\n"
            "<b>دستورات ادمین:</b>\n"
            "/stats — آمار پنل\n/users — لیست کاربران\n"
            "/user &lt;name&gt; — جزئیات کاربر\n/sub &lt;name&gt; — لینک ساب + QR\n"
            "/create &lt;name&gt; [GB] [days] — ساخت کاربر\n"
            "/reset &lt;name&gt; — ریست مصرف\n/delete &lt;name&gt; — حذف کاربر\n"
            "/broadcast — پیام همگانی\n/backup — پشتیبان‌گیری",
            markup=main_menu_kb(is_admin=True))
        return
    if cmd == "/stats":
        await admin_stats(store, token, chat_id)
        return
    if cmd == "/broadcast":
        _ADMIN_PENDING[str(chat_id)] = "broadcast"
        await reply("📢 متن پیام همگانی را بنویسید:", markup=cancel_kb())
        return
    if cmd == "/backup":
        await admin_backup(store, token, chat_id)
        return
    if cmd == "/best":
        await admin_benchmark(store, token, chat_id)
        return
    if cmd == "/risk":
        await admin_risk(store, token, chat_id)
        return
    if cmd == "/conns":
        await admin_connections(store, token, chat_id)
        return
    if cmd == "/users":
        await admin_grid(store, token, chat_id, [ib["uid"] for ib in db.get("inbounds", [])])
        return

    def find_user(q: str):
        ql = q.lower()
        for ib in db.get("inbounds", []):
            if ib.get("name", "").lower() == ql or ib.get("uid") == q:
                return ib
        for ib in db.get("inbounds", []):
            if ql in ib.get("name", "").lower():
                return ib
        return None

    if cmd == "/create":
        if not args:
            await reply("مثال: <code>/create Ali 30 30</code>")
            return
        try:
            quota = float(args[1]) if len(args) > 1 else 0
            days = int(args[2]) if len(args) > 2 else 0
        except Exception:
            await reply("مقادیر نامعتبر. مثال: <code>/create Ali 30 30</code>")
            return
        ib = _make_inbound_dict(args[0][:64], quota, days,
                                settings.get("default_fingerprint", "chrome"), "via-bot")

        def _add(db):
            db["inbounds"].append(ib)
        await store.mutate(_add)
        _refresh_panel()
        await reply("\n".join([f"✅ کاربر ساخته شد:"] + _user_snapshot_lines(ib)))
        await _send_sub(store, token, chat_id, ib, ib["name"])
        return

    if cmd in ("/user", "/sub", "/reset", "/delete"):
        if not args:
            await reply("نام کاربر را وارد کنید.")
            return
        ib = find_user(" ".join(args))
        if not ib:
            await reply("❌ کاربر یافت نشد.")
            return
        if cmd == "/user":
            q = ib.get("quota_gb") or 0
            used = (ib.get("used_up") or 0) + (ib.get("used_down") or 0)
            rem = "∞" if q <= 0 else fmt_bytes(max(0, q * 1024**3 - used))
            await reply("\n".join(_user_snapshot_lines(ib) +
                                  [f"📉 باقی‌مانده: {rem}", f"🆔 <code>{ib.get('uid')}</code>"]))
        elif cmd == "/sub":
            if ib.get("sub_enabled", True) is False:
                await reply("⛔ اشتراک این کاربر غیرفعال است.")
                return
            await _send_sub(store, token, chat_id, ib, ib.get("name"))
        elif cmd == "/reset":
            def _rst(db):
                for x in db["inbounds"]:
                    if x.get("uid") == ib.get("uid"):
                        x["used_up"] = 0
                        x["used_down"] = 0
                        x["request_count"] = 0
            await store.mutate(_rst)
            await reply(f"♻️ مصرف <b>{esc(ib.get('name'))}</b> ریست شد.")
        elif cmd == "/delete":
            def _del(db):
                db["inbounds"] = [x for x in db["inbounds"] if x.get("uid") != ib.get("uid")]
            await store.mutate(_del)
            _refresh_panel()
            await reply(f"🗑 کاربر <b>{esc(ib.get('name'))}</b> حذف شد.")
        return
    await reply("دستور ناشناخته. /help")


# ================================= poll loop ==================================
def token_fingerprint(token: str) -> str:
    return __import__("hashlib").sha256(token.encode()).hexdigest()[:12]


async def poll_loop(store, get_token_chat, interval: float = 2.5):
    """Long-poll getUpdates loop. get_token_chat() -> (token, admin_chat, enabled)."""
    offset = 0
    while True:
        try:
            token, admin_chat, enabled = get_token_chat()
            if not enabled or not token:
                await asyncio.sleep(10)
                continue
            async with httpx.AsyncClient(timeout=30) as c:
                try:
                    r = await c.post(_api_url(token, "getUpdates"),
                                     json={"offset": offset, "timeout": 20,
                                           "allowed_updates": ["message", "callback_query"]})
                    data = r.json()
                except Exception as e:
                    poll_state.update({"last_ts": time.time(), "ok": False, "error": f"net: {e}"[:200]})
                    await asyncio.sleep(5)
                    continue
            if not data.get("ok"):
                poll_state.update({"last_ts": time.time(), "ok": False,
                                   "error": str(data.get("description") or "api-error")[:200]})
                await asyncio.sleep(15)
                continue
            poll_state.update({"last_ts": time.time(), "ok": True, "error": ""})

            for upd in data.get("result", []):
                offset = max(offset, int(upd.get("update_id", 0)) + 1)

                cb = upd.get("callback_query")
                if cb:
                    try:
                        await handle_shop_callback(store, token, str(admin_chat or ""), cb)
                    except Exception as e:
                        log.warning("callback error: %s", e)
                    continue

                msg = upd.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                text = msg.get("text") or ""
                tg_user = msg.get("from") or {}
                is_admin = bool(admin_chat) and chat == str(admin_chat)

                if is_admin:
                    if text.startswith("/"):
                        await _handle_admin_command(store, token, chat, text)
                        continue
                    if await handle_shop_message(store, token, chat, tg_user, text, is_admin=True):
                        continue
                    await send_message(token, chat, "🏠 منوی اصلی",
                                       markup=main_menu_kb(is_admin=True))
                    continue

                # ---- customer (shop) mode ----
                try:
                    db0 = await store.get()
                    shop_on = bool((db0.get("settings") or {}).get("shop_enabled", True))
                except Exception:
                    shop_on = True

                if shop_on:
                    if text.startswith("/start"):
                        try:
                            cust = await get_customer(store, tg_user)
                            parts = text.split()
                            if len(parts) > 1 and parts[1].startswith("ref_"):
                                ref = parts[1][4:]
                                if ref.isdigit() and int(ref) != cust["tg_id"] and not cust.get("referred_by"):
                                    rid = int(ref)

                                    def _ref(db, _rid=rid, _tid=cust["tg_id"]):
                                        u = find_customer(db, _tid)
                                        if u and not u.get("referred_by") and find_customer(db, _rid):
                                            u["referred_by"] = _rid
                                            r2 = find_customer(db, _rid)
                                            if u["tg_id"] not in (r2.get("referrals") or []):
                                                r2.setdefault("referrals", []).append(u["tg_id"])
                                    await store.mutate(_ref)
                        except Exception as e:
                            log.warning("referral error: %s", e)
                        await send_message(token, chat,
                                           "👋 <b>به فروشگاه خوش آمدید!</b>\nاز منوی زیر شروع کنید:",
                                           markup=main_menu_kb(is_admin=False))
                        continue
                    try:
                        if await handle_shop_message(store, token, chat, tg_user, text, is_admin=False):
                            continue
                    except Exception as e:
                        log.warning("shop error: %s", e)
                if text.startswith("/"):
                    await send_message(token, chat,
                                       "👋 برای استفاده از فروشگاه /start را بزنید.",
                                       markup=main_menu_kb() if shop_on else None)
                else:
                    await send_message(token, chat, "🏠 منوی اصلی", markup=main_menu_kb())

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("telegram poll error: %s", e)
            await asyncio.sleep(5)
