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
from telegram.error import RetryAfter
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


def btn(text, callback_data=None, url=None, style=None):
    b = {"text": str(text)}
    if callback_data:
        b["callback_data"] = callback_data
    if url:
        b["url"] = url
    if style:
        b["style"] = style
    return b


def fmt_price(val):
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "0.00"
    return f"{val:.2f}"


def extract_support_username() -> str:
    raw = (SUPPORT_CONTACT or "").strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.lstrip("@").strip()


def parse_proxy_item(content: str):
    content = (content or "").strip()
    if not content:
        return None
    parts = content.split(":")
    if len(parts) < 4:
        return None
    try:
        port_num = int(parts[1])
    except (ValueError, TypeError):
        return None
    if port_num < 1 or port_num > 65535:
        return None
    server = parts[0].strip()
    port = parts[1].strip()
    password = parts[-1].strip()
    username = ":".join(parts[2:-1]).strip()
    if not server or not username or not password:
        return None
    return server, port, username, password


def format_item_block(idx: int, content: str, total: int = 1) -> str:
    parsed = parse_proxy_item(content)
    proxy_label = "🏷 Proxy Details" if total == 1 else f"🏷 Proxy {idx} Details"
    item_label = "🎁 Item" if total == 1 else f"🎁 Item {idx}"
    if parsed:
        server, port, username, password = parsed
        return (
            f"{proxy_label}\n\n"
            f"Server : <code>{html.escape(server)}</code>\n\n"
            f"Port : <code>{html.escape(port)}</code>\n\n"
            f"Username : <code>{html.escape(username)}</code>\n\n"
            f"Password : <code>{html.escape(password)}</code>\n\n"
            f"⛔️One-line :\n"
            f"<code>{html.escape(content)}</code>"
        )
    return f"{item_label}\n\n<code>{html.escape(content)}</code>"


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


async def get_usernames_batch(uids):
    if not uids:
        return {}
    try:
        r = await sb(supabase.table("users").select("id,username,first_name").in_("id", list(uids)))
        data = r.data or []
        return {
            int(u["id"]): (u.get("username") or u.get("first_name") or f"User {u['id']}")
            for u in data
        }
    except Exception as e:
        log.warning(f"get_usernames_batch error: {e}")
        return {}


# -------- Menu labels --------
MENU_BUY = "🛒 Buy Items"
MENU_BALANCE = "💰 Add Balance"
MENU_PROFILE = "👤 Profile"
MENU_HISTORY = "📜 History"
MENU_SUPPORT = "🆘 Support"
MENU_ADMIN = "⚙️ Admin Panel"
MENU_BUTTONS = {MENU_BUY, MENU_BALANCE, MENU_PROFILE, MENU_HISTORY, MENU_SUPPORT, MENU_ADMIN}

ADM_PRODUCTS = "📦 Product"
ADM_BROADCAST = "📢 Broadcast"
ADM_TOPUPS = "💳 Pending Topups"
ADM_STATS = "📊 Stats"
ADM_BACK = "⬅️ Back to Main Menu"
ADMIN_BUTTONS = {ADM_PRODUCTS, ADM_BROADCAST, ADM_TOPUPS, ADM_STATS, ADM_BACK}


def main_menu(admin: bool = False):
    kb = [
        [{"text": MENU_BUY, "style": "success"}, {"text": MENU_BALANCE, "style": "success"}],
        [{"text": MENU_PROFILE, "style": "primary"}, {"text": MENU_HISTORY, "style": "primary"}],
        [{"text": MENU_SUPPORT, "style": "danger"}],
    ]
    if admin:
        kb.append([{"text": MENU_ADMIN, "style": "danger"}])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def admin_menu():
    kb = [
        [{"text": ADM_PRODUCTS, "style": "success"}, {"text": ADM_BROADCAST, "style": "primary"}],
        [{"text": ADM_TOPUPS, "style": "primary"}, {"text": ADM_STATS, "style": "primary"}],
        [{"text": ADM_BACK, "style": "danger"}],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


# ============================================================
# USER HANDLERS
# ============================================================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)
    ctx.user_data["in_admin"] = False
    await update.message.reply_text(
        "👋 <b>Welcome to AlphaSupply!</b>\n\n"
        "Buy VPN, Proxy, Mail accounts with instant delivery.\n\n"
        "Use the menu below to get started.",
        parse_mode="HTML",
        reply_markup=main_menu(is_admin(update.effective_user.id)),
    )


async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["in_admin"] = False
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
    kb = [[btn(f"📦 {c.title()}", callback_data=f"cat:{c}", style="primary")] for c in cats]
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
        in_stock = p["stock"] > 0
        emoji = "🟢" if in_stock else "🔴"
        kb.append([btn(
            f"{emoji} {p['name']} — {p['price']} {CURRENCY}",
            callback_data=f"prod:{p['id']}",
            style="success" if in_stock else "danger",
        )])
    kb.append([btn("⬅️ Back", callback_data="back_cats", style="danger")])
    await q.edit_message_text(
        f"📦 Please choose a <b>{html.escape(cat.title())}</b> to buy",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def back_cats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    r = await sb(supabase.table("products").select("category").eq("is_active", True))
    cats = sorted({row["category"] for row in (r.data or [])})
    kb = [[btn(f"📦 {c.title()}", callback_data=f"cat:{c}", style="primary")] for c in cats]
    await q.edit_message_text(
        "🛒 <b>Select a category:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ============================================================
# QUANTITY SELECTOR
# ============================================================
def build_quantity_view(product, qty):
    max_q = max(1, int(product["stock"]))
    if product["stock"] <= 0:
        qty = 1
    else:
        qty = max(1, min(qty, max_q))
    total = float(product["price"]) * qty

    text = (
        f"🛒 <b>{html.escape(product['name'])}</b>\n\n"
        f"💰 Price: {fmt_price(product['price'])} {CURRENCY}\n"
        f"📦 Stock: {product['stock']}\n"
        f"📝 {html.escape(product.get('description') or '—')}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔢 Quantity: <b>{qty}</b>\n"
        f"💵 Total: <b>{fmt_price(total)} {CURRENCY}</b>\n"
        f"━━━━━━━━━━━━━━━"
    )

    if product["stock"] <= 0:
        kb = [
            [btn("❌ Out of Stock", callback_data="noop", style="danger")],
            [btn("⬅️ Back", callback_data=f"cat:{product['category']}", style="danger")],
        ]
    else:
        kb = [
            [
                btn("-", callback_data=f"qty:dec:{product['id']}:{qty}", style="danger"),
                btn(f"🛒 {qty}", callback_data=f"qty:info:{product['id']}", style="success"),
                btn("+", callback_data=f"qty:inc:{product['id']}:{qty}", style="primary"),
            ],
            [btn("✏️ Enter Quantity", callback_data=f"qty:manual:{product['id']}", style="primary")],
            [btn("✅ Confirm & Buy", callback_data=f"qty:buy:{product['id']}:{qty}", style="success")],
            [btn("⬅️ Back", callback_data=f"cat:{product['category']}", style="danger")],
        ]
    return text, InlineKeyboardMarkup(kb)


async def render_quantity_view(q, product, qty):
    text, kb = build_quantity_view(product, qty)
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass


async def prod_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":", 1)[1])
    r = await sb(supabase.table("products").select("*").eq("id", pid).limit(1))
    if not r.data:
        await q.edit_message_text("Product not found.")
        return
    p = r.data[0]
    await render_quantity_view(q, p, 1)


async def qty_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    action = parts[1]
    pid = int(parts[2])

    if action == "info":
        await q.answer()
        return

    r = await sb(supabase.table("products").select("*").eq("id", pid).limit(1))
    if not r.data:
        await q.answer("Product not found.", show_alert=True)
        return
    p = r.data[0]

    if action == "inc":
        qty = int(parts[3]) + 1
        if qty > p["stock"]:
            await q.answer(f"Only {p['stock']} in stock!", show_alert=True)
            return
        await q.answer()
        await render_quantity_view(q, p, qty)

    elif action == "dec":
        qty = max(1, int(parts[3]) - 1)
        await q.answer()
        await render_quantity_view(q, p, qty)

    elif action == "manual":
        await q.answer()
        ctx.user_data["qty_manual_pid"] = pid
        ctx.user_data["state"] = "qty_manual"
        await q.edit_message_text(
            f"🔢 <b>Enter Quantity</b>\n\n"
            f"Product: <b>{html.escape(p['name'])}</b>\n"
            f"Price: {fmt_price(p['price'])} {CURRENCY}\n"
            f"Available Stock: {p['stock']}\n\n"
            f"Send a number between <b>1</b> and <b>{p['stock']}</b>:",
            parse_mode="HTML",
        )

    elif action == "buy":
        qty = int(parts[3])
        await do_purchase(q, ctx, p, qty)


async def qty_manual_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pid = ctx.user_data.pop("qty_manual_pid", None)
    ctx.user_data.pop("state", None)
    if not pid:
        await update.message.reply_text("Session expired. Use /start.")
        return
    txt = update.message.text.strip()
    try:
        qty = int(txt)
    except ValueError:
        await update.message.reply_text("❌ Invalid. Send a whole number:")
        ctx.user_data["qty_manual_pid"] = pid
        ctx.user_data["state"] = "qty_manual"
        return

    r = await sb(supabase.table("products").select("*").eq("id", pid).limit(1))
    if not r.data:
        await update.message.reply_text("Product not found.")
        return
    p = r.data[0]

    if qty < 1:
        qty = 1
    if qty > p["stock"]:
        await update.message.reply_text(
            f"❌ Only <b>{p['stock']}</b> in stock. Send a number between 1 and {p['stock']}:",
            parse_mode="HTML",
        )
        ctx.user_data["qty_manual_pid"] = pid
        ctx.user_data["state"] = "qty_manual"
        return

    text, kb = build_quantity_view(p, qty)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ============================================================
# PURCHASE EXECUTION
# ============================================================
def build_purchase_header(product, qty, total_cost, new_balance):
    header = (
        "✅ <b>Purchase Successful</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📦Product Name :- 🛍 <b>{html.escape(product['name'])}</b>\n"
    )
    if qty > 1:
        header += f"🔢Quantity :- <b>{qty}</b>\n"
    header += (
        f"💵Price :- {fmt_price(total_cost)} {CURRENCY}\n"
        f"💳Your Balance :- {fmt_price(new_balance)} {CURRENCY}\n"
    )
    return header


async def send_purchase_result(q, ctx, product, items, qty, total_cost, new_balance):
    header = build_purchase_header(product, qty, total_cost, new_balance)
    total_items = len(items)
    blocks = [format_item_block(i, c, total=total_items) for i, c in enumerate(items, 1)]

    full_parts = [header]
    for block in blocks:
        full_parts.append("━━━━━━━━━━━━━━━━━━━━━━\n" + block)
    full = "\n\n".join(full_parts)

    if len(full) <= 4000:
        try:
            await q.edit_message_text(full, parse_mode="HTML")
            return
        except Exception:
            pass

    try:
        await q.edit_message_text(header, parse_mode="HTML")
    except Exception:
        try:
            await ctx.bot.send_message(q.from_user.id, header, parse_mode="HTML")
        except Exception:
            pass

    current = ""
    for block in blocks:
        seg = "━━━━━━━━━━━━━━━━━━━━━━\n" + block + "\n\n"
        if len(current) + len(seg) > 3800:
            if current.strip():
                try:
                    await ctx.bot.send_message(q.from_user.id, current.rstrip(), parse_mode="HTML")
                except Exception:
                    pass
            current = seg
        else:
            current += seg
    if current.strip():
        try:
            await ctx.bot.send_message(q.from_user.id, current.rstrip(), parse_mode="HTML")
        except Exception:
            pass


async def do_purchase(q, ctx, product, qty):
    uid = q.from_user.id
    try:
        r = await sb(supabase.rpc("purchase_product_batch", {
            "p_user_id": uid,
            "p_product_id": product["id"],
            "p_quantity": qty,
        }))
        result = r.data
    except Exception as e:
        log.exception("purchase failed")
        msg = str(e).lower()
        if "insufficient_balance" in msg:
            need = float(product["price"]) * qty
            await q.answer(
                f"❌ Insufficient balance. Need {fmt_price(need)} {CURRENCY}.",
                show_alert=True,
            )
            await render_quantity_view(q, product, qty)
        elif "out_of_stock" in msg:
            await q.answer("❌ Not enough stock. Reduce quantity.", show_alert=True)
            await render_quantity_view(q, product, qty)
        else:
            await q.answer("❌ Purchase failed. Try again.", show_alert=True)
        return

    await q.answer("✅ Purchase successful!")

    items = result.get("items", []) or []
    if isinstance(items, str):
        import json as _json
        try:
            items = _json.loads(items)
        except Exception:
            items = [items]
    new_balance = result.get("new_balance", 0)
    total_cost = result.get("total_cost", 0)

    await send_purchase_result(q, ctx, product, items, qty, total_cost, new_balance)


async def noop_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Not available")


# ============================================================
# PROFILE / HISTORY / SUPPORT / TOPUP
# ============================================================
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
    username = extract_support_username()
    if not username:
        await update.message.reply_text("🆘 Support contact is not configured yet.")
        return
    kb = InlineKeyboardMarkup([[
        btn("💬 Contact Support", url=f"https://t.me/{username}", style="success"),
    ]])
    await update.message.reply_text(
        "🆘 <b>Support</b>\n\nClick the button below to contact our support team:",
        parse_mode="HTML",
        reply_markup=kb,
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


# ============================================================
# ADMIN — PRODUCT MANAGEMENT
# ============================================================
async def build_products_view():
    r = await sb(supabase.table("products").select("*").order("id"))
    products = r.data or []
    if not products:
        kb = [
            [btn("➕ Add New Product", callback_data="adm:newprod", style="success")],
            [btn("⬅️ Back", callback_data="adm:back", style="danger")],
        ]
        return "📦 <b>No products yet.</b>\n\nAdd your first product:", InlineKeyboardMarkup(kb)

    kb = []
    for p in products:
        emoji = "🟢" if p.get("is_active") else "🔴"
        stock = p.get("stock", 0)
        kb.append([btn(
            f"{emoji} {p['name']} • {p['price']} {CURRENCY} • stock: {stock}",
            callback_data=f"adm:prod:{p['id']}",
            style="primary",
        )])
    kb.append([btn("➕ Add New Product", callback_data="adm:newprod", style="success")])
    kb.append([btn("⬅️ Back", callback_data="adm:back", style="danger")])
    return f"📦 <b>All Products</b> ({len(products)})\n\nClick a product to manage:", InlineKeyboardMarkup(kb)


async def show_admin_product_detail(target, pid: int, is_callback: bool = True):
    r = await sb(supabase.table("products").select("*").eq("id", pid).limit(1))
    if not r.data:
        if is_callback:
            await target.edit_message_text("Product not found.")
        else:
            await target.reply_text("Product not found.")
        return
    p = r.data[0]

    unsold_r = await sb(supabase.table("product_items").select("id", count="exact")
                        .eq("product_id", pid).eq("is_sold", False))
    total_r = await sb(supabase.table("product_items").select("id", count="exact")
                       .eq("product_id", pid))
    unsold = unsold_r.count or 0
    total = total_r.count or 0
    sold = total - unsold

    text = (
        f"📦 <b>{html.escape(p['name'])}</b>\n\n"
        f"🏷️ Category: {html.escape(p['category'])}\n"
        f"💰 Price: {fmt_price(p['price'])} {CURRENCY}\n"
        f"📝 {html.escape(p.get('description') or '—')}\n\n"
        f"📊 <b>Stock Info:</b>\n"
        f"🟢 Available: <b>{unsold}</b>\n"
        f"🔴 Sold: {sold}\n"
        f"📦 Total: {total}\n"
        f"Status: {'✅ Active' if p.get('is_active') else '❌ Inactive'}"
    )

    if p.get("is_active"):
        kb = [
            [btn("➕ Add Stock", callback_data=f"adm:stock:{pid}", style="success"),
             btn("🗑️ Clear Unsold", callback_data=f"adm:clrstock:{pid}", style="danger")],
            [btn("✏️ Edit Price", callback_data=f"adm:eprice:{pid}", style="primary"),
             btn("🗑️ Delete Product", callback_data=f"adm:delconf:{pid}", style="danger")],
            [btn("⬅️ Back to Products", callback_data="adm:prods", style="primary")],
        ]
    else:
        kb = [
            [btn("✅ Reactivate Product", callback_data=f"adm:reactivate:{pid}", style="success")],
            [btn("🗑️ Delete Permanently", callback_data=f"adm:delconf:{pid}", style="danger")],
            [btn("⬅️ Back to Products", callback_data="adm:prods", style="primary")],
        ]
    markup = InlineKeyboardMarkup(kb)
    if is_callback:
        await target.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await target.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ============================================================
# ADMIN — PENDING TOPUPS (paginated list + detail + reject flow)
# ============================================================
TOPUPS_PER_PAGE = 10


async def build_topups_list_view(page: int = 1):
    r = await sb(supabase.table("topup_requests").select("*").eq("status", "pending").order("id"))
    reqs = r.data or []

    if not reqs:
        kb = [[btn("⬅️ Back to Admin Panel", callback_data="adm:back", style="danger")]]
        return (
            "💳 <b>Pending Topups</b>\n\n✅ No pending topups right now.",
            InlineKeyboardMarkup(kb),
        )

    total = len(reqs)
    total_pages = max(1, (total + TOPUPS_PER_PAGE - 1) // TOPUPS_PER_PAGE)
    page = max(1, min(page, total_pages))

    start = (page - 1) * TOPUPS_PER_PAGE
    page_reqs = reqs[start:start + TOPUPS_PER_PAGE]

    uids = list({int(t["user_id"]) for t in page_reqs})
    uname_map = await get_usernames_batch(uids)

    kb = []
    for t in page_reqs:
        uname = uname_map.get(int(t["user_id"])) or f"User {t['user_id']}"
        uname = uname.lstrip("@")
        if len(uname) > 16:
            uname = uname[:14] + "…"
        amount_str = fmt_price(t["amount"])
        kb.append([btn(
            f"👤 @{uname} • {amount_str} {CURRENCY}",
            callback_data=f"tup:view:{t['id']}",
            style="primary",
        )])

    nav = []
    if page > 1:
        nav.append(btn("⬅️ Prev", callback_data=f"tup:page:{page-1}", style="primary"))
    if total_pages > 1:
        nav.append(btn(f"{page}/{total_pages}", callback_data="noop", style="primary"))
    if page < total_pages:
        nav.append(btn("Next ➡️", callback_data=f"tup:page:{page+1}", style="primary"))
    if nav:
        kb.append(nav)

    kb.append([btn("⬅️ Back to Admin Panel", callback_data="adm:back", style="danger")])

    text = (
        f"💳 <b>Pending Topups</b>\n\n"
        f"📊 Total: <b>{total}</b>  •  Page {page}/{total_pages}\n\n"
        f"Click a topup to open:"
    )
    return text, InlineKeyboardMarkup(kb)


async def build_topup_detail_view(tid: int, page_hint: int = 1):
    r = await sb(supabase.table("topup_requests").select("*").eq("id", tid).limit(1))
    if not r.data:
        return "Topup not found.", InlineKeyboardMarkup([[btn("⬅️ Back", callback_data=f"tup:page:{page_hint}", style="primary")]])

    t = r.data[0]
    status = (t.get("status") or "").lower()

    if status != "pending":
        uname_map = await get_usernames_batch([int(t["user_id"])])
        uname = (uname_map.get(int(t["user_id"])) or f"User {t['user_id']}").lstrip("@")
        reason = t.get("reject_reason") or ""
        text = (
            f"💳 <b>Topup #{t['id']}</b>\n\n"
            f"👤 User: @{html.escape(uname)}\n"
            f"💰 Amount: {fmt_price(t['amount'])} {CURRENCY}\n"
            f"📌 Status: <b>{status.upper()}</b>\n"
        )
        if reason:
            text += f"📝 Reason: <i>{html.escape(reason)}</i>\n"
        kb = [[btn("⬅️ Back to List", callback_data=f"tup:page:{page_hint}", style="primary")]]
        return text, InlineKeyboardMarkup(kb)

    uname_map = await get_usernames_batch([int(t["user_id"])])
    uname = (uname_map.get(int(t["user_id"])) or f"User {t['user_id']}").lstrip("@")

    text = (
        f"💳 <b>Topup #{t['id']}</b>\n\n"
        f"👤 User: <a href=\"tg://user?id={t['user_id']}\">@{html.escape(uname)}</a>\n"
        f"🆔 ID: <code>{t['user_id']}</code>\n"
        f"💰 Amount: <b>{fmt_price(t['amount'])} {CURRENCY}</b>\n"
        f"📝 TrxID: <code>{html.escape(str(t.get('txn_id') or '—'))}</code>\n"
        f"📅 Date: {(t.get('created_at') or '')[:16].replace('T', ' ')}\n\n"
        f"Choose an action:"
    )
    kb = [
        [
            btn("✅ Approve", callback_data=f"tup:ok:{tid}", style="success"),
            btn("❌ Reject", callback_data=f"tup:rejstart:{tid}", style="danger"),
        ],
        [btn("⬅️ Back to List", callback_data=f"tup:page:{page_hint}", style="primary")],
    ]
    return text, InlineKeyboardMarkup(kb)


async def _do_reject_topup(ctx, tid: int, reason: str):
    reason = (reason or "No reason provided").strip()
    try:
        await sb(supabase.table("topup_requests").update({
            "status": "rejected", "reject_reason": reason,
        }).eq("id", tid))
    except Exception:
        try:
            await sb(supabase.table("topup_requests").update({
                "status": "rejected",
            }).eq("id", tid))
        except Exception as e:
            log.error(f"reject topup error: {e}")
            return False

    r = await sb(supabase.table("topup_requests").select("user_id,amount").eq("id", tid).limit(1))
    if r.data:
        u = r.data[0]
        try:
            await ctx.bot.send_message(
                u["user_id"],
                f"❌ <b>Topup Rejected</b>\n\n"
                f"Amount: {fmt_price(u['amount'])} {CURRENCY}\n"
                f"Reason: <i>{html.escape(reason)}</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return True


async def adm_btn_topups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text, kbd = await build_topups_list_view(1)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kbd)


async def adm_topup_reject_reason_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.user_data.pop("reject_topup_id", None)
    ctx.user_data.pop("state", None)
    if not tid:
        await update.message.reply_text("Session expired. Try again.")
        return
    reason = update.message.text.strip() or "No reason provided"
    await _do_reject_topup(ctx, int(tid), reason)
    text, kbd = await build_topups_list_view(1)
    await update.message.reply_text(
        f"❌ Topup #{tid} rejected.\n\n{text}",
        parse_mode="HTML", reply_markup=kbd,
    )


async def adm_topup_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    data = q.data

    # Cancel reject flow
    if data == "tup:rejcancel":
        tid = ctx.user_data.pop("reject_topup_id", None)
        ctx.user_data.pop("state", None)
        if tid:
            text, kb = await build_topup_detail_view(int(tid))
        else:
            text, kb = await build_topups_list_view(1)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        return

    parts = data.split(":")
    if len(parts) < 3:
        return
    action = parts[1]

    if action == "page":
        page = int(parts[2])
        text, kb = await build_topups_list_view(page)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    elif action == "view":
        tid = int(parts[2])
        text, kb = await build_topup_detail_view(tid)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    elif action == "ok":
        tid = int(parts[2])
        try:
            await sb(supabase.rpc("approve_topup", {"p_topup_id": tid}))
        except Exception as e:
            await q.answer(f"Error: {str(e)[:100]}", show_alert=True)
            return
        r = await sb(supabase.table("topup_requests").select("user_id,amount").eq("id", tid).limit(1))
        if r.data:
            u = r.data[0]
            try:
                await ctx.bot.send_message(
                    u["user_id"],
                    f"✅ Topup of {fmt_price(u['amount'])} {CURRENCY} approved!",
                )
            except Exception:
                pass
        await q.answer("✅ Approved!", show_alert=False)
        text, kb = await build_topups_list_view(1)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    elif action == "rejstart":
        tid = int(parts[2])
        ctx.user_data["reject_topup_id"] = tid
        ctx.user_data["state"] = "adm_topup_reject"
        kb = [
            [btn("⏭️ Skip (Reject Without Reason)", callback_data=f"tup:rejskip:{tid}", style="danger")],
            [btn("❌ Cancel", callback_data="tup:rejcancel", style="primary")],
        ]
        try:
            await q.edit_message_text(
                f"❌ <b>Reject Topup #{tid}</b>\n\n"
                f"Send the rejection reason as a text message.\n\n"
                f"Or tap <b>Skip</b> to reject without a reason.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        except Exception:
            pass

    elif action == "rejskip":
        tid = int(parts[2])
        ctx.user_data.pop("reject_topup_id", None)
        ctx.user_data.pop("state", None)
        await _do_reject_topup(ctx, tid, "No reason provided")
        await q.answer("❌ Rejected.", show_alert=False)
        text, kb = await build_topups_list_view(1)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


# ============================================================
# ADMIN — text-menu entry points
# ============================================================
async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ctx.user_data["in_admin"] = True
    await update.message.reply_text(
        "⚙️ <b>Admin Panel</b>\n\nChoose an option from the menu below:",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


async def adm_btn_products(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kbd = await build_products_view()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kbd)


async def adm_btn_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["state"] = "adm_broadcast"
    await update.message.reply_text(
        "📢 <b>Broadcast</b>\n\nSend the message you want to broadcast to all users.\n\n"
        "It supports text, photos, videos, forwards — anything.\n\n/cancel to abort.",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


async def adm_btn_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    u = await sb(supabase.table("users").select("id", count="exact"))
    p = await sb(supabase.table("products").select("id", count="exact").eq("is_active", True))
    o = await sb(supabase.table("orders").select("id", count="exact"))
    tp = await sb(supabase.table("topup_requests").select("id", count="exact").eq("status", "pending"))
    await update.message.reply_text(
        f"📊 <b>Stats</b>\n\n"
        f"👥 Users: {u.count or 0}\n"
        f"📦 Products: {p.count or 0}\n"
        f"🛒 Orders: {o.count or 0}\n"
        f"⏳ Pending Topups: {tp.count or 0}",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


# ============================================================
# ADMIN — add-product state flow
# ============================================================
async def adm_addprod_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["p_name"] = update.message.text.strip()
    ctx.user_data["state"] = "adm_prod_cat"
    await update.message.reply_text(
        "Step 2/4 — Send <b>category</b> (e.g., vpn, proxy, mail):",
        parse_mode="HTML",
    )


async def adm_addprod_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["p_cat"] = update.message.text.strip().lower()
    ctx.user_data["state"] = "adm_prod_price"
    await update.message.reply_text("Step 3/4 — Send <b>price</b> (number):", parse_mode="HTML")


async def adm_addprod_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid price. Send a number:")
        return
    ctx.user_data["p_price"] = price
    ctx.user_data["state"] = "adm_prod_desc"
    await update.message.reply_text("Step 4/4 — Send <b>description</b> (or /skip):", parse_mode="HTML")


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
        f"✅ Product added: <b>{html.escape(data['name'])}</b>",
        parse_mode="HTML",
    )
    text, kbd = await build_products_view()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kbd)


# ============================================================
# ADMIN — add-stock state flow
# ============================================================
async def adm_addstock_items(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pid = ctx.user_data.pop("stock_pid", None)
    ctx.user_data.pop("state", None)
    if not pid:
        await update.message.reply_text("Session lost. Try again.", reply_markup=admin_menu())
        return
    lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
    if not lines:
        await update.message.reply_text("No items received.", reply_markup=admin_menu())
        return
    await sb(supabase.table("product_items").insert([{"product_id": pid, "content": l} for l in lines]))
    r = await sb(supabase.table("product_items").select("id", count="exact")
                 .eq("product_id", pid).eq("is_sold", False))
    new_stock = r.count or 0
    await sb(supabase.table("products").update({"stock": new_stock}).eq("id", pid))
    await update.message.reply_text(
        f"✅ Added <b>{len(lines)}</b> items. New stock: <b>{new_stock}</b>",
        parse_mode="HTML",
    )
    await show_admin_product_detail(update.message, pid, is_callback=False)


# ============================================================
# ADMIN — edit-price state flow
# ============================================================
async def adm_edit_price_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pid = ctx.user_data.pop("edit_price_pid", None)
    ctx.user_data.pop("state", None)
    if not pid:
        await update.message.reply_text("Session expired. Try again.", reply_markup=admin_menu())
        return
    try:
        new_price = float(update.message.text.strip())
        if new_price < 0:
            new_price = 0.0
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Send a number:")
        ctx.user_data["edit_price_pid"] = pid
        ctx.user_data["state"] = "adm_edit_price"
        return
    await sb(supabase.table("products").update({"price": new_price}).eq("id", pid))
    await update.message.reply_text(
        f"✅ Price updated to <b>{fmt_price(new_price)} {CURRENCY}</b>",
        parse_mode="HTML",
    )
    await show_admin_product_detail(update.message, pid, is_callback=False)


# ============================================================
# ADMIN — broadcast
# ============================================================
async def adm_broadcast_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("state", None)
    r = await sb(supabase.table("users").select("id"))
    users = r.data or []
    if not users:
        await update.message.reply_text("No users to broadcast to.", reply_markup=admin_menu())
        return

    status = await update.message.reply_text(f"📤 Broadcasting to {len(users)} users...")

    from_chat = update.message.chat_id
    msg_id = update.message.message_id
    sem = asyncio.Semaphore(20)
    counters = {"sent": 0, "failed": 0}

    async def send_to(uid: int):
        async with sem:
            for _attempt in range(3):
                try:
                    await ctx.bot.copy_message(
                        chat_id=uid, from_chat_id=from_chat, message_id=msg_id,
                    )
                    counters["sent"] += 1
                    break
                except RetryAfter as e:
                    wait = e.retry_after
                    if hasattr(wait, "total_seconds"):
                        wait = wait.total_seconds()
                    await asyncio.sleep(float(wait) + 1)
                except Exception:
                    counters["failed"] += 1
                    break
            else:
                counters["failed"] += 1
            await asyncio.sleep(0.05)

    await asyncio.gather(*[send_to(u["id"]) for u in users], return_exceptions=True)

    report = (
        f"✅ <b>Broadcast complete</b>\n\n"
        f"✅ Sent: {counters['sent']}\n"
        f"❌ Failed: {counters['failed']}\n"
        f"📊 Total: {len(users)}"
    )
    try:
        await status.edit_text(report, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(report, parse_mode="HTML")
    await update.message.reply_text("Admin Menu:", reply_markup=admin_menu())


# ============================================================
# ADMIN — inline callbacks (adm:*)
# ============================================================
async def adm_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Admin only.", show_alert=True)
        return
    data = q.data

    if data == "adm:prods":
        await q.answer()
        text, kbd = await build_products_view()
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kbd)

    elif data == "adm:back":
        await q.answer()
        await q.message.reply_text(
            "⚙️ <b>Admin Panel</b>\n\nChoose an option from the menu below:",
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )
        try:
            await q.delete_message()
        except Exception:
            pass

    elif data == "adm:newprod":
        await q.answer()
        ctx.user_data["state"] = "adm_prod_name"
        await q.edit_message_text(
            "➕ <b>New Product</b>\n\nStep 1/4 — Send product <b>name</b>:",
            parse_mode="HTML",
        )

    elif data.startswith("adm:prod:"):
        await q.answer()
        pid = int(data.split(":")[2])
        await show_admin_product_detail(q, pid, is_callback=True)

    elif data.startswith("adm:stock:"):
        await q.answer()
        pid = int(data.split(":")[2])
        ctx.user_data["stock_pid"] = pid
        ctx.user_data["state"] = "adm_stock_items"
        await q.edit_message_text(
            "📦 <b>Add Stock</b>\n\nSend items, <b>one per line</b>.\nEach line becomes one deliverable.",
            parse_mode="HTML",
        )

    elif data.startswith("adm:clrstock:"):
        await q.answer()
        pid = int(data.split(":")[2])
        try:
            await sb(supabase.table("product_items").delete().eq("product_id", pid).eq("is_sold", False))
            await sb(supabase.table("products").update({"stock": 0}).eq("id", pid))
        except Exception as e:
            await q.answer(f"Error: {str(e)[:100]}", show_alert=True)
            return
        await q.answer("Unsold stock cleared!", show_alert=True)
        await show_admin_product_detail(q, pid, is_callback=True)

    elif data.startswith("adm:eprice:"):
        await q.answer()
        pid = int(data.split(":")[2])
        r = await sb(supabase.table("products").select("name,price").eq("id", pid).limit(1))
        if not r.data:
            await q.edit_message_text("Product not found.")
            return
        p = r.data[0]
        ctx.user_data["edit_price_pid"] = pid
        ctx.user_data["state"] = "adm_edit_price"
        await q.edit_message_text(
            f"✏️ <b>Edit Price</b>\n\n"
            f"Product: <b>{html.escape(p['name'])}</b>\n"
            f"Current price: {fmt_price(p['price'])} {CURRENCY}\n\n"
            f"Send the new price (number):",
            parse_mode="HTML",
        )

    elif data.startswith("adm:reactivate:"):
        await q.answer()
        pid = int(data.split(":")[2])
        await sb(supabase.table("products").update({"is_active": True}).eq("id", pid))
        await q.answer("Product reactivated!", show_alert=True)
        await show_admin_product_detail(q, pid, is_callback=True)

    elif data.startswith("adm:delconf:"):
        await q.answer()
        pid = int(data.split(":")[2])
        r = await sb(supabase.table("products").select("*").eq("id", pid).limit(1))
        if not r.data:
            await q.edit_message_text("Product not found.")
            return
        p = r.data[0]
        kb = [[
            btn("✅ Yes, Delete", callback_data=f"adm:delyes:{pid}", style="danger"),
            btn("❌ Cancel", callback_data=f"adm:prod:{pid}", style="primary"),
        ]]
        await q.edit_message_text(
            f"⚠️ <b>Delete Product?</b>\n\n"
            f"<b>{html.escape(p['name'])}</b>\n\n"
            f"If this product has order history, it will be <b>deactivated</b> instead.\n"
            f"Otherwise it will be permanently removed.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("adm:delyes:"):
        await q.answer("Processing...")
        pid = int(data.split(":")[2])
        try:
            order_check = await sb(supabase.table("orders").select("id", count="exact").eq("product_id", pid))
            has_orders = (order_check.count or 0) > 0

            if has_orders:
                await sb(supabase.table("products").update({"is_active": False}).eq("id", pid))
                note = "✅ Product <b>deactivated</b> (has order history)."
            else:
                await sb(supabase.table("product_items").delete().eq("product_id", pid))
                await sb(supabase.table("products").delete().eq("id", pid))
                note = "✅ Product <b>deleted permanently</b>."
        except Exception as e:
            await q.edit_message_text(f"❌ Error: {html.escape(str(e))}")
            return
        text, kbd = await build_products_view()
        try:
            await q.edit_message_text(f"{note}\n\n{text}", parse_mode="HTML", reply_markup=kbd)
        except Exception:
            await ctx.bot.send_message(q.from_user.id, text, parse_mode="HTML", reply_markup=kbd)

    else:
        await q.answer()


# ============================================================
# ROUTER
# ============================================================
async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    state = ctx.user_data.get("state")
    user_is_admin = is_admin(update.effective_user.id)

    if text in ADMIN_BUTTONS and user_is_admin:
        ctx.user_data.pop("state", None)
        if text == ADM_PRODUCTS:
            return await adm_btn_products(update, ctx)
        if text == ADM_BROADCAST:
            return await adm_btn_broadcast(update, ctx)
        if text == ADM_TOPUPS:
            return await adm_btn_topups(update, ctx)
        if text == ADM_STATS:
            return await adm_btn_stats(update, ctx)
        if text == ADM_BACK:
            ctx.user_data["in_admin"] = False
            await update.message.reply_text(
                "⬅️ Returned to Main Menu.",
                reply_markup=main_menu(user_is_admin),
            )
            return
        return

    if text in MENU_BUTTONS:
        ctx.user_data.pop("state", None)
        if text == MENU_ADMIN:
            return await admin_panel(update, ctx)
        ctx.user_data["in_admin"] = False
        if text == MENU_BUY:
            return await show_categories(update, ctx)
        if text == MENU_BALANCE:
            return await start_topup(update, ctx)
        if text == MENU_PROFILE:
            return await show_profile(update, ctx)
        if text == MENU_HISTORY:
            return await show_history(update, ctx)
        if text == MENU_SUPPORT:
            return await show_support(update, ctx)
        return

    if state == "topup_amount":
        return await topup_amount_handler(update, ctx)
    if state == "topup_txn":
        return await topup_txn_handler(update, ctx)
    if state == "qty_manual":
        return await qty_manual_handler(update, ctx)
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
    if state == "adm_edit_price":
        return await adm_edit_price_handler(update, ctx)
    if state == "adm_broadcast":
        return await adm_broadcast_handler(update, ctx)
    if state == "adm_topup_reject":
        return await adm_topup_reject_reason_handler(update, ctx)

    kb = admin_menu() if (user_is_admin and ctx.user_data.get("in_admin")) else main_menu(user_is_admin)
    await update.message.reply_text("Unknown option. Use the menu.", reply_markup=kb)


# ============================================================
# HEALTHCHECK / MAIN
# ============================================================
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


async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    app.add_handler(CallbackQueryHandler(cat_callback, pattern="^cat:"))
    app.add_handler(CallbackQueryHandler(prod_callback, pattern="^prod:"))
    app.add_handler(CallbackQueryHandler(qty_callback, pattern="^qty:"))
    app.add_handler(CallbackQueryHandler(back_cats, pattern="^back_cats$"))
    app.add_handler(CallbackQueryHandler(noop_cb, pattern="^noop$"))

    app.add_handler(CallbackQueryHandler(adm_topup_action, pattern="^tup:"))
    app.add_handler(CallbackQueryHandler(adm_callback, pattern="^adm:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    asyncio.create_task(start_health())
    log.info("🚀 Starting polling...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
