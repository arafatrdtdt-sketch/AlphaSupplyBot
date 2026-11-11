import os
import html
import asyncio
import logging
from dotenv import load_dotenv
from aiohttp import web
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from supabase import create_client, Client

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
PORT = int(os.getenv("PORT", "8080"))
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@support")
CURRENCY = os.getenv("CURRENCY", "BDT")
PAYMENT_INFO = os.getenv("PAYMENT_INFO", "bKash: 01XXXXXXXXX (Personal)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("AlphaSupply")

if not (BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY):
    raise SystemExit("❌ Missing required env vars: BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------- helpers ----------------
async def sb(q):
    return await asyncio.to_thread(q.execute)


def is_admin(uid):
    return uid in ADMIN_IDS


async def get_user(uid):
    r = await sb(supabase.table("users").select("*").eq("id", uid).limit(1))
    return r.data[0] if r.data else None


async def ensure_user(tg_user):
    u = await get_user(tg_user.id)
    if u:
        await sb(supabase.table("users").update({
            "username": tg_user.username,
            "first_name": tg_user.first_name,
        }).eq("id", tg_user.id))
        return u
    r = await sb(supabase.table("users").insert({
        "id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "balance": 0,
        "is_admin": is_admin(tg_user.id),
    }))
    return r.data[0]


MENU_BUY = "🛒 Buy Items"
MENU_PROFILE = "👤 Profile"
MENU_BALANCE = "💰 Add Balance"
MENU_HISTORY = "📜 History"
MENU_SUPPORT = "🆘 Support"
MENU_ADMIN = "⚙️ Admin Panel"
MENU_BUTTONS = {MENU_BUY, MENU_PROFILE, MENU_BALANCE, MENU_HISTORY, MENU_SUPPORT, MENU_ADMIN}


def main_menu(admin: bool = False):
    kb = [
        [KeyboardButton(MENU_BUY), KeyboardButton(MENU_PROFILE)],
        [KeyboardButton(MENU_BALANCE), KeyboardButton(MENU_HISTORY)],
        [KeyboardButton(MENU_SUPPORT)],
    ]
    if admin:
        kb.append([KeyboardButton(MENU_ADMIN)])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


# ---------------- user handlers ----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)
    await update.message.reply_text(
        "👋 <b>Welcome to AlphaSupply!</b>\n\n"
        "Buy VPN, Proxy, Mail accounts with instant delivery.\n\n"
        "Use the menu below to get started.",
        parse_mode="HTML",
        reply_markup=main_menu(is_admin(update.effective_user.id)),
    )


async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "Cancelled.",
        reply_markup=main_menu(is_admin(update.effective_user.id)),
    )


async def show_categories(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = await sb(supabase.table("products").select("category").eq("is_active", True))
    cats = sorted({row["category"] for row in (r.data or [])})
    if not cats:
        await update.message.reply_text("❌ No products available right now.")
        return
    kb = [[InlineKeyboardButton(f"📦 {c.title()}", callback_data=f"cat:{c}")] for c in cats]
    await update.message.reply_text(
        "🛒 <b>Select a category:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cat_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cat = q.data.split(":", 1)[1]
    r = await sb(supabase.table("products").select("*").eq("category", cat).eq("is_active", True))
    products = r.data or []
    if not products:
        await q.edit_message_text(f"No products in {html.escape(cat)}.")
        return
    kb = []
    for p in products:
        emoji = "🟢" if p["stock"] > 0 else "🔴"
        kb.append([InlineKeyboardButton(
            f"{emoji} {p['name']} — {p['price']} {CURRENCY}",
            callback_data=f"prod:{p['id']}",
        )])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back_cats")])
    await q.edit_message_text(
        f"📦 <b>{html.escape(cat.title())}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def back_cats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    r = await sb(supabase.table("products").select("category").eq("is_active", True))
    cats = sorted({row["category"] for row in (r.data or [])})
    kb = [[InlineKeyboardButton(f"📦 {c.title()}", callback_data=f"cat:{c}")] for c in cats]
    await q.edit_message_text(
        "🛒 <b>Select a category:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def prod_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":", 1)[1])
    r = await sb(supabase.table("products").select("*").eq("id", pid).limit(1))
    if not r.data:
        await q.edit_message_text("Product not found.")
        return
    p = r.data[0]
    text = (
        f"<b>{html.escape(p['name'])}</b>\n\n"
        f"💰 Price: {p['price']} {CURRENCY}\n"
        f"📦 Stock: {p['stock']}\n"
        f"📝 {html.escape(p.get('description') or '—')}"
    )
    kb = []
    if p["stock"] > 0:
        kb.append([InlineKeyboardButton("✅ Buy Now", callback_data=f"buy:{pid}")])
    else:
        kb.append([InlineKeyboardButton("❌ Out of Stock", callback_data="noop")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data=f"cat:{p['category']}")])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))


async def buy_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":", 1)[1])
    uid = q.from_user.id
    try:
        r = await sb(supabase.rpc("purchase_product", {"p_user_id": uid, "p_product_id": pid}))
        result = r.data
    except Exception as e:
        log.exception("purchase failed")
        msg = str(e).lower()
        if "insufficient_balance" in msg:
            await q.edit_message_text("❌ <b>Insufficient balance.</b> Add balance first.", parse_mode="HTML")
        elif "out_of_stock" in msg:
            await q.edit_message_text("❌ <b>Out of stock.</b> Try another.", parse_mode="HTML")
        else:
            await q.edit_message_text("❌ Purchase failed. Try again later.")
        return
    await q.edit_message_text(
        f"✅ <b>Purchase successful!</b>\n\n"
        f"🎁 Your product:\n<code>{html.escape(str(result['content']))}</code>\n\n"
        f"💳 New balance: <b>{result['new_balance']} {CURRENCY}</b>\n"
        f"🧾 Order: #{result['order_id']}",
        parse_mode="HTML",
    )


async def noop_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Not available")


async def show_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    if not u:
        await update.message.reply_text("Send /start first.")
        return
    r = await sb(supabase.table("orders").select("id", count="exact").eq("user_id", uid))
    count = r.count or 0
    joined = (u.get("created_at") or "")[:10]
    uname = u.get("username")
    uname_text = f"@{html.escape(uname)}" if uname else "—"
    fname = html.escape(u.get("first_name") or "-")
    await update.message.reply_text(
        f"👤 <b>Profile</b>\n\n"
        f"🆔 <code>{uid}</code>\n"
        f"👤 {fname}\n"
        f"🔗 {uname_text}\n"
        f"💰 Balance: <b>{u['balance']} {CURRENCY}</b>\n"
        f"🧾 Orders: {count}\n"
        f"📅 Joined: {joined}",
        parse_mode="HTML",
    )


async def show_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    contact = html.escape(SUPPORT_CONTACT)
    await update.message.reply_text(
        f"🆘 <b>Support</b>\n\n"
        f"Contact: {contact}\n"
        f"We usually reply within a few hours.",
        parse_mode="HTML",
    )


async def show_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    r = await sb(
        supabase.table("transactions").select("*")
        .eq("user_id", uid).order("created_at", desc=True).limit(20)
    )
    txs = r.data or []
    if not txs:
        await update.message.reply_text("📜 No transactions yet.")
        return
    lines = ["📜 <b>Transaction History</b>\n"]
    for t in txs:
        sign = "+" if t["amount"] >= 0 else ""
        emoji = {"topup": "💰", "purchase": "🛒", "refund": "↩️", "admin_adjust": "⚙️"}.get(t["type"], "•")
        dt = (t.get("created_at") or "")[:16].replace("T", " ")
        lines.append(
            f"{emoji} <code>{sign}{t['amount']}</code> {CURRENCY} • {html.escape(t['type'])}\n"
            f"   <i>{dt}</i> • Bal: {t['balance_after']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def start_topup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["state"] = "topup_amount"
    info = html.escape(PAYMENT_INFO)
    await update.message.reply_text(
        f"💰 <b>Add Balance</b>\n\n"
        f"Send payment to:\n<code>{info}</code>\n\n"
        f"Then send the <b>amount</b> you paid:",
        parse_mode="HTML",
    )


async def topup_amount_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.strip())
        assert amt > 0
    except Exception:
        await update.message.reply_text("❌ Invalid amount. Send a positive number:")
        return
    ctx.user_data["topup_amount"] = amt
    ctx.user_data["state"] = "topup_txn"
    await update.message.reply_text(
        "Now send <b>Transaction ID (TrxID)</b>:",
        parse_mode="HTML",
    )


async def topup_txn_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txn = update.message.text.strip()
    amt = ctx.user_data.pop("topup_amount", None)
    ctx.user_data.pop("state", None)
    uid = update.effective_user.id

    if not amt:
        await update.message.reply_text(
            "Something went wrong. Try again.",
            reply_markup=main_menu(is_admin(uid)),
        )
        return

    await sb(supabase.table("topup_requests").insert({
        "user_id": uid, "amount": amt, "txn_id": txn, "status": "pending",
    }))

    await update.message.reply_text(
        f"✅ <b>Topup request submitted!</b>\n\n"
        f"Amount: {amt} {CURRENCY}\n"
        f"TrxID: <code>{html.escape(txn)}</code>\n\n"
        f"Admin will verify and credit shortly.",
        parse_mode="HTML",
        reply_markup=main_menu(is_admin(uid)),
    )

    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                aid,
                f"💳 <b>New Topup</b>\n"
                f"User: <code>{uid}</code>\n"
                f"Amount: {amt} {CURRENCY}\n"
                f"TrxID: <code>{html.escape(txn)}</code>\n\n"
                f"Go to ⚙️ Admin Panel → 💳 Pending Topups",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ---------------- admin ----------------
async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    kb = [
        [InlineKeyboardButton("➕ Add Product", callback_data="adm:addprod")],
        [InlineKeyboardButton("📦 Add Stock", callback_data="adm:addstock")],
        [InlineKeyboardButton("💳 Pending Topups", callback_data="adm:topups")],
        [InlineKeyboardButton("📊 Stats", callback_data="adm:stats")],
    ]
    await update.message.reply_text(
        "⚙️ <b>Admin Panel</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def adm_addprod_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["state"] = "adm_prod_name"
    await q.edit_message_text("Send product <b>name</b>:", parse_mode="HTML")


async def adm_addprod_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["p_name"] = update.message.text.strip()
    ctx.user_data["state"] = "adm_prod_cat"
    await update.message.reply_text(
        "Send <b>category</b> (e.g., vpn, proxy, mail):",
        parse_mode="HTML",
    )


async def adm_addprod_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["p_cat"] = update.message.text.strip().lower()
    ctx.user_data["state"] = "adm_prod_price"
    await update.message.reply_text("Send <b>price</b> (number):", parse_mode="HTML")


async def adm_addprod_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid price. Send a number:")
        return
    ctx.user_data["p_price"] = price
    ctx.user_data["state"] = "adm_prod_desc"
    await update.message.reply_text("Send <b>description</b> (or /skip):", parse_mode="HTML")


async def adm_addprod_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    desc = "" if update.message.text.startswith("/skip") else update.message.text.strip()
    data = {
        "name": ctx.user_data.pop("p_name"),
        "category": ctx.user_data.pop("p_cat"),
        "price": ctx.user_data.pop("p_price"),
        "description": desc, "stock": 0, "is_active": True,
    }
    ctx.user_data.pop("state", None)
    await sb(supabase.table("products").insert(data))
    await update.message.reply_text(
        f"✅ Product added: <b>{html.escape(data['name'])}</b>\n"
        f"Use 📦 Add Stock to add deliverable items.",
        parse_mode="HTML",
        reply_markup=main_menu(True),
    )


async def adm_addstock_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    r = await sb(supabase.table("products").select("id,name,stock").order("id"))
    products = r.data or []
    if not products:
        await q.edit_message_text("No products yet. Add a product first.")
        return
    kb = [[InlineKeyboardButton(f"{p['name']} (stock: {p['stock']})", callback_data=f"stk:{p['id']}")] for p in products]
    await q.edit_message_text("Select product to add stock:", reply_markup=InlineKeyboardMarkup(kb))


async def adm_addstock_prod(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":", 1)[1])
    ctx.user_data["stock_pid"] = pid
    ctx.user_data["state"] = "adm_stock_items"
    await q.edit_message_text(
        "Send stock items, <b>one per line</b>.\nEach line = one deliverable item.",
        parse_mode="HTML",
    )


async def adm_addstock_items(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pid = ctx.user_data.pop("stock_pid", None)
    ctx.user_data.pop("state", None)
    if not pid:
        await update.message.reply_text("Session lost. Try again.")
        return
    lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
    if not lines:
        await update.message.reply_text("No items received.", reply_markup=main_menu(True))
        return
    await sb(supabase.table("product_items").insert([{"product_id": pid, "content": l} for l in lines]))
    r = await sb(supabase.table("product_items").select("id", count="exact").eq("product_id", pid).eq("is_sold", False))
    new_stock = r.count or 0
    await sb(supabase.table("products").update({"stock": new_stock}).eq("id", pid))
    await update.message.reply_text(
        f"✅ Added <b>{len(lines)}</b> items. New stock: <b>{new_stock}</b>",
        parse_mode="HTML",
        reply_markup=main_menu(True),
    )


async def adm_topups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    r = await sb(supabase.table("topup_requests").select("*").eq("status", "pending").order("id"))
    reqs = r.data or []
    if not reqs:
        await q.edit_message_text("No pending topups.")
        return
    await q.edit_message_text(f"⏳ {len(reqs)} pending topup(s). Sending below...")
    for req in reqs:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"tup:ok:{req['id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"tup:no:{req['id']}"),
        ]])
        await ctx.bot.send_message(
            q.from_user.id,
            f"<b>Topup #{req['id']}</b>\n"
            f"User: <code>{req['user_id']}</code>\n"
            f"Amount: {req['amount']} {CURRENCY}\n"
            f"TrxID: <code>{html.escape(str(req['txn_id']))}</code>\n"
            f"Date: {(req.get('created_at') or '')[:16].replace('T', ' ')}",
            parse_mode="HTML",
            reply_markup=kb,
        )


async def adm_topup_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    parts = q.data.split(":")
    action, tid = parts[1], int(parts[2])
    if action == "ok":
        try:
            await sb(supabase.rpc("approve_topup", {"p_topup_id": tid}))
        except Exception as e:
            await q.edit_message_text(f"Error: {html.escape(str(e))}")
            return
        await q.edit_message_text("✅ Approved.")
        r = await sb(supabase.table("topup_requests").select("user_id,amount").eq("id", tid).limit(1))
        if r.data:
            u = r.data[0]
            try:
                await ctx.bot.send_message(
                    u["user_id"],
                    f"✅ Topup of {u['amount']} {CURRENCY} approved!",
                )
            except Exception:
                pass
    else:
        await sb(supabase.table("topup_requests").update({"status": "rejected"}).eq("id", tid))
        await q.edit_message_text("❌ Rejected.")


async def adm_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    u = await sb(supabase.table("users").select("id", count="exact"))
    p = await sb(supabase.table("products").select("id", count="exact").eq("is_active", True))
    o = await sb(supabase.table("orders").select("id", count="exact"))
    tp = await sb(supabase.table("topup_requests").select("id", count="exact").eq("status", "pending"))
    await q.edit_message_text(
        f"📊 <b>Stats</b>\n\n"
        f"👥 Users: {u.count or 0}\n"
        f"📦 Products: {p.count or 0}\n"
        f"🛒 Orders: {o.count or 0}\n"
        f"⏳ Pending Topups: {tp.count or 0}",
        parse_mode="HTML",
    )


# ---------------- router ----------------
async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    state = ctx.user_data.get("state")

    if text in MENU_BUTTONS:
        ctx.user_data.pop("state", None)
        if text == MENU_BUY:
            return await show_categories(update, ctx)
        if text == MENU_PROFILE:
            return await show_profile(update, ctx)
        if text == MENU_BALANCE:
            return await start_topup(update, ctx)
        if text == MENU_HISTORY:
            return await show_history(update, ctx)
        if text == MENU_SUPPORT:
            return await show_support(update, ctx)
        if text == MENU_ADMIN:
            return await admin_panel(update, ctx)
        return

    if state == "topup_amount":
        return await topup_amount_handler(update, ctx)
    if state == "topup_txn":
        return await topup_txn_handler(update, ctx)
    if state == "adm_prod_name":
        return await adm_addprod_name(update, ctx)
    if state == "adm_prod_cat":
        return await adm_addprod_cat(update, ctx)
    if state == "adm_prod_price":
        return await adm_addprod_price(update, ctx)
    if state == "adm_prod_desc":
        return await adm_addprod_desc(update, ctx)
    if state == "adm_stock_items":
        return await adm_addstock_items(update, ctx)

    await update.message.reply_text(
        "Unknown option. Use the menu.",
        reply_markup=main_menu(is_admin(update.effective_user.id)),
    )


# ---------------- healthcheck ----------------
async def health(request):
    return web.Response(text="OK")


async def start_health():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"✅ Healthcheck listening on :{PORT}")


# ---------------- main ----------------
async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    app.add_handler(CallbackQueryHandler(cat_callback, pattern="^cat:"))
    app.add_handler(CallbackQueryHandler(prod_callback, pattern="^prod:"))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy:"))
    app.add_handler(CallbackQueryHandler(back_cats, pattern="^back_cats$"))
    app.add_handler(CallbackQueryHandler(noop_cb, pattern="^noop$"))

    app.add_handler(CallbackQueryHandler(adm_addprod_start, pattern="^adm:addprod$"))
    app.add_handler(CallbackQueryHandler(adm_addstock_start, pattern="^adm:addstock$"))
    app.add_handler(CallbackQueryHandler(adm_addstock_prod, pattern="^stk:"))
    app.add_handler(CallbackQueryHandler(adm_topups, pattern="^adm:topups$"))
    app.add_handler(CallbackQueryHandler(adm_topup_action, pattern="^tup:"))
    app.add_handler(CallbackQueryHandler(adm_stats, pattern="^adm:stats$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    asyncio.create_task(start_health())
    log.info("🚀 Starting polling...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    try:
        await asyncio.Event().wait()   # run forever
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
