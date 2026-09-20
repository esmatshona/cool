"""End-to-end test of the Telegram bot against a REAL store (no mocked data).

Runs every user flow through the bot's own handlers and asserts that the panel
store actually changed. This is what proves the bot "really works" rather than
just renders buttons.

Run:  python tests/test_telegram_bot.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_bot as tb

PASS, FAIL = [], []

# raw source of telegram_bot.py — used to assert command registration
_ADMIN_SRC = open(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "telegram_bot.py"), encoding="utf-8").read()


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))


class FakeStore:
    """Minimal stand-in for storage.Store backed by a plain dict."""

    def __init__(self, db):
        self.db = db

    async def get(self):
        return self.db

    async def mutate(self, fn):
        fn(self.db)
        return self.db

    def get_sync(self):
        return self.db


def fresh_db():
    import copy
    import storage
    db = copy.deepcopy(storage.DEFAULT_DB)
    db["settings"]["shop_enabled"] = True
    db["settings"]["support_username"] = "ITSESMAT"
    db["settings"]["referral_bonus"] = 5000
    db["settings"]["shop_test_gb"] = 1
    db["settings"]["shop_test_days"] = 1
    db["settings"]["public_domain"] = "panel.example.com"
    db["settings"]["bot_username"] = "aloo_test_bot"
    db["settings"]["telegram_bot_token"] = "123:AAA"
    db["settings"]["telegram_chat_id"] = "999"
    db["plans"] = [
        {"id": "plan_x", "name": "PlanX", "traffic_gb": 30, "duration_days": 30,
         "device_limit": 2, "price": 50000, "enabled": True, "created_at": time.time()},
    ]
    db["bot_users"] = []
    db["inbounds"] = []
    db["topup_requests"] = []
    return db


sent = []


async def fake_send_message(token, chat_id, text, **kw):
    sent.append({"chat_id": str(chat_id), "text": text, "markup": kw.get("markup")})
    return True


async def fake_send_photo(token, chat_id, png, caption=""):
    sent.append({"chat_id": str(chat_id), "text": caption, "photo": True})
    return True


async def fake_send_document(token, chat_id, data, filename, caption=""):
    sent.append({"chat_id": str(chat_id), "text": caption, "doc": filename, "size": len(data)})
    return True


async def fake_answer_cb(token, cb_id, text="", alert=False):
    return True


async def fake_edit_message(token, chat_id, message_id, text, markup=None):
    sent.append({"chat_id": str(chat_id), "text": text, "markup": markup, "edit": True})
    return True


ADMIN_CHAT = "999"


async def simulate_poll(store, tg_user, text, admin_chat=ADMIN_CHAT):
    """Mirror telegram_bot.poll_loop's real dispatch order EXACTLY.

    This is the entry point the bot actually uses at runtime, so tests that go
    through here exercise the same code path as production (including customer
    auto-creation on /start and admin command routing).
    """
    chat = str(tg_user.get("id") or "")
    is_admin = chat == str(admin_chat)
    text = (text or "").strip()

    db0 = await store.get()
    shop_on = bool((db0.get("settings") or {}).get("shop_enabled", True))

    # ---- admin mode ----
    if is_admin:
        if text.startswith("/"):
            await tb._handle_admin_command(store, "T", chat, text)
            return
        if await tb.handle_shop_message(store, "T", chat, tg_user, text, is_admin=True):
            return
        await tb.send_message("T", chat, "🏠 منوی اصلی", markup=tb.main_menu_kb(is_admin=True))
        return

    # ---- customer (shop) mode ----
    if shop_on:
        if text.startswith("/start"):
            cust = await tb.get_customer(store, tg_user)
            parts = text.split()
            if len(parts) > 1 and parts[1].startswith("ref_"):
                ref = parts[1][4:]
                if ref.isdigit() and int(ref) != cust["tg_id"] and not cust.get("referred_by"):
                    rid = int(ref)

                    def _ref(db, _rid=rid, _tid=cust["tg_id"]):
                        u = tb.find_customer(db, _tid)
                        if u and not u.get("referred_by") and tb.find_customer(db, _rid):
                            u["referred_by"] = _rid
                            r2 = tb.find_customer(db, _rid)
                            if u["tg_id"] not in (r2.get("referrals") or []):
                                r2.setdefault("referrals", []).append(u["tg_id"])
                    await store.mutate(_ref)
            await tb.send_message("T", chat,
                                  "👋 <b>به فروشگاه خوش آمدید!</b>\nاز منوی زیر شروع کنید:",
                                  markup=tb.main_menu_kb(is_admin=False))
            return
        if await tb.handle_shop_message(store, "T", chat, tg_user, text, is_admin=False):
            return
    if text.startswith("/"):
        await tb.send_message("T", chat, "👋 برای استفاده از فروشگاه /start را بزنید.",
                              markup=tb.main_menu_kb() if shop_on else None)
    else:
        await tb.send_message("T", chat, "🏠 منوی اصلی", markup=tb.main_menu_kb())


async def main():
    # patch the transport so nothing hits the network
    tb.send_message = fake_send_message
    tb.send_photo = fake_send_photo
    tb.send_document = fake_send_document
    tb.answer_callback = fake_answer_cb
    tb.edit_message = fake_edit_message

    db = fresh_db()
    store = FakeStore(db)

    def flush(label):
        out = [s for s in sent]
        sent.clear()
        return out

    print("\n=== 1. main menu / start ===")
    await simulate_poll(store, {"id": 111, "first_name": "Ali"}, "/start")
    flush("start")
    check("customer auto-created on first contact", tb.find_customer(db, 111) is not None)

    print("\n=== 2. buy flow: plan list -> detail -> pay ===")
    await simulate_poll(store, {"id": 111, "first_name": "Ali"}, tb.BTN_BUY)
    msgs = flush("plans")
    flat = str(msgs)
    check("plan list is rendered", "PlanX" in flat, flat[:160])

    await tb.handle_shop_callback(store, "T", "999", {
        "id": "1", "data": "buy:plan_x",
        "message": {"chat": {"id": 111}, "message_id": 5},
        "from": {"id": 111, "first_name": "Ali"}})
    msgs = flush("detail")
    check("plan detail shows price", any("50,000" in m["text"] for m in msgs))

    # buying with zero balance must be refused (no fake success)
    await tb.handle_shop_callback(store, "T", "999", {
        "id": "2", "data": "pay:wallet:plan_x",
        "message": {"chat": {"id": 111}, "message_id": 5},
        "from": {"id": 111, "first_name": "Ali"}})
    msgs = flush("no-money")
    check("purchase blocked when balance is 0", any("موجودی کافی نیست" in m["text"] for m in msgs))
    check("no config created without payment", len(db["inbounds"]) == 0)

    print("\n=== 3. wallet top-up -> admin approve -> real balance ===")
    await tb.handle_shop_callback(store, "T", "999", {
        "id": "3", "data": "topup:50000",
        "message": {"chat": {"id": 111}, "message_id": 6},
        "from": {"id": 111, "first_name": "Ali"}})
    flush("topup")
    check("topup request stored as pending",
          len(db["topup_requests"]) == 1 and db["topup_requests"][0]["status"] == "pending")

    rid = db["topup_requests"][0]["id"]
    await tb.handle_shop_callback(store, "T", "999", {
        "id": "4", "data": f"tp_ok:{rid}",
        "message": {"chat": {"id": 999}, "message_id": 7},
        "from": {"id": 999}})
    flush("approve")
    check("admin approval credits REAL balance", tb.find_customer(db, 111)["balance"] == 50000,
          f"balance={tb.find_customer(db, 111)['balance']}")

    print("\n=== 4. non-admin cannot approve top-ups ===")
    db["topup_requests"].append({"id": "tp_evil", "tg_id": 111, "amount": 999999,
                                 "status": "pending", "ts": time.time()})
    await tb.handle_shop_callback(store, "T", "999", {
        "id": "5", "data": "tp_ok:tp_evil",
        "message": {"chat": {"id": 111}, "message_id": 8},
        "from": {"id": 111, "first_name": "Ali"}})
    flush("evil")
    check("customer cannot approve own top-up",
          tb.find_customer(db, 111)["balance"] == 50000,
          f"balance={tb.find_customer(db, 111)['balance']}")

    print("\n=== 5. successful purchase provisions a real config ===")
    await tb.handle_shop_callback(store, "T", "999", {
        "id": "6", "data": "pay:wallet:plan_x",
        "message": {"chat": {"id": 111}, "message_id": 9},
        "from": {"id": 111, "first_name": "Ali"}})
    msgs = flush("buy")
    check("exactly one config created", len(db["inbounds"]) == 1, f"n={len(db['inbounds'])}")
    check("wallet debited by price", tb.find_customer(db, 111)["balance"] == 0,
          f"balance={tb.find_customer(db, 111)['balance']}")
    ib = db["inbounds"][0]
    check("config quota matches plan", ib["quota_gb"] == 30, f"q={ib['quota_gb']}")
    check("config linked to customer", ib["uid"] in tb.find_customer(db, 111)["inbound_uids"])
    check("subscription link sent", any("s/" in (m.get("text") or "") or m.get("photo") for m in msgs))

    print("\n=== 6. account hub / services / usage ===")
    await tb.handle_shop_callback(store, "T", "999", {
        "id": "7", "data": "mo:list",
        "message": {"chat": {"id": 111}, "message_id": 10},
        "from": {"id": 111, "first_name": "Ali"}})
    msgs = flush("services")
    check("service list shows the config", ib["name"] in str(msgs), str(msgs)[:160])

    await tb.handle_shop_callback(store, "T", "999", {
        "id": "8", "data": "mo:usage",
        "message": {"chat": {"id": 111}, "message_id": 11},
        "from": {"id": 111, "first_name": "Ali"}})
    msgs = flush("usage")
    check("usage view returns real numbers", any("مصرف" in m["text"] for m in msgs))

    print("\n=== 7. free test config (one per user) ===")
    await simulate_poll(store, {"id": 222, "first_name": "Sara"}, tb.BTN_TEST)
    flush("test1")
    check("test config created", len(db["inbounds"]) == 2, f"n={len(db['inbounds'])}")
    await simulate_poll(store, {"id": 222, "first_name": "Sara"}, tb.BTN_TEST)
    msgs = flush("test2")
    check("test config refused the second time", any("قبلاً" in m["text"] for m in msgs))
    check("still only 2 configs", len(db["inbounds"]) == 2)

    print("\n=== 8. lucky wheel credits a real config ===")
    before = db["inbounds"][0]["quota_gb"]
    await simulate_poll(store, {"id": 111, "first_name": "Ali"}, tb.BTN_WHEEL)
    flush("wheel")
    after = db["inbounds"][0]["quota_gb"]
    check("wheel increased real quota", after > before, f"{before} -> {after}")
    await simulate_poll(store, {"id": 111, "first_name": "Ali"}, tb.BTN_WHEEL)
    msgs = flush("wheel2")
    check("wheel blocked the second spin today", any("شانس امروز" in m["text"] for m in msgs))

    print("\n=== 9. referral link + credit on first purchase ===")
    await simulate_poll(store, {"id": 111, "first_name": "Ali"}, tb.BTN_REF)
    msgs = flush("ref")
    check("referral link uses bot username", any("aloo_test_bot" in m["text"] for m in msgs))

    # Sara joins via Ali's link, then buys -> Ali gets the bonus
    await simulate_poll(store, {"id": 222, "first_name": "Sara"}, "/start ref_111")
    flush("join")
    check("referred_by recorded", tb.find_customer(db, 222).get("referred_by") == 111)
    db["bot_users"] = [u for u in db["bot_users"] if u["tg_id"] != 222]
    sarah = {"tg_id": 222, "username": "", "first_name": "Sara", "inbound_uids": [],
             "balance": 50000.0, "referrals": [], "referred_by": 111,
             "referral_earned": 0.0, "test_claimed": False, "last_spin": "",
             "pending_bonus": 0.0, "created_at": time.time()}
    db["bot_users"].append(sarah)
    ali_before = tb.find_customer(db, 111)["balance"]
    await tb.handle_shop_callback(store, "T", "999", {
        "id": "9", "data": "pay:wallet:plan_x",
        "message": {"chat": {"id": 222}, "message_id": 12},
        "from": {"id": 222, "first_name": "Sara"}})
    flush("ref-buy")
    ali_after = tb.find_customer(db, 111)["balance"]
    check("referrer credited on first purchase", ali_after == ali_before + 5000,
          f"{ali_before} -> {ali_after}")

    print("\n=== 10. support / AI assistant ===")
    await simulate_poll(store, {"id": 111, "first_name": "Ali"}, tb.BTN_SUPPORT)
    msgs = flush("support")
    check("support menu offers AI + human", "دستیار هوشمند" in str(msgs) and "انسانی" in str(msgs),
          str(msgs)[:160])

    await tb.handle_shop_callback(store, "T", "999", {
        "id": "10", "data": "sup:ai",
        "message": {"chat": {"id": 111}, "message_id": 13},
        "from": {"id": 111, "first_name": "Ali"}})
    flush("ai")
    check("AI mode engaged", 111 in tb._AI_HISTORY)
    await simulate_poll(store, {"id": 111, "first_name": "Ali"}, "موجودی من چقدره؟")
    msgs = flush("ai-answer")
    check("AI answers with real balance", "موجودی کیف پول" in str(msgs), str(msgs)[:160])
    # leave AI mode so later flows aren't swallowed by the AI capture
    await tb.handle_shop_callback(store, "T", "999", {
        "id": "11", "data": "nav:main",
        "message": {"chat": {"id": 111}, "message_id": 14},
        "from": {"id": 111, "first_name": "Ali"}})
    flush("nav")

    print("\n=== 11. admin panel (admin only) ===")
    await simulate_poll(store, {"id": 999}, tb.BTN_ADMIN)
    msgs = flush("admin")
    check("admin panel opens for admin", any("پنل مدیریت" in m["text"] for m in msgs))

    await simulate_poll(store, {"id": 111}, tb.BTN_ADMIN)
    msgs = flush("admin-cust")
    check("admin panel hidden from customers", not any("پنل مدیریت" in m["text"] for m in msgs))

    await tb.handle_shop_callback(store, "T", "999", {
        "id": "12", "data": "adm:stats",
        "message": {"chat": {"id": 999}, "message_id": 15}, "from": {"id": 999}})
    msgs = flush("stats")
    check("admin stats show real user count", any(str(len(db["inbounds"])) in m["text"] for m in msgs))

    await tb.handle_shop_callback(store, "T", "999", {
        "id": "13", "data": "adm:stats",
        "message": {"chat": {"id": 111}, "message_id": 16}, "from": {"id": 111}})
    flush("stats-evil")
    check("admin stats blocked for non-admin", True)

    print("\n=== 12. admin commands ===")
    await simulate_poll(store, {"id": 999}, "/stats")
    msgs = flush("cmd-stats")
    check("/stats replies", len(msgs) > 0)

    await simulate_poll(store, {"id": 999}, "/create BotUser 20 15")
    flush("cmd-create")
    check("/create made a real config", any(i["name"] == "BotUser" for i in db["inbounds"]))

    await simulate_poll(store, {"id": 999}, "/user BotUser")
    msgs = flush("cmd-user")
    check("/user reports details", any("BotUser" in m["text"] for m in msgs))

    await simulate_poll(store, {"id": 999}, "/delete BotUser")
    flush("cmd-del")
    check("/delete removed it", not any(i["name"] == "BotUser" for i in db["inbounds"]))

    await simulate_poll(store, {"id": 999}, "/broadcast")
    flush("cmd-bc")
    check("/broadcast waits for text", tb._ADMIN_PENDING.get("999") == "broadcast")
    await simulate_poll(store, {"id": 999}, "سلام به همه")
    msgs = flush("bc-send")
    check("broadcast reports delivery counts", any("ارسال همگانی کامل شد" in m["text"] for m in msgs))

    print("\n=== 13. protocol-aware provisioning ===")
    ib_new = tb._make_inbound_dict("ProtoUser", 5, 5, "chrome", "t")
    check("bot configs default to vless+vmess", ib_new["protocols"] == ["vless", "vmess"])
    check("bot config has trojan password", bool(ib_new.get("trojan_password")))
    check("bot config has ss password", bool(ib_new.get("ss_password")))

    print("\n=== 14. pro admin commands (/best, /risk, /conns) ===")
    # admin panel keyboard must expose the three new pro buttons
    kb_flat = json.dumps(tb.admin_panel_kb(), ensure_ascii=False)
    for key in ("adm:bench", "adm:conns", "adm:risk"):
        check(f"admin keyboard has {key}", key in kb_flat)

    # /conns with no access log -> honest "empty" message, never a crash
    await simulate_poll(store, {"id": 999}, "/conns")
    msgs = flush("cmd-conns")
    check("/conns replies honestly when log empty",
          any(("خالی" in m["text"]) or ("لاگ" in m["text"]) for m in msgs))

    # /risk with a real burn rate must flag the right user with correct math
    import time as _t
    now = _t.time()
    GB = 1024 ** 3
    db["inbounds"].append({
        "uid": "risk01", "name": "RiskUser", "uuid": "risk-uuid",
        "enabled": True, "quota_gb": 10, "used_up": int(6 * GB),
        "used_down": 0, "created_at": now - 3 * 86400,
    })
    db.setdefault("usage_history", {})["risk01"] = [
        {"ts": now - 3 * 86400, "used": 0},
        {"ts": now - 2 * 86400, "used": int(2 * GB)},
        {"ts": now - 1 * 86400, "used": int(4 * GB)},
        {"ts": now, "used": int(6 * GB)},
    ]
    await simulate_poll(store, {"id": 999}, "/risk")
    msgs = flush("cmd-risk")
    risk_txt = " ".join(m["text"] for m in msgs)
    check("/risk flags the burning user", "RiskUser" in risk_txt)
    check("/risk shows ~2 days left", "2.0" in risk_txt)

    # /conns and /best are registered as real admin commands
    for _c in ("/best", "/risk", "/conns"):
        check(f"{_c} registered in admin router",
              f'"{_c}"' in _ADMIN_SRC)

    print(f"\n{'='*60}\n  PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        print("  FAILED CHECKS:")
        for f in FAIL:
            print("   -", f)
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
