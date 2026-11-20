"""
Photo shop bot — sells photographers' bundles and premium-channel access
for Telegram Stars, keeps a per-photographer ledger for payouts.

Run:  BOT_TOKEN=123:abc python3 bot.py  [config.json]
Needs python-telegram-bot[job-queue] >= 22.0 (see requirements.txt).
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
import sys
import time

from telegram import (
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Kb,
    LabeledPrice,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from ledger import DAY, STARS_HOLD_DAYS, Ledger, allocate

log = logging.getLogger("photoshop")
CONFIG_PATH = sys.argv[1] if len(sys.argv) > 1 else "config.json"
SUB_PERIOD = 30 * DAY          # Telegram Stars subscriptions are always 30 days
INVITE_TTL = 2 * DAY           # personal invite links expire after this
ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


# ───────────────────────── config ─────────────────────────

class ConfigError(Exception):
    pass


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)

    phs = cfg.get("photographers") or {}
    if not phs:
        raise ConfigError("no photographers defined")
    for key in phs:
        if not ID_RE.match(key):
            raise ConfigError(f"photographer key {key!r}: use lowercase letters, digits, - or _")

    bundles = {}
    for b in cfg.get("bundles", []):
        bid = b.get("id", "")
        if not ID_RE.match(bid):
            raise ConfigError(f"bundle id {bid!r}: use lowercase letters, digits, - or _ (max 40)")
        if bid in bundles:
            raise ConfigError(f"duplicate bundle id {bid!r}")
        if b.get("photographer") not in phs:
            raise ConfigError(f"bundle {bid!r}: unknown photographer {b.get('photographer')!r}")
        if not isinstance(b.get("price_stars"), int) or b["price_stars"] < 1:
            raise ConfigError(f"bundle {bid!r}: price_stars must be a positive integer")
        if not str(b.get("url", "")).startswith("http"):
            raise ConfigError(f"bundle {bid!r}: url must be an http(s) link")
        b.setdefault("active", True)
        bundles[bid] = b
    cfg["bundles_by_id"] = bundles

    prem = cfg.get("premium")
    if prem:
        if prem.get("mode", "monthly") not in ("monthly", "lifetime"):
            raise ConfigError("premium.mode must be 'monthly' or 'lifetime'")
        prem.setdefault("mode", "monthly")
        if not isinstance(prem.get("price_stars"), int) or prem["price_stars"] < 1:
            raise ConfigError("premium.price_stars must be a positive integer")
        split = prem.setdefault("split", {k: 1 for k in phs})
        for k in split:
            if k not in phs:
                raise ConfigError(f"premium.split: unknown photographer {k!r}")
        if not isinstance(prem.get("channel_id"), int):
            raise ConfigError("premium.channel_id must be the numeric channel id (starts with -100)")

    cfg.setdefault("admins", [])
    cfg.setdefault("house_cut_percent", 0)
    cfg.setdefault("shop_name", "Photo Shop")
    cfg.setdefault("support_contact", "")
    cfg.setdefault("expiry_grace_hours", 24)
    return cfg


def cfg_of(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    return ctx.bot_data["cfg"]


def ledger_of(ctx: ContextTypes.DEFAULT_TYPE) -> Ledger:
    return ctx.bot_data["ledger"]


def is_admin(ctx, user_id: int) -> bool:
    return user_id in cfg_of(ctx)["admins"]


def photographer_key_for(ctx, user_id: int) -> str | None:
    for key, p in cfg_of(ctx)["photographers"].items():
        if p.get("telegram_id") == user_id:
            return key
    return None


def esc(s) -> str:
    return html.escape(str(s or ""))


def ts(value) -> int | None:
    """Telegram dates may arrive as datetime or int depending on library version."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return int(value.timestamp())
    return int(value)


def fmt_date(epoch: int | None) -> str:
    if epoch is None:
        return "never (lifetime)"
    return dt.datetime.fromtimestamp(epoch).strftime("%b %d, %Y")


async def notify(ctx, user_ids, text: str) -> None:
    for uid in set(filter(None, user_ids)):
        try:
            await ctx.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            log.warning("could not notify %s: %s", uid, e)


# ───────────────────────── customer screens ─────────────────────────

def home_kb(ctx) -> Kb:
    rows = [[Btn("📸 Browse bundles", callback_data="phs")]]
    if cfg_of(ctx).get("premium"):
        rows.append([Btn("⭐ Premium 4K channel", callback_data="prem")])
    rows.append([Btn("🧾 My purchases", callback_data="mine")])
    return Kb(rows)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = cfg_of(ctx)
    # deep links: t.me/yourbot?start=b_<bundle-id>  or  ?start=premium
    arg = ctx.args[0] if ctx.args else ""
    if arg.startswith("b_") and arg[2:] in cfg["bundles_by_id"]:
        text, kb = bundle_screen(ctx, update.effective_user.id, arg[2:])
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    if arg == "premium" and cfg.get("premium"):
        text, kb = await premium_screen(ctx, update.effective_user.id)
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        f"Welcome to <b>{esc(cfg['shop_name'])}</b>!\n"
        "Buy photo bundles straight from the photographers, or join the premium 4K channel. "
        "Payment is in Telegram Stars ⭐.",
        reply_markup=home_kb(ctx),
        parse_mode=ParseMode.HTML,
    )


def photographers_screen(ctx):
    cfg = cfg_of(ctx)
    rows = []
    for key, p in cfg["photographers"].items():
        n = sum(1 for b in cfg["bundles_by_id"].values() if b["photographer"] == key and b["active"])
        if n:
            rows.append([Btn(f"{p['name']} ({n})", callback_data=f"ph:{key}")])
    rows.append([Btn("« Back", callback_data="home")])
    text = "Pick a photographer:" if len(rows) > 1 else "No bundles for sale right now — check back soon."
    return text, Kb(rows)


def photographer_screen(ctx, key: str):
    cfg = cfg_of(ctx)
    p = cfg["photographers"][key]
    rows = [
        [Btn(f"{b['title']} — ⭐{b['price_stars']}", callback_data=f"b:{b['id']}")]
        for b in cfg["bundles_by_id"].values()
        if b["photographer"] == key and b["active"]
    ]
    rows.append([Btn("« Photographers", callback_data="phs")])
    text = f"<b>{esc(p['name'])}</b>"
    if p.get("bio"):
        text += f"\n{esc(p['bio'])}"
    return text, Kb(rows)


def bundle_screen(ctx, user_id: int, bid: str):
    cfg = cfg_of(ctx)
    b = cfg["bundles_by_id"][bid]
    p = cfg["photographers"][b["photographer"]]
    text = (
        f"<b>{esc(b['title'])}</b>\nby {esc(p['name'])}\n\n"
        f"{esc(b.get('description', ''))}\n\nPrice: ⭐{b['price_stars']}"
    )
    if ledger_of(ctx).owns_bundle(user_id, bid):
        rows = [[Btn("✅ You own this — open download", url=b["url"])]]
    elif b["active"]:
        rows = [[Btn(f"Buy for ⭐{b['price_stars']}", callback_data=f"buy:{bid}")]]
    else:
        rows = []
        text += "\n\n<i>No longer for sale.</i>"
    rows.append([Btn("« Back", callback_data=f"ph:{b['photographer']}")])
    return text, Kb(rows)


async def premium_screen(ctx, user_id: int):
    cfg = cfg_of(ctx)
    prem = cfg["premium"]
    led = ledger_of(ctx)
    per = "/month" if prem["mode"] == "monthly" else " one-time"
    text = f"<b>Premium 4K channel</b>\n\n{esc(prem.get('description', 'Full-resolution 4K downloads from all our photographers.'))}\n\nPrice: ⭐{prem['price_stars']}{per}"
    rows = []
    if led.is_premium_active(user_id):
        m = led.premium(user_id)
        text += f"\n\n✅ You're a member. Access until: {fmt_date(m['expires_at'])}"
        if not m["joined_at"]:
            rows.append([Btn("🔗 Get my invite link", callback_data="reinvite")])
    elif prem["mode"] == "monthly":
        link = await ctx.bot.create_invoice_link(
            title="Premium 4K channel",
            description=f"30 days of {cfg['shop_name']} premium. Renews monthly; cancel anytime in Telegram.",
            payload="premium",
            currency="XTR",
            prices=[LabeledPrice("Premium (30 days)", prem["price_stars"])],
            subscription_period=SUB_PERIOD,
        )
        rows.append([Btn(f"Subscribe — ⭐{prem['price_stars']}/month", url=link)])
    else:
        rows.append([Btn(f"Buy access — ⭐{prem['price_stars']}", callback_data="buyprem")])
    rows.append([Btn("« Back", callback_data="home")])
    return text, Kb(rows)


def mine_screen(ctx, user_id: int):
    cfg = cfg_of(ctx)
    led = ledger_of(ctx)
    rows = []
    for bid in led.owned_bundles(user_id):
        b = cfg["bundles_by_id"].get(bid)
        if b:
            rows.append([Btn(f"⬇️ {b['title']}", url=b["url"])])
    lines = ["<b>Your purchases</b>"]
    if not rows:
        lines.append("No bundles yet.")
    if cfg.get("premium"):
        m = led.premium(user_id)
        if led.is_premium_active(user_id):
            lines.append(f"Premium: active until {fmt_date(m['expires_at'])}")
            if not m["joined_at"]:
                rows.append([Btn("🔗 Get my premium invite link", callback_data="reinvite")])
        else:
            lines.append("Premium: not active")
    rows.append([Btn("« Back", callback_data="home")])
    return "\n".join(lines), Kb(rows)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id
    cfg = cfg_of(ctx)
    await q.answer()

    async def show(text, kb):
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
        except TelegramError:  # e.g. "message is not modified", or original was an invoice
            await q.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)

    if data == "home":
        await show(f"<b>{esc(cfg['shop_name'])}</b>", home_kb(ctx))
    elif data == "phs":
        await show(*photographers_screen(ctx))
    elif data.startswith("ph:") and data[3:] in cfg["photographers"]:
        await show(*photographer_screen(ctx, data[3:]))
    elif data.startswith("b:") and data[2:] in cfg["bundles_by_id"]:
        await show(*bundle_screen(ctx, uid, data[2:]))
    elif data.startswith("buy:") and data[4:] in cfg["bundles_by_id"]:
        await send_bundle_invoice(ctx, q.message.chat_id, data[4:])
    elif data == "prem" and cfg.get("premium"):
        await show(*await premium_screen(ctx, uid))
    elif data == "buyprem" and cfg.get("premium"):
        prem = cfg["premium"]
        await ctx.bot.send_invoice(
            chat_id=q.message.chat_id,
            title="Premium 4K channel",
            description=f"Lifetime access to {cfg['shop_name']} premium.",
            payload="premium",
            currency="XTR",
            prices=[LabeledPrice("Premium access", prem["price_stars"])],
        )
    elif data == "reinvite":
        await send_premium_invite(ctx, uid)
    elif data == "mine":
        await show(*mine_screen(ctx, uid))


async def send_bundle_invoice(ctx, chat_id: int, bid: str) -> None:
    cfg = cfg_of(ctx)
    b = cfg["bundles_by_id"][bid]
    p = cfg["photographers"][b["photographer"]]
    kwargs = {}
    if b.get("preview_url"):
        kwargs.update(photo_url=b["preview_url"], photo_width=1280, photo_height=853)
    await ctx.bot.send_invoice(
        chat_id=chat_id,
        title=b["title"][:32],
        description=(f"by {p['name']}. " + b.get("description", ""))[:255],
        payload=f"bundle:{bid}",
        currency="XTR",
        prices=[LabeledPrice(b["title"][:32], b["price_stars"])],
        **kwargs,
    )


# ───────────────────────── payments ─────────────────────────

async def on_precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram requires an answer within 10 seconds — keep this fast."""
    q = update.pre_checkout_query
    cfg = cfg_of(ctx)
    led = ledger_of(ctx)
    payload = q.invoice_payload

    if q.currency != "XTR":
        await q.answer(ok=False, error_message="Unsupported currency.")
        return

    if payload.startswith("bundle:"):
        b = cfg["bundles_by_id"].get(payload[7:])
        if not b or not b["active"]:
            await q.answer(ok=False, error_message="Sorry, this bundle is no longer for sale.")
        elif q.total_amount != b["price_stars"]:
            await q.answer(ok=False, error_message="The price changed — please open the bundle again.")
        elif led.owns_bundle(q.from_user.id, b["id"]):
            await q.answer(ok=False, error_message="You already own this bundle — see My purchases.")
        else:
            await q.answer(ok=True)
    elif payload == "premium" and cfg.get("premium"):
        prem = cfg["premium"]
        if prem["mode"] == "lifetime" and led.is_premium_active(q.from_user.id):
            await q.answer(ok=False, error_message="You already have premium access.")
        else:
            # monthly renewals keep the price the user subscribed at, so don't enforce here
            await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Unknown item.")


async def on_paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sp = update.message.successful_payment
    user = update.effective_user
    cfg = cfg_of(ctx)
    led = ledger_of(ctx)
    payload = sp.invoice_payload
    stars = sp.total_amount
    charge = sp.telegram_payment_charge_id
    who = f"@{user.username}" if user.username else esc(user.full_name)

    # 1) work out who earns what, 2) record it, 3) deliver. Recording comes first so a
    #    delivery hiccup can never lose a payment.
    if payload.startswith("bundle:"):
        bid = payload[7:]
        b = cfg["bundles_by_id"].get(bid)
        owners = {b["photographer"]: 1} if b else {"house": 1}
        item_type, item_id = "bundle", bid
    else:
        owners = dict(cfg["premium"]["split"]) if cfg.get("premium") else {"house": 1}
        # rotate who gets any leftover Star from uneven splits, so it evens out over time
        keys = list(owners)
        r = int(time.time()) % len(keys)
        owners = {k: owners[k] for k in keys[r:] + keys[:r]}
        item_type, item_id = "premium", "premium"
        b = None

    alloc = allocate(stars, owners, cfg["house_cut_percent"])
    is_new = led.record_purchase(
        charge_id=charge, user_id=user.id, username=user.username, item_type=item_type,
        item_id=item_id, stars=stars, allocations=alloc, is_recurring=bool(sp.is_recurring),
    )
    if not is_new:
        log.info("duplicate payment update %s ignored", charge)
        return

    if item_type == "bundle":
        if not b:
            await update.message.reply_text("Payment received, but that bundle was just removed. "
                                            "An admin has been notified and will sort it out or refund you.")
            await notify(ctx, cfg["admins"], f"⚠️ Paid for missing bundle <code>{esc(bid)}</code> "
                                             f"by {who}. Charge: <code>{esc(charge)}</code>")
            return
        await update.message.reply_text(
            f"Thanks! Here's <b>{esc(b['title'])}</b>. The link is also saved under 🧾 My purchases.",
            reply_markup=Kb([[Btn("⬇️ Download", url=b["url"])]]),
            parse_mode=ParseMode.HTML,
        )
        ph = cfg["photographers"][b["photographer"]]
        msg = f"💰 {who} bought <b>{esc(b['title'])}</b> for ⭐{stars}"
        await notify(ctx, [*cfg["admins"], ph.get("telegram_id")], msg)
        return

    # premium
    prev = led.premium(user.id)
    if cfg["premium"]["mode"] == "monthly":
        expires = ts(sp.subscription_expiration_date) or int(time.time()) + SUB_PERIOD
    else:
        expires = None
    led.grant_premium(user.id, expires)

    renewal = bool(sp.is_recurring) and not bool(getattr(sp, "is_first_recurring", False))
    already_in = prev is not None and prev["joined_at"] and prev["removed_at"] is None
    if renewal and already_in:
        await update.message.reply_text(f"⭐ Premium renewed — access until {fmt_date(expires)}.")
    else:
        await update.message.reply_text("Welcome to premium! 🎉")
        if not already_in:
            await send_premium_invite(ctx, user.id)
    kind = "renewed premium" if renewal else "joined premium"
    await notify(ctx, cfg["admins"], f"⭐ {who} {kind} (⭐{stars})")


async def send_premium_invite(ctx, user_id: int) -> None:
    """Personal join-request link: only the buyer gets approved, so a shared link is useless."""
    cfg = cfg_of(ctx)
    led = ledger_of(ctx)
    if not led.is_premium_active(user_id):
        await ctx.bot.send_message(user_id, "Your premium access isn't active.")
        return
    channel = cfg["premium"]["channel_id"]
    old = led.premium(user_id)["invite_link"]
    if old:
        try:
            await ctx.bot.revoke_chat_invite_link(channel, old)
        except TelegramError:
            pass
    try:
        link = await ctx.bot.create_chat_invite_link(
            chat_id=channel,
            name=f"buyer {user_id}"[:32],
            creates_join_request=True,
            expire_date=int(time.time()) + INVITE_TTL,
        )
    except TelegramError as e:
        log.error("invite link failed: %s", e)
        await ctx.bot.send_message(user_id, "Couldn't create your invite link — an admin has been notified.")
        await notify(ctx, cfg["admins"], f"⚠️ Invite link failed for {user_id}: {esc(e)}\n"
                                         "Is the bot an admin in the channel with 'Invite users' permission?")
        return
    led.set_invite(user_id, link.invite_link)
    await ctx.bot.send_message(
        user_id,
        "Tap below to join the premium channel. Your request is approved automatically. "
        "The link is personal and expires in 48h — you can get a fresh one from 🧾 My purchases.",
        reply_markup=Kb([[Btn("➡️ Join premium channel", url=link.invite_link)]]),
    )


async def on_join_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    cfg = cfg_of(ctx)
    if not cfg.get("premium") or req.chat.id != cfg["premium"]["channel_id"]:
        return
    led = ledger_of(ctx)
    uid = req.from_user.id
    if led.is_premium_active(uid):
        link = led.premium(uid)["invite_link"]
        await req.approve()
        led.mark_joined(uid)
        if link:
            try:
                await ctx.bot.revoke_chat_invite_link(req.chat.id, link)
            except TelegramError:
                pass
    else:
        await req.decline()
        try:
            me = await ctx.bot.get_me()
            await ctx.bot.send_message(
                req.user_chat_id,
                "This channel is for premium members.",
                reply_markup=Kb([[Btn("⭐ Get premium", url=f"https://t.me/{me.username}?start=premium")]]),
            )
        except TelegramError:
            pass


async def expire_members(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = cfg_of(ctx)
    if not cfg.get("premium"):
        return
    led = ledger_of(ctx)
    channel = cfg["premium"]["channel_id"]
    me = await ctx.bot.get_me()
    for uid in led.expired_members(grace_seconds=int(cfg["expiry_grace_hours"] * 3600)):
        await remove_from_channel(ctx, channel, uid)
        led.mark_removed(uid)
        try:
            await ctx.bot.send_message(
                uid, "Your premium access has ended. Thanks for being a member!",
                reply_markup=Kb([[Btn("⭐ Renew premium", url=f"https://t.me/{me.username}?start=premium")]]),
            )
        except TelegramError:
            pass


async def remove_from_channel(ctx, channel: int, uid: int) -> None:
    try:
        await ctx.bot.ban_chat_member(channel, uid)
        await ctx.bot.unban_chat_member(channel, uid, only_if_banned=True)  # kick, but allow rejoining later
    except TelegramError as e:
        log.warning("could not remove %s from channel: %s", uid, e)


# ───────────────────────── support / terms ─────────────────────────

async def cmd_paysupport(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    contact = cfg_of(ctx)["support_contact"]
    await update.message.reply_text(
        "Payment problem? Send us your issue"
        + (f" at {contact}" if contact else " here")
        + " with the date of purchase. Failed deliveries are refunded in Stars."
    )


async def cmd_terms(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        cfg_of(ctx).get("terms", "Digital downloads for personal use. Refunds only if a purchase can't be delivered.")
    )


# ───────────────────────── admin / photographer ─────────────────────────

def admin_only(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_admin(ctx, update.effective_user.id):
            return
        return await fn(update, ctx)
    return wrapper


def balance_table(ctx, keys) -> str:
    led = ledger_of(ctx)
    lines = [f"{'who':<10}{'earned':>8}{'held':>7}{'paid':>7}{'owed':>7}"]
    for k in keys:
        b = led.balance(k)
        lines.append(f"{k[:10]:<10}{b.earned:>8}{b.pending:>7}{b.paid:>7}{b.owed_now:>7}")
    return "<pre>" + esc("\n".join(lines)) + "</pre>"


def balance_keys(ctx) -> list[str]:
    keys = list(cfg_of(ctx)["photographers"])
    if cfg_of(ctx)["house_cut_percent"] or ledger_of(ctx).balance("house").earned:
        keys.append("house")
    return keys


@admin_only
async def cmd_balances(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    keys = balance_keys(ctx)
    await update.message.reply_text(
        "Balances in ⭐ Stars\n" + balance_table(ctx, keys)
        + f"\n<i>held</i> = still inside Telegram's {STARS_HOLD_DAYS}-day hold. "
          "<i>owed</i> = clear of the hold, minus payouts already recorded.\n"
          "Record a payout: <code>/payout key stars [note]</code>",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def cmd_payout(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    phs = cfg_of(ctx)["photographers"]
    try:
        key, stars = ctx.args[0], int(ctx.args[1])
        note = " ".join(ctx.args[2:])
        if key not in phs and key != "house":
            raise ValueError
        ledger_of(ctx).record_payout(key, stars, note, update.effective_user.id)
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /payout <photographer-key> <stars> [note]\n"
                                        f"Keys: {', '.join(phs)}")
        return
    b = ledger_of(ctx).balance(key)
    await update.message.reply_text(f"Recorded ⭐{stars} paid to {key}. Still owed: ⭐{b.owed_now}")
    if key in phs:
        await notify(ctx, [phs[key].get("telegram_id")], f"💸 Payout recorded: ⭐{stars}" + (f" — {esc(note)}" if note else ""))


@admin_only
async def cmd_sales(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    days = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 7
    rows = ledger_of(ctx).recent_sales(int(time.time()) - days * DAY)
    if not rows:
        await update.message.reply_text(f"No sales in the last {days} days.")
        return
    total = sum(r["stars"] for r in rows if r["refunded_at"] is None)
    lines = [f"Sales, last {days} days — ⭐{total} total"]
    for r in rows[:40]:
        when = dt.datetime.fromtimestamp(r["created_at"]).strftime("%m/%d %H:%M")
        flag = " (refunded)" if r["refunded_at"] else ""
        who = "@" + r["username"] if r["username"] else str(r["user_id"])
        lines.append(f"{when} {r['item_id']} ⭐{r['stars']} {who}{flag}\n  {r['charge_id']}")
    await update.message.reply_text("<pre>" + esc("\n".join(lines)) + "</pre>", parse_mode=ParseMode.HTML)


@admin_only
async def cmd_refund(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    led = ledger_of(ctx)
    cfg = cfg_of(ctx)
    if not ctx.args:
        await update.message.reply_text("Usage: /refund <charge_id>  (charge ids are listed in /sales)")
        return
    charge = ctx.args[0]
    p = led.get_purchase(charge)
    if not p:
        await update.message.reply_text("No purchase with that charge id.")
        return
    if p["refunded_at"]:
        await update.message.reply_text("Already refunded.")
        return
    try:
        await ctx.bot.refund_star_payment(user_id=p["user_id"], telegram_payment_charge_id=charge)
    except TelegramError as e:
        await update.message.reply_text(f"Telegram refused the refund: {e}")
        return
    led.mark_refunded(charge)
    if p["item_type"] == "premium" and cfg.get("premium"):
        if cfg["premium"]["mode"] == "monthly":
            try:  # stop future renewals too
                await ctx.bot.edit_user_star_subscription(p["user_id"], charge, is_canceled=True)
            except TelegramError:
                pass
        await remove_from_channel(ctx, cfg["premium"]["channel_id"], p["user_id"])
        led.mark_removed(p["user_id"])
    await update.message.reply_text(f"Refunded ⭐{p['stars']} for {p['item_id']}.")
    await notify(ctx, [p["user_id"]], f"↩️ You've been refunded ⭐{p['stars']}.")


@admin_only
async def cmd_reload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ctx.bot_data["cfg"] = load_config(CONFIG_PATH)
    except (ConfigError, ValueError, OSError) as e:
        await update.message.reply_text(f"Config NOT reloaded: {e}")
        return
    n = len(cfg_of(ctx)["bundles_by_id"])
    await update.message.reply_text(f"Config reloaded — {n} bundles.")


async def cmd_mysales(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    key = photographer_key_for(ctx, update.effective_user.id)
    if not key:
        return
    rows = ledger_of(ctx).recent_sales(int(time.time()) - 30 * DAY, photographer=key)
    lines = [f"Your earnings (⭐)\n{balance_table(ctx, [key])}"]
    live = [r for r in rows if r["refunded_at"] is None]
    lines.append(f"Last 30 days: {len(live)} sales, ⭐{sum(r['share'] for r in live)} your share")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your Telegram id: <code>{update.effective_user.id}</code>",
                                    parse_mode=ParseMode.HTML)


async def on_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """When the bot is added to a channel, tell admins the channel id (needed in config)."""
    m = update.my_chat_member
    if m.chat.type == "channel" and m.new_chat_member.status == "administrator":
        await notify(ctx, cfg_of(ctx)["admins"],
                     f"I'm now an admin in <b>{esc(m.chat.title)}</b>.\n"
                     f"channel_id: <code>{m.chat.id}</code>")


@admin_only
async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """DM a consistent snapshot of the ledger database to the admin."""
    import sqlite3
    import tempfile

    snap = os.path.join(tempfile.gettempdir(), f"shop-backup-{dt.date.today():%Y%m%d}.db")
    src = ledger_of(ctx).db
    dst = sqlite3.connect(snap)
    with dst:
        src.backup(dst)
    dst.close()
    with open(snap, "rb") as f:
        await update.message.reply_document(f, filename=os.path.basename(snap),
                                            caption="Ledger backup. Keep it somewhere safe.")
    os.remove(snap)


def resolve_db_path(cfg: dict) -> str:
    """DATABASE_PATH env > Railway volume > config 'database' > ./shop.db"""
    path = os.environ.get("DATABASE_PATH")
    if not path and os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        path = os.path.join(os.environ["RAILWAY_VOLUME_MOUNT_PATH"], "shop.db")
    if not path:
        path = cfg.get("database", "shop.db")
    if os.environ.get("RAILWAY_ENVIRONMENT") and not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") \
            and not os.environ.get("DATABASE_PATH"):
        log.warning("Running on Railway WITHOUT a volume — the ledger will be wiped on every redeploy!")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return path


# ───────────────────────── main ─────────────────────────

def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = load_config(CONFIG_PATH)
    token = os.environ.get("BOT_TOKEN") or cfg.get("bot_token")
    if not token:
        sys.exit("Set BOT_TOKEN (or bot_token in config.json)")

    app = Application.builder().token(token).build()
    app.bot_data["cfg"] = cfg
    app.bot_data["db_path"] = db_path = resolve_db_path(cfg)
    log.info("ledger database: %s", db_path)
    app.bot_data["ledger"] = Ledger(db_path)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("paysupport", cmd_paysupport))
    app.add_handler(CommandHandler("terms", cmd_terms))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("mysales", cmd_mysales))
    app.add_handler(CommandHandler("balances", cmd_balances))
    app.add_handler(CommandHandler("payout", cmd_payout))
    app.add_handler(CommandHandler("sales", cmd_sales))
    app.add_handler(CommandHandler("refund", cmd_refund))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(PreCheckoutQueryHandler(on_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_paid))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    app.job_queue.run_repeating(expire_members, interval=3600, first=30)

    log.info("starting %s", cfg["shop_name"])
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
