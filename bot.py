import logging
import os
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from time import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# NEW IMPORTS FOR RAZORPAY
from flask import Flask, request, jsonify
import hmac
import hashlib
import threading
import asyncio

# ─────────────────────────────────────────────
#  LOAD ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────

load_dotenv()

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError(
        "❌ BOT_TOKEN not set!\n"
        "Create a .env file with: BOT_TOKEN=your_token_here\n"
        "Or set environment variable: export BOT_TOKEN=your_token_here"
    )

ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

TIER_CHANNELS = {
    "bronze": -1004446165809,
    "gold":   -1004487193283,
}

TIER_PRICES = {
    "bronze": 100,
    "gold":   250,
}

TIER_DURATION_DAYS = {
    "bronze": 60,
    "gold":   60,
}

TIER_LABELS = {
    "bronze": "🥉 Bronze — 100 ⭐ / 2 months",
    "gold":   "🥇 Gold   — 250 ⭐ / 2 months",
}

CHANNEL_TO_TIER = {v: k for k, v in TIER_CHANNELS.items()}

# Razorpay configuration
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_BRONZE_PAGE = os.environ.get("RAZORPAY_BRONZE_PAGE", "")
RAZORPAY_GOLD_PAGE = os.environ.get("RAZORPAY_GOLD_PAGE", "")
WEBHOOK_PORT = int(os.environ.get("PORT", 8080))

RAZORPAY_PRICES = {
    "bronze": 8000,
    "gold": 20000,
}

RAZORPAY_PAYMENT_PAGES = {
    "bronze": RAZORPAY_BRONZE_PAGE,
    "gold": RAZORPAY_GOLD_PAGE,
}

USER_COOLDOWNS = {}
COOLDOWN_SECONDS = 3

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  FLASK WEBHOOK SERVER
# ─────────────────────────────────────────────

webhook_app = Flask(__name__)
bot_app = None

def verify_razorpay_signature(payload, signature, secret):
    """Verify webhook authenticity using HMAC SHA256."""
    if not secret:
        logger.warning("No webhook secret configured!")
        return False
    
    expected_signature = hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_signature, signature)

@webhook_app.route('/webhook/razorpay', methods=['POST'])
def razorpay_webhook():
    """Handle Razorpay webhook events."""
    try:
        payload = request.get_data()
        signature = request.headers.get('X-Razorpay-Signature', '')
        
        logger.info("Received Razorpay webhook")
        
        if RAZORPAY_WEBHOOK_SECRET and not verify_razorpay_signature(payload, signature, RAZORPAY_WEBHOOK_SECRET):
            logger.warning("Invalid Razorpay webhook signature")
            return jsonify({"error": "Invalid signature"}), 401
        
        event = request.get_json()
        event_type = event.get('event')
        
        logger.info(f"Webhook event: {event_type}")
        
        if event_type == 'payment.captured':
            payment = event['payload']['payment']['entity']
            
            payment_id = payment['id']
            amount = payment['amount']
            
            notes = payment.get('notes', {})
            user_id = notes.get('user_id')
            tier = notes.get('tier')
            
            logger.info(f"Payment captured: {payment_id}, user: {user_id}, tier: {tier}, amount: {amount}")
            
            if not user_id or not tier:
                logger.error(f"Missing user_id or tier in payment {payment_id}")
                return jsonify({"error": "Missing metadata"}), 400
            
            user_id = int(user_id)
            
            expected_amount = RAZORPAY_PRICES.get(tier)
            if amount != expected_amount:
                logger.error(f"Amount mismatch: expected {expected_amount}, got {amount}")
                return jsonify({"error": "Amount mismatch"}), 400
            
            if charge_id_already_used(payment_id):
                logger.warning(f"Duplicate payment {payment_id}")
                return jsonify({"status": "already_processed"}), 200
            
            log_payment(user_id, tier, payment_id, amount // 100, "razorpay_captured")
            
            existing = get_active_subscription(user_id, tier)
            if existing:
                old_expiry = datetime.fromisoformat(existing[1])
                base_date = max(old_expiry, datetime.now())
            else:
                base_date = datetime.now()
            
            expiry = base_date + timedelta(days=TIER_DURATION_DAYS[tier])
            save_subscription(user_id, tier, expiry, payment_id)
            
            logger.info(f"✅ Razorpay payment: user {user_id}, tier {tier}, payment {payment_id}")
            
            if bot_app:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(send_invite_link(user_id, tier, expiry, payment_id))
            
            return jsonify({"status": "success"}), 200
        
        else:
            logger.info(f"Received Razorpay event: {event_type}")
            return jsonify({"status": "ignored"}), 200
    
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@webhook_app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({"status": "ok", "service": "telegram-bot-webhook"}), 200

@webhook_app.route('/', methods=['GET'])
def index():
    """Root endpoint."""
    return jsonify({
        "status": "online",
        "service": "Desire Musing Bot Webhook Server",
        "endpoints": {
            "webhook": "/webhook/razorpay",
            "health": "/health"
        }
    }), 200

# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────

def init_db():
    con = sqlite3.connect("subscriptions.db")
    con.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id     INTEGER,
            tier        TEXT,
            expiry      TEXT,
            charge_id   TEXT,
            started_at  TEXT,
            PRIMARY KEY (user_id, tier)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS payments_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            tier        TEXT,
            charge_id   TEXT UNIQUE,
            stars       INTEGER,
            paid_at     TEXT,
            status      TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            user_id     INTEGER PRIMARY KEY,
            reason      TEXT,
            banned_at   TEXT,
            banned_by   INTEGER
        )
    """)
    con.commit()
    con.close()

def get_active_subscription(user_id: int, tier: str):
    try:
        con = sqlite3.connect("subscriptions.db")
        row = con.execute(
            """SELECT tier, expiry, started_at FROM subscriptions
               WHERE user_id = ? AND tier = ? AND expiry > ?""",
            (user_id, tier, datetime.now().isoformat()),
        ).fetchone()
        con.close()
        return row
    except Exception as e:
        logger.error(f"Database error in get_active_subscription: {e}")
        return None

def get_all_active_subscriptions(user_id: int):
    try:
        con = sqlite3.connect("subscriptions.db")
        rows = con.execute(
            """SELECT tier, expiry, started_at FROM subscriptions
               WHERE user_id = ? AND expiry > ?""",
            (user_id, datetime.now().isoformat()),
        ).fetchall()
        con.close()
        return rows
    except Exception as e:
        logger.error(f"Database error in get_all_active_subscriptions: {e}")
        return []

def save_subscription(user_id: int, tier: str, expiry: datetime, charge_id: str):
    try:
        con = sqlite3.connect("subscriptions.db")
        con.execute(
            """INSERT OR REPLACE INTO subscriptions
               (user_id, tier, expiry, charge_id, started_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, tier, expiry.isoformat(), charge_id, datetime.now().isoformat()),
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.error(f"Database error in save_subscription: {e}")

def delete_subscription(user_id: int, tier: str):
    try:
        con = sqlite3.connect("subscriptions.db")
        con.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND tier = ?",
            (user_id, tier),
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.error(f"Database error in delete_subscription: {e}")

def get_expired_subscriptions():
    try:
        con = sqlite3.connect("subscriptions.db")
        rows = con.execute(
            "SELECT user_id, tier FROM subscriptions WHERE expiry <= ?",
            (datetime.now().isoformat(),),
        ).fetchall()
        con.close()
        return rows
    except Exception as e:
        logger.error(f"Database error in get_expired_subscriptions: {e}")
        return []

def charge_id_already_used(charge_id: str) -> bool:
    try:
        con = sqlite3.connect("subscriptions.db")
        row = con.execute(
            "SELECT id FROM payments_log WHERE charge_id = ?", (charge_id,)
        ).fetchone()
        con.close()
        return row is not None
    except Exception as e:
        logger.error(f"Database error in charge_id_already_used: {e}")
        return False

def log_payment(user_id: int, tier: str, charge_id: str, stars: int, status: str):
    try:
        con = sqlite3.connect("subscriptions.db")
        con.execute(
            """INSERT OR IGNORE INTO payments_log
               (user_id, tier, charge_id, stars, paid_at, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, tier, charge_id, stars, datetime.now().isoformat(), status),
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.warning(f"Could not log payment: {e}")

def is_blacklisted(user_id: int) -> bool:
    try:
        con = sqlite3.connect("subscriptions.db")
        row = con.execute(
            "SELECT user_id FROM blacklist WHERE user_id = ?", (user_id,)
        ).fetchone()
        con.close()
        return row is not None
    except Exception as e:
        logger.error(f"Database error in is_blacklisted: {e}")
        return False

def add_to_blacklist(user_id: int, reason: str, banned_by: int):
    try:
        con = sqlite3.connect("subscriptions.db")
        con.execute(
            """INSERT OR REPLACE INTO blacklist (user_id, reason, banned_at, banned_by)
               VALUES (?, ?, ?, ?)""",
            (user_id, reason, datetime.now().isoformat(), banned_by)
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.error(f"Database error in add_to_blacklist: {e}")

def remove_from_blacklist(user_id: int):
    try:
        con = sqlite3.connect("subscriptions.db")
        con.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
        con.commit()
        con.close()
    except Exception as e:
        logger.error(f"Database error in remove_from_blacklist: {e}")

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def format_subscription_card(tier: str, expiry_str: str, started_str: str) -> str:
    exp_dt = datetime.fromisoformat(expiry_str)
    days_left = (exp_dt - datetime.now()).days
    emoji = "🥉" if tier == "bronze" else "🥇"

    if days_left > 1:
        status = f"✅ {days_left} days remaining"
    elif days_left == 1:
        status = "⚠️ Expires tomorrow!"
    else:
        status = "⚠️ Expires today!"

    return (
        f"{emoji} *{tier.title()} — Active*\n"
        f"   Started : {datetime.fromisoformat(started_str).strftime('%d %b %Y')}\n"
        f"   Expires : {exp_dt.strftime('%d %b %Y')}\n"
        f"   Status  : {status}"
    )

async def send_invite_link(user_id: int, tier: str, expiry: datetime, payment_id: str):
    """Send channel invite link after successful payment."""
    if not bot_app:
        logger.error("Bot app not initialized")
        return
    
    try:
        link = await bot_app.bot.create_chat_invite_link(
            chat_id=TIER_CHANNELS[tier],
            creates_join_request=True,
            expire_date=datetime.now() + timedelta(minutes=20),
            member_limit=1,
        )
        
        await bot_app.bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ *Payment Successful! Welcome aboard!*\n\n"
                f"💳 Amount paid: ₹{RAZORPAY_PRICES[tier] // 100}\n"
                f"📦 Plan: *{tier.title()}*\n"
                f"📅 Active until: *{expiry.strftime('%d %b %Y')}*\n"
                f"🔖 Payment ID: `{payment_id}`\n\n"
                f"👇 Tap the link and press *'Request to Join'*\n"
                f"*(Single-use link — valid for 20 minutes)*\n\n"
                f"{link.invite_link}\n\n"
                f"Use /membership anytime to check your status."
            ),
            parse_mode="Markdown"
        )
        
        logger.info(f"Invite link sent to user {user_id}")
        
    except Exception as e:
        logger.error(f"Failed to send invite link: {e}")

def rate_limit(seconds=COOLDOWN_SECONDS):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            now = time()
            
            if user_id in USER_COOLDOWNS:
                if now - USER_COOLDOWNS[user_id] < seconds:
                    if update.callback_query:
                        await update.callback_query.answer(
                            "⏳ Please wait a moment before trying again.",
                            show_alert=True
                        )
                    return
            
            USER_COOLDOWNS[user_id] = now
            return await func(update, context)
        return wrapper
    return decorator

def check_user_access(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        
        if user.is_bot:
            logger.warning(f"Bot account blocked: {user.id} ({user.username})")
            return
        
        if is_blacklisted(user.id):
            if update.message:
                await update.message.reply_text(
                    "❌ You are banned from using this bot.\n"
                    "Contact support if you believe this is a mistake."
                )
            return
        
        return await func(update, context)
    return wrapper

# ─────────────────────────────────────────────
#  HANDLERS
# ─────────────────────────────────────────────

@check_user_access
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    active = get_all_active_subscriptions(user_id)
    active_tiers = {row[0] for row in active}

    keyboard = []
    for tier, label in TIER_LABELS.items():
        if tier in active_tiers:
            keyboard.append([InlineKeyboardButton(
                f"✅ {label} (Active)", callback_data=f"already_{tier}"
            )])
        else:
            keyboard.append([InlineKeyboardButton(label, callback_data=f"select_tier_{tier}")])
    keyboard.append([InlineKeyboardButton("📋 My Subscriptions", callback_data="my_subs")])

    await update.message.reply_text(
        "✨ *Welcome to Desire Musing!*\n\n"
        "Get exclusive access to premium content by choosing a plan below.\n"
        "Payment is made with Telegram Stars ⭐ or Razorpay 💳\n\n"
        "🥉 *Bronze* — 100 ⭐ or ₹80 / 2 months\n"
        "🥇 *Gold* — 250 ⭐ or ₹200 / 2 months\n\n"
        "👇 Select your plan to get started:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

@check_user_access
async def membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    subs = get_all_active_subscriptions(user_id)

    if not subs:
        await update.message.reply_text(
            "❌ *You have no active subscriptions.*\n\n"
            "Use /start to get access to exclusive content!",
            parse_mode="Markdown",
        )
        return

    lines = [format_subscription_card(tier, expiry, started) for tier, expiry, started in subs]
    await update.message.reply_text(
        "📋 *Your Subscriptions:*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown",
    )

@check_user_access
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⭐ *Telegram Stars Balance*\n\n"
        "To check your Stars balance open Telegram and go to:\n"
        "*Settings → My Stars*\n\n"
        "Your balance is shown at the top of that screen.\n\n"
        "💡 *Our prices:*\n"
        "🥉 Bronze — 100 ⭐ or ₹80 (2 months)\n"
        "🥇 Gold   — 250 ⭐ or ₹200 (2 months)\n\n"
        "Use /start when you're ready!",
        parse_mode="Markdown",
    )

async def select_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE, tier: str):
    keyboard = [
        [InlineKeyboardButton("⭐ Telegram Stars", callback_data=f"pay_stars_{tier}")],
        [InlineKeyboardButton("💳 Razorpay (UPI/Card/NetBanking)", callback_data=f"pay_razorpay_{tier}")],
        [InlineKeyboardButton("« Back", callback_data="back_to_start")]
    ]
    
    await update.callback_query.message.reply_text(
        f"💳 *Choose Payment Method*\n\n"
        f"Plan: *{tier.title()}*\n"
        f"Price: ₹{RAZORPAY_PRICES.get(tier, 0) // 100} or {TIER_PRICES.get(tier, 0)} ⭐\n\n"
        f"Select your preferred payment method:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

@check_user_access
@rate_limit(seconds=3)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith("select_tier_"):
        tier = data.replace("select_tier_", "")
        await select_payment_method(update, context, tier)
        return

    if data == "back_to_start":
        active = get_all_active_subscriptions(user_id)
        active_tiers = {row[0] for row in active}

        keyboard = []
        for tier, label in TIER_LABELS.items():
            if tier in active_tiers:
                keyboard.append([InlineKeyboardButton(
                    f"✅ {label} (Active)", callback_data=f"already_{tier}"
                )])
            else:
                keyboard.append([InlineKeyboardButton(label, callback_data=f"select_tier_{tier}")])
        keyboard.append([InlineKeyboardButton("📋 My Subscriptions", callback_data="my_subs")])

        await query.message.edit_text(
            "✨ *Welcome to Desire Musing!*\n\n"
            "Get exclusive access to premium content by choosing a plan below.\n\n"
            "🥉 *Bronze* — 100 ⭐ or ₹80 / 2 months\n"
            "🥇 *Gold* — 250 ⭐ or ₹200 / 2 months\n\n"
            "👇 Select your plan to get started:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("already_"):
        tier = data.replace("already_", "")
        sub = get_active_subscription(user_id, tier)
        if sub:
            exp_dt = datetime.fromisoformat(sub[1])
            days_left = (exp_dt - datetime.now()).days
            await query.message.reply_text(
                f"✅ *You already have an active {tier.title()} subscription!*\n\n"
                f"Expires: *{exp_dt.strftime('%d %b %Y')}* ({days_left} days left)\n\n"
                f"Renewal opens in the last 24 hours of your plan.\n"
                f"Use /membership to view full details.",
                parse_mode="Markdown",
            )
        return

    if data == "my_subs":
        subs = get_all_active_subscriptions(user_id)
        if not subs:
            await query.message.reply_text(
                "❌ *You have no active subscriptions.*\n\n"
                "Tap a plan above to get started!",
                parse_mode="Markdown",
            )
        else:
            lines = [format_subscription_card(tier, expiry, started) for tier, expiry, started in subs]
            await query.message.reply_text(
                "📋 *Your Subscriptions:*\n\n" + "\n\n".join(lines),
                parse_mode="Markdown",
            )
        return

    if data.startswith("pay_razorpay_"):
        tier = data.replace("pay_razorpay_", "")
        
        if tier not in RAZORPAY_PRICES:
            await query.message.reply_text("❌ Unknown tier.")
            return
        
        existing = get_active_subscription(user_id, tier)
        if existing:
            exp_dt = datetime.fromisoformat(existing[1])
            days_left = (exp_dt - datetime.now()).days
            if days_left > 1:
                await query.message.reply_text(
                    f"⚠️ You already have an active {tier.title()} subscription!\n"
                    f"Expires: {exp_dt.strftime('%d %b %Y')} ({days_left} days left)",
                    parse_mode="Markdown"
                )
                return
        
        payment_link = RAZORPAY_PAYMENT_PAGES.get(tier, "")
        
        if not payment_link:
            await query.message.reply_text("❌ Payment link not configured. Contact admin.")
            return
        
        payment_link_with_params = f"{payment_link}?user_id={user_id}&tier={tier}"
        
        keyboard = [[InlineKeyboardButton("💳 Pay Now", url=payment_link_with_params)]]
        
        await query.message.reply_text(
            f"💳 *Razorpay Payment*\n\n"
            f"Plan: *{tier.title()}*\n"
            f"Amount: *₹{RAZORPAY_PRICES[tier] // 100}*\n\n"
            f"Click the button below to complete payment.\n"
            f"You'll receive instant access after payment! ⚡\n\n"
            f"⚠️ *Note:* Payment processed by Razorpay (Secure)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        logger.info(f"Razorpay link sent to user {user_id} for {tier}")
        return

    if data.startswith("pay_stars_"):
        tier = data.replace("pay_stars_", "")
        if tier not in TIER_PRICES:
            await query.message.reply_text("❌ Unknown tier. Please try again.")
            return

        existing = get_active_subscription(user_id, tier)
        if existing:
            exp_dt = datetime.fromisoformat(existing[1])
            days_left = (exp_dt - datetime.now()).days

            if days_left > 1:
                await query.message.reply_text(
                    f"⚠️ *You already have an active {tier.title()} subscription!*\n\n"
                    f"Expires: *{exp_dt.strftime('%d %b %Y')}* ({days_left} days left)\n\n"
                    f"You can renew in the last 24 hours of your plan.\n"
                    f"No payment needed right now — you're all set! 🎉",
                    parse_mode="Markdown",
                )
                return

        try:
            await context.bot.send_invoice(
                chat_id=user_id,
                title=f"Desire Musing {tier.title()} Access",
                description=f"{TIER_DURATION_DAYS[tier]}-day access to the {tier.title()} private channel.",
                payload=tier,
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(f"{tier.title()} Subscription", TIER_PRICES[tier])],
            )
            logger.info(f"Invoice sent to user {user_id} for {tier}")
        except Exception as e:
            logger.error(f"Failed to send invoice to user {user_id}: {e}")
            await query.message.reply_text(
                "❌ Failed to generate payment invoice. Please try again or contact support."
            )

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    user_id = query.from_user.id
    tier = query.invoice_payload

    if tier not in TIER_PRICES:
        await query.answer(
            ok=False,
            error_message="Invalid subscription tier. Please try again."
        )
        return

    expected_amount = TIER_PRICES[tier]
    actual_amount = query.total_amount

    if actual_amount != expected_amount:
        await query.answer(
            ok=False,
            error_message=f"Payment amount mismatch. Expected {expected_amount} stars."
        )
        return

    existing = get_active_subscription(user_id, tier)
    if existing:
        exp_dt = datetime.fromisoformat(existing[1])
        days_left = (exp_dt - datetime.now()).days
        if days_left > 1:
            await query.answer(
                ok=False,
                error_message=(
                    f"You already have an active {tier.title()} subscription "
                    f"until {exp_dt.strftime('%d %b %Y')}. No payment needed!"
                ),
            )
            return

    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payment = update.message.successful_payment
    tier = payment.invoice_payload
    charge_id = payment.telegram_payment_charge_id
    stars_paid = payment.total_amount

    if charge_id_already_used(charge_id):
        logger.warning(f"Duplicate charge_id {charge_id} ignored for user {user_id}")
        await update.message.reply_text(
            "⚠️ This payment was already processed.\n"
            "Use /membership to check your subscription status."
        )
        return

    log_payment(user_id, tier, charge_id, stars_paid, "success")

    existing = get_active_subscription(user_id, tier)
    if existing:
        old_expiry = datetime.fromisoformat(existing[1])
        base_date = max(old_expiry, datetime.now())
    else:
        base_date = datetime.now()

    expiry = base_date + timedelta(days=TIER_DURATION_DAYS[tier])

    save_subscription(user_id, tier, expiry, charge_id)
    logger.info(f"✅ Payment confirmed: user {user_id}, tier {tier}, charge {charge_id}, expires {expiry.date()}")

    try:
        link = await context.bot.create_chat_invite_link(
            chat_id=TIER_CHANNELS[tier],
            creates_join_request=True,
            expire_date=datetime.now() + timedelta(minutes=20),
            member_limit=1,
        )
        
        await update.message.reply_text(
            f"✅ *Payment Successful! Thank you!*\n\n"
            f"⭐ Stars paid : *{stars_paid}*\n"
            f"📦 Plan       : *{tier.title()}*\n"
            f"📅 Active until: *{expiry.strftime('%d %b %Y')}*\n"
            f"🔖 Ref        : `{charge_id}`\n\n"
            f"👇 Tap the link and press *'Request to Join'*\n"
            f"*(Single-use link — valid for 20 minutes)*\n\n"
            f"{link.invite_link}\n\n"
            f"Use /membership anytime to check your status.",
            parse_mode="Markdown",
        )
        
        context.job_queue.run_once(
            revoke_link_callback,
            when=1200,
            data={"chat_id": TIER_CHANNELS[tier], "link": link.invite_link},
        )
        
    except Exception as e:
        logger.error(f"Failed to create invite link for user {user_id}: {e}")
        await update.message.reply_text(
            f"✅ *Payment received!*\n\n"
            f"We had trouble generating your invite link automatically.\n"
            f"Please contact the admin and share this reference:\n`{charge_id}`\n\n"
            f"We'll add you manually within a few minutes.",
            parse_mode="Markdown",
        )

async def revoke_link_callback(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.revoke_chat_invite_link(
            chat_id=data["chat_id"],
            invite_link=data["link"]
        )
        logger.info(f"Revoked invite link: {data['link']}")
    except Exception as e:
        logger.warning(f"Could not revoke link: {e}")

async def approve_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.chat_join_request.user.id
    chat_id = update.chat_join_request.chat.id
    tier = CHANNEL_TO_TIER.get(chat_id)

    if not tier:
        await update.chat_join_request.decline()
        logger.warning(f"Join request from unknown channel {chat_id} — declined")
        return

    if is_blacklisted(user_id):
        await update.chat_join_request.decline()
        logger.warning(f"Declined join request from blacklisted user {user_id}")
        return

    sub = get_active_subscription(user_id, tier)

    if sub:
        await update.chat_join_request.approve()
        logger.info(f"✅ Approved join request: user {user_id} → {tier}")
    else:
        await update.chat_join_request.decline()
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"❌ *Access Denied*\n\n"
                    f"You don't have an active *{tier.title()}* subscription.\n\n"
                    f"Use /start to subscribe and get access."
                ),
                parse_mode="Markdown",
            )
        except:
            pass
        logger.warning(f"❌ Declined join request: user {user_id} has no active {tier} sub")

# ─────────────────────────────────────────────
#  ADMIN COMMANDS
# ─────────────────────────────────────────────

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/ban <user_id> <reason>`\n\n"
            "Example: `/ban 123456789 Spam and abuse`",
            parse_mode="Markdown"
        )
        return

    try:
        target_user_id = int(context.args[0])
        reason = " ".join(context.args[1:])

        add_to_blacklist(target_user_id, reason, update.effective_user.id)

        for tier, channel_id in TIER_CHANNELS.items():
            try:
                await context.bot.ban_chat_member(channel_id, target_user_id)
                delete_subscription(target_user_id, tier)
            except Exception as e:
                logger.warning(f"Could not kick user {target_user_id} from {tier}: {e}")

        await update.message.reply_text(
            f"✅ *User Banned*\n\n"
            f"User ID: `{target_user_id}`\n"
            f"Reason: {reason}\n\n"
            f"They have been removed from all channels and blacklisted.",
            parse_mode="Markdown"
        )
        logger.info(f"User {target_user_id} banned by admin: {reason}")

    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
    except Exception as e:
        logger.error(f"Error in ban command: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "Usage: `/unban <user_id>`\n\n"
            "Example: `/unban 123456789`",
            parse_mode="Markdown"
        )
        return

    try:
        target_user_id = int(context.args[0])

        if not is_blacklisted(target_user_id):
            await update.message.reply_text(f"⚠️ User {target_user_id} is not banned.")
            return

        remove_from_blacklist(target_user_id)

        for tier, channel_id in TIER_CHANNELS.items():
            try:
                await context.bot.unban_chat_member(channel_id, target_user_id)
            except Exception as e:
                logger.warning(f"Could not unban user {target_user_id} from {tier}: {e}")

        await update.message.reply_text(
            f"✅ *User Unbanned*\n\n"
            f"User ID: `{target_user_id}`\n\n"
            f"They can now use the bot again.",
            parse_mode="Markdown"
        )
        logger.info(f"User {target_user_id} unbanned by admin")

    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
    except Exception as e:
        logger.error(f"Error in unban command: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def check_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "Usage: `/check <user_id>`\n\n"
            "Example: `/check 123456789`",
            parse_mode="Markdown"
        )
        return

    try:
        target_user_id = int(context.args[0])

        banned = is_blacklisted(target_user_id)
        subs = get_all_active_subscriptions(target_user_id)

        response = f"📊 *User Status Report*\n\n"
        response += f"User ID: `{target_user_id}`\n"
        response += f"Banned: {'❌ Yes' if banned else '✅ No'}\n\n"

        if subs:
            response += "📋 *Active Subscriptions:*\n\n"
            for tier, expiry, started in subs:
                exp_dt = datetime.fromisoformat(expiry)
                days_left = (exp_dt - datetime.now()).days
                response += (
                    f"• {tier.title()}\n"
                    f"  Expires: {exp_dt.strftime('%d %b %Y')} ({days_left} days)\n"
                    f"  Started: {datetime.fromisoformat(started).strftime('%d %b %Y')}\n\n"
                )
        else:
            response += "❌ No active subscriptions"

        await update.message.reply_text(response, parse_mode="Markdown")

    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
    except Exception as e:
        logger.error(f"Error in check command: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

# ─────────────────────────────────────────────
#  DAILY EXPIRY CHECK
# ─────────────────────────────────────────────

async def check_expiries(app):
    logger.info("Running daily expiry check...")
    expired = get_expired_subscriptions()

    if not expired:
        logger.info("No expired subscriptions.")
        return

    for user_id, tier in expired:
        channel_id = TIER_CHANNELS.get(tier)
        if not channel_id:
            continue
        try:
            await app.bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
            await app.bot.unban_chat_member(chat_id=channel_id, user_id=user_id)
            
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⏰ *Your {tier.title()} subscription has expired.*\n\n"
                    f"You have been removed from the {tier.title()} channel.\n\n"
                    f"Use /start to renew and get access again! 🔄"
                ),
                parse_mode="Markdown",
            )
            logger.info(f"Kicked expired user {user_id} from {tier}")
        except Exception as e:
            logger.warning(f"Could not kick user {user_id} from {tier}: {e}")
        finally:
            delete_subscription(user_id, tier)

# ─────────────────────────────────────────────
#  RUN WEBHOOK SERVER
# ─────────────────────────────────────────────

def run_webhook_server():
    logger.info(f"Starting webhook server on port {WEBHOOK_PORT}...")
    webhook_app.run(host='0.0.0.0', port=WEBHOOK_PORT, debug=False, use_reloader=False)

# ───���─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    global bot_app
    
    init_db()
    
    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("membership", membership))
    bot_app.add_handler(CommandHandler("balance", balance))
    bot_app.add_handler(CommandHandler("ban", ban_user))
    bot_app.add_handler(CommandHandler("unban", unban_user))
    bot_app.add_handler(CommandHandler("check", check_user))
    bot_app.add_handler(CallbackQueryHandler(button_handler))
    bot_app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    bot_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    bot_app.add_handler(ChatJoinRequestHandler(approve_join_request))

    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start", "🏠 Main menu & subscription plans"),
            BotCommand("membership", "📋 Check your active subscriptions"),
            BotCommand("balance", "💰 How to check your Stars balance"),
        ])
        
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            check_expiries,
            trigger="interval",
            hours=24,
            args=[application],
            next_run_time=datetime.now(),
        )
        scheduler.start()
        
        logger.info("✅ Bot started successfully!")
        logger.info(f"✅ Webhook server running on port {WEBHOOK_PORT}")

    bot_app.post_init = post_init

    webhook_thread = threading.Thread(target=run_webhook_server, daemon=True)
    webhook_thread.start()
    
    logger.info("🚀 Bot + Webhook server running... Press Ctrl+C to stop.")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
