import logging
import os
import psycopg2
from psycopg2 import pool as pg_pool
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from flask import Flask, request, jsonify
import hmac
import hashlib
import threading
import requests
from dotenv import load_dotenv

load_dotenv('.env.admin')
load_dotenv()  # also pick up generic .env if present

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
MAIN_BOT_API_URL = os.environ.get("MAIN_BOT_API_URL", "http://localhost:8080")
SECRET_API_KEY = os.environ.get("SECRET_API_KEY", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
# Railway injects $PORT for the public web server. Prefer it, then fall back
# to ADMIN_PORT (explicit override) and finally 8081 for local development.
WEBHOOK_PORT = int(os.environ.get("PORT") or os.environ.get("ADMIN_PORT", "8081"))

# Same Neon Postgres database as the main bot. We use a separate table
# (pending_payments) so the two services don't step on each other.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise ValueError(
        "❌ DATABASE_URL not set!\n"
        "Set the Neon connection string as an environment variable:\n"
        "DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require"
    )

# Tier names accepted when an admin approves a payment. Values are only used
# for validation here; actual charge amounts live in bot.py (RAZORPAY_PRICES).
TIER_PRICES = {
    "bronze": 100,
    "gold": 200,
}

TIER_DURATION_DAYS = {
    "bronze": 60,
    "gold": 60,
}

TIER_EMOJI = {
    "bronze": "🥉",
    "gold": "🥇",
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  DATABASE (PostgreSQL / Neon)
# ─────────────────────────────────────────────

_db_pool = None
_db_pool_lock = threading.Lock()


def _build_pool():
    return pg_pool.SimpleConnectionPool(
        minconn=1,
        maxconn=3,
        dsn=DATABASE_URL,
    )


def _get_pool():
    global _db_pool
    if _db_pool is None:
        with _db_pool_lock:
            if _db_pool is None:
                _db_pool = _build_pool()
    return _db_pool


class _PooledConnection:
    def __enter__(self):
        pool = _get_pool()
        self._pool = pool
        self.conn = pool.getconn()
        try:
            self.conn.rollback()
        except Exception:
            pass
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                self.conn.commit()
            except Exception as e:
                logger.error(f"DB commit failed: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass
        else:
            try:
                self.conn.rollback()
            except Exception:
                pass
        try:
            self._pool.putconn(self.conn)
        except Exception as e:
            logger.warning(f"Returning conn to pool failed: {e}")
        return False


def _db():
    return _PooledConnection()


def init_db():
    """Create the pending_payments table on first boot. Idempotent."""
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_payments (
                    id          SERIAL PRIMARY KEY,
                    payment_id  TEXT UNIQUE,
                    amount      INTEGER,
                    email       TEXT,
                    contact     TEXT,
                    user_id     BIGINT,
                    tier        TEXT,
                    received_at TEXT,
                    status      TEXT DEFAULT 'pending'
                )
            """)
    logger.info("✅ Admin bot database tables ready (PostgreSQL/Neon)")


def _coerce_user_id(user_id):
    """Notes from Razorpay arrive as strings; coerce to int or None."""
    if user_id is None or user_id == "":
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def save_payment(payment_id, amount, email, contact, user_id, tier):
    try:
        with _db() as con:
            with con.cursor() as cur:
                cur.execute(
                    """INSERT INTO pending_payments
                       (payment_id, amount, email, contact, user_id, tier, received_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (payment_id) DO NOTHING""",
                    (
                        payment_id,
                        amount,
                        email,
                        contact,
                        _coerce_user_id(user_id),
                        tier,
                        datetime.now().isoformat(),
                    ),
                )
    except Exception as e:
        logger.error(f"Database error in save_payment: {e}")


def mark_approved(payment_id):
    try:
        with _db() as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE pending_payments SET status = 'approved' WHERE payment_id = %s",
                    (payment_id,),
                )
    except Exception as e:
        logger.error(f"Database error in mark_approved: {e}")


def get_pending_payments():
    try:
        with _db() as con:
            with con.cursor() as cur:
                cur.execute(
                    """SELECT payment_id, amount, email, contact, user_id, tier, received_at
                       FROM pending_payments WHERE status = 'pending'
                       ORDER BY received_at DESC LIMIT 20"""
                )
                return cur.fetchall()
    except Exception as e:
        logger.error(f"Database error in get_pending_payments: {e}")
        return []


# ─── Stats helpers (read-only queries against shared Neon DB) ────────────────

def count_pending_payments() -> int:
    try:
        with _db() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM pending_payments WHERE status = 'pending'"
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception as e:
        logger.error(f"Database error in count_pending_payments: {e}")
        return 0


def count_active_by_tier(tier: str) -> int:
    """Count subscribers whose subscription hasn't expired yet."""
    try:
        with _db() as con:
            with con.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*) FROM subscriptions
                       WHERE tier = %s AND expiry > %s""",
                    (tier, datetime.now().isoformat()),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception as e:
        # Table may not exist yet if main bot hasn't run init_db. Treat as 0.
        logger.warning(f"count_active_by_tier({tier}) failed: {e}")
        return 0


def count_banned_users() -> int:
    try:
        with _db() as con:
            with con.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM blacklist")
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception as e:
        logger.warning(f"count_banned_users failed: {e}")
        return 0


def list_subscribers_by_tier(tier: str, limit: int = 20):
    """Return (user_id, expiry, started_at) for active subscribers of a tier."""
    try:
        with _db() as con:
            with con.cursor() as cur:
                cur.execute(
                    """SELECT user_id, expiry, started_at FROM subscriptions
                       WHERE tier = %s AND expiry > %s
                       ORDER BY expiry ASC LIMIT %s""",
                    (tier, datetime.now().isoformat(), limit),
                )
                return cur.fetchall()
    except Exception as e:
        logger.warning(f"list_subscribers_by_tier({tier}) failed: {e}")
        return []


def list_banned_users(limit: int = 20):
    """Return (user_id, reason, banned_at) of banned users."""
    try:
        with _db() as con:
            with con.cursor() as cur:
                cur.execute(
                    """SELECT user_id, reason, banned_at FROM blacklist
                       ORDER BY banned_at DESC LIMIT %s""",
                    (limit,),
                )
                return cur.fetchall()
    except Exception as e:
        logger.warning(f"list_banned_users failed: {e}")
        return []


def get_recent_payments_summary(days: int = 30):
    """Return (count, total_stars) for successful payments in the last N days."""
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with _db() as con:
            with con.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*), COALESCE(SUM(stars), 0)
                       FROM payments_log
                       WHERE paid_at > %s AND status IN ('success', 'razorpay_captured', 'razorpay_admin_approved')""",
                    (cutoff,),
                )
                row = cur.fetchone()
                if row:
                    return int(row[0]), int(row[1] or 0)
                return 0, 0
    except Exception as e:
        logger.warning(f"get_recent_payments_summary failed: {e}")
        return 0, 0


# ─────────────────────────────────────────────
#  FLASK WEBHOOK SERVER
# ─────────────────────────────────────────────

webhook_app = Flask(__name__)
admin_bot = None

def verify_razorpay_signature(payload, signature, secret):
    if not secret:
        return False
    expected_signature = hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_signature, signature)

@webhook_app.route('/webhook/razorpay', methods=['POST'])
def razorpay_webhook():
    try:
        payload = request.get_data()
        signature = request.headers.get('X-Razorpay-Signature', '')
        
        logger.info("Received Razorpay webhook")
        
        if RAZORPAY_WEBHOOK_SECRET and not verify_razorpay_signature(payload, signature, RAZORPAY_WEBHOOK_SECRET):
            logger.warning("Invalid signature")
            return jsonify({"error": "Invalid signature"}), 401
        
        event = request.get_json()
        event_type = event.get('event')
        
        if event_type == 'payment.captured':
            payment = event['payload']['payment']['entity']
            
            payment_id = payment['id']
            amount = payment['amount']
            email = payment.get('email', 'N/A')
            contact = payment.get('contact', 'N/A')
            
            notes = payment.get('notes', {})
            user_id = notes.get('user_id')
            tier = notes.get('tier')
            
            logger.info(f"Payment: {payment_id}, ₹{amount//100}, user: {user_id}, tier: {tier}")
            
            # Save to database
            save_payment(payment_id, amount, email, contact, user_id, tier)
            
            # Notify admin
            if admin_bot:
                threading.Thread(
                    target=notify_admin,
                    args=(payment_id, amount, email, contact, user_id, tier)
                ).start()
            
            return jsonify({"status": "notified"}), 200
        
        return jsonify({"status": "ignored"}), 200
    
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@webhook_app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "admin-bot"}), 200

def notify_admin(payment_id, amount, email, contact, user_id, tier):
    """Send payment notification to admin."""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_notify_admin(payment_id, amount, email, contact, user_id, tier))

async def _notify_admin(payment_id, amount, email, contact, user_id, tier):
    try:
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment_id}")
            ]
        ]

        tier_emoji = TIER_EMOJI.get((tier or "").lower(), "💳")
        received_at = datetime.now().strftime("%d %b %H:%M")

        message = (
            f"🚨 *NEW PAYMENT RECEIVED*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{tier_emoji} *Tier:*       {(tier or 'Not provided').title()}\n"
            f"💰 *Amount:*    ₹{amount // 100}\n"
            f"🕐 *Received:*  {received_at}\n\n"
            f"👤 *User ID:*   `{user_id or 'Not provided'}`\n"
            f"📧 *Email:*       {email}\n"
            f"📱 *Contact:*   {contact}\n"
            f"🔖 *Payment:*  `{payment_id}`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👇 *Take action:*"
        )

        await admin_bot.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=message,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.info(f"Admin notified about payment {payment_id}")
    except Exception as e:
        logger.error(f"Failed to notify admin: {e}")

# ─────────────────────────────────────────────
#  BOT HANDLERS — DASHBOARD UI
# ─────────────────────────────────────────────

def _build_main_menu_keyboard():
    """Build the main admin menu with live count badges."""
    pending = count_pending_payments()
    bronze = count_active_by_tier("bronze")
    gold = count_active_by_tier("gold")
    banned = count_banned_users()

    pending_label = f"📋 Pending Payments ({pending})" if pending else "📋 Pending Payments"
    banned_label = f"🚫 Banned Users ({banned})" if banned else "🚫 Banned Users"

    keyboard = [
        [InlineKeyboardButton(pending_label, callback_data="admin_pending")],
        [InlineKeyboardButton(f"🥉 Bronze — {bronze} active", callback_data="admin_tier_bronze")],
        [InlineKeyboardButton(f"🥇 Gold — {gold} active", callback_data="admin_tier_gold")],
        [InlineKeyboardButton(banned_label, callback_data="admin_banned")],
        [InlineKeyboardButton("📊 Subscription Stats", callback_data="admin_stats")],
    ]
    return InlineKeyboardMarkup(keyboard)


def _main_menu_text():
    return (
        "🛡️ *Admin Control Panel*\n\n"
        "Welcome back. Here's your dashboard.\n\n"
        "📊 Quick actions below\n"
        "🔔 You'll be notified of new payments\n"
        "⚡ Tap any button to drill in\n\n"
        "👇 *ADMIN ACTIONS:*"
    )


def _back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="admin_back")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    await update.message.reply_text(
        _main_menu_text(),
        parse_mode="Markdown",
        reply_markup=_build_main_menu_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abort an in-progress payment approval (clears pending state)."""
    if update.effective_user.id != ADMIN_USER_ID:
        return

    if 'approving_payment' in context.user_data:
        payment_id = context.user_data.pop('approving_payment')
        await update.message.reply_text(
            f"🚫 *Approval cancelled*\n"
            f"Payment `{payment_id}` left pending.\n\n"
            f"Use the notification or /pending to retry.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("ℹ️ Nothing to cancel.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Render the stats card directly via /stats."""
    if update.effective_user.id != ADMIN_USER_ID:
        return
    text, keyboard = _build_stats_view()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def pending_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy /pending command — same content as the dashboard button."""
    if update.effective_user.id != ADMIN_USER_ID:
        return
    text, keyboard = _build_pending_view()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ─── Dashboard view builders (text + keyboard tuples) ────────────────────────

def _build_pending_view():
    pending = get_pending_payments()
    if not pending:
        text = (
            "📋 *Pending Payments*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "✅ No pending payments right now.\n\n"
            "New Razorpay payments will appear here automatically."
        )
        return text, _back_button()

    lines = [f"📋 *Pending Payments* ({len(pending)})", "━━━━━━━━━━━━━━━━━━━━", ""]
    for payment_id, amount, email, contact, user_id, tier, received_at in pending:
        tier_emoji = TIER_EMOJI.get((tier or "").lower(), "💳")
        try:
            ts = datetime.fromisoformat(received_at).strftime("%d %b %H:%M")
        except Exception:
            ts = received_at or "N/A"
        lines.append(
            f"{tier_emoji} *{(tier or 'N/A').title()}* — ₹{amount // 100}\n"
            f"   👤 User: `{user_id or 'N/A'}`\n"
            f"   🕐 {ts}\n"
            f"   🔖 `{payment_id}`\n"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 Tap a payment notification to approve.")
    return "\n".join(lines), _back_button()


def _build_tier_view(tier: str):
    tier_emoji = TIER_EMOJI.get(tier, "💳")
    rows = list_subscribers_by_tier(tier)

    if not rows:
        text = (
            f"{tier_emoji} *{tier.title()} Subscribers*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"No active {tier} subscribers yet."
        )
        return text, _back_button()

    lines = [
        f"{tier_emoji} *{tier.title()} Subscribers* ({len(rows)})",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
    ]
    for user_id, expiry_str, started_str in rows:
        try:
            exp_dt = datetime.fromisoformat(expiry_str)
            days_left = (exp_dt - datetime.now()).days
            exp_fmt = exp_dt.strftime("%d %b %Y")
        except Exception:
            exp_fmt = expiry_str or "N/A"
            days_left = "?"
        try:
            started_fmt = datetime.fromisoformat(started_str).strftime("%d %b %Y")
        except Exception:
            started_fmt = started_str or "N/A"
        lines.append(
            f"👤 `{user_id}`\n"
            f"   📅 Expires: {exp_fmt} ({days_left} days)\n"
            f"   🕐 Started: {started_fmt}\n"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 Showing up to 20 subscribers, sorted by expiry.")
    return "\n".join(lines), _back_button()


def _build_banned_view():
    rows = list_banned_users()
    if not rows:
        text = (
            "🚫 *Banned Users*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "✅ No users are currently banned."
        )
        return text, _back_button()

    lines = [f"🚫 *Banned Users* ({len(rows)})", "━━━━━━━━━━━━━━━━━━━━", ""]
    for user_id, reason, banned_at in rows:
        try:
            ts = datetime.fromisoformat(banned_at).strftime("%d %b %Y")
        except Exception:
            ts = banned_at or "N/A"
        reason_str = reason or "(no reason)"
        if len(reason_str) > 60:
            reason_str = reason_str[:57] + "..."
        lines.append(
            f"👤 `{user_id}`\n"
            f"   📝 {reason_str}\n"
            f"   🕐 Banned: {ts}\n"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 Use /unban <user_id> in the main bot to remove a ban.")
    return "\n".join(lines), _back_button()


def _build_stats_view():
    pending = count_pending_payments()
    bronze = count_active_by_tier("bronze")
    gold = count_active_by_tier("gold")
    banned = count_banned_users()
    total_active = bronze + gold

    payments_30d, stars_30d = get_recent_payments_summary(30)

    text = (
        f"📊 *Subscription Overview*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🟢 *Active subscriptions*\n"
        f"   🥉 Bronze: {bronze}\n"
        f"   🥇 Gold: {gold}\n"
        f"   ━━━━━━━━━━\n"
        f"   Total: *{total_active}*\n\n"
        f"📋 *Pending payments:* {pending}\n"
        f"🚫 *Banned users:* {banned}\n\n"
        f"💰 *Last 30 days*\n"
        f"   Payments: {payments_30d}\n"
        f"   Stars: ⭐ {stars_30d}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Counts are live from the database._"
    )
    return text, _back_button()


# ─── Master callback router ──────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != ADMIN_USER_ID:
        return

    data = query.data

    # ─── Dashboard navigation ────────────────────────────────────────────
    if data == "admin_back":
        await query.message.edit_text(
            _main_menu_text(),
            parse_mode="Markdown",
            reply_markup=_build_main_menu_keyboard(),
        )
        return

    if data == "admin_pending":
        text, keyboard = _build_pending_view()
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if data == "admin_tier_bronze":
        text, keyboard = _build_tier_view("bronze")
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if data == "admin_tier_gold":
        text, keyboard = _build_tier_view("gold")
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if data == "admin_banned":
        text, keyboard = _build_banned_view()
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if data == "admin_stats":
        text, keyboard = _build_stats_view()
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    # ─── Payment notification actions (existing flow) ────────────────────
    if data.startswith("approve_"):
        payment_id = data.replace("approve_", "")

        await query.message.reply_text(
            f"💳 *Approving payment*\n"
            f"🔖 `{payment_id}`\n\n"
            f"📝 Reply with the user details:\n"
            f"`<user_id> <tier>`\n\n"
            f"📌 *Examples:*\n"
            f"• `12345 bronze` — Bronze tier\n"
            f"• `67890 gold` — Gold tier\n\n"
            f"Send /cancel to abort.",
            parse_mode="Markdown"
        )
        context.user_data['approving_payment'] = payment_id
        return

    if data.startswith("reject_"):
        payment_id = data.replace("reject_", "")
        mark_approved(payment_id)  # Mark as processed

        await query.message.edit_text(
            f"❌ *Payment rejected*\n"
            f"🔖 `{payment_id}`",
            parse_mode="Markdown"
        )
        return


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    
    if 'approving_payment' not in context.user_data:
        return
    
    payment_id = context.user_data['approving_payment']
    text = update.message.text.strip()
    
    try:
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("❌ Format: `<user_id> <tier>`", parse_mode="Markdown")
            return
        
        user_id = int(parts[0])
        tier = parts[1].lower()
        
        if tier not in TIER_PRICES:
            await update.message.reply_text("❌ Invalid tier. Use: bronze or gold")
            return
        
        # Send to main bot
        response = requests.post(
            f"{MAIN_BOT_API_URL}/api/grant_subscription",
            json={
                "user_id": user_id,
                "tier": tier,
                "payment_id": payment_id,
                "api_key": SECRET_API_KEY
            },
            timeout=10
        )
        
        if response.status_code == 200:
            mark_approved(payment_id)
            del context.user_data['approving_payment']

            tier_emoji = TIER_EMOJI.get(tier, "💳")
            await update.message.reply_text(
                f"✅ *Access Granted Successfully!*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{tier_emoji} *Tier:*     {tier.title()}\n"
                f"👤 *User:*     `{user_id}`\n"
                f"🔖 *Payment:* `{payment_id}`\n"
                f"📅 *Duration:* {TIER_DURATION_DAYS[tier]} days\n\n"
                f"✨ User has received their invite link.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"❌ Failed to grant subscription: {response.text}"
            )
    
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID")
    except Exception as e:
        logger.error(f"Error granting subscription: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def run_webhook_server():
    logger.info(f"Starting admin webhook server on port {WEBHOOK_PORT}...")
    webhook_app.run(host='0.0.0.0', port=WEBHOOK_PORT, debug=False, use_reloader=False)

def main():
    global admin_bot
    
    if not ADMIN_BOT_TOKEN:
        raise ValueError("ADMIN_BOT_TOKEN not set!")
    
    init_db()
    
    admin_bot = ApplicationBuilder().token(ADMIN_BOT_TOKEN).build()
    
    admin_bot.add_handler(CommandHandler("start", start))
    admin_bot.add_handler(CommandHandler("pending", pending_payments))
    admin_bot.add_handler(CommandHandler("stats", stats_command))
    admin_bot.add_handler(CommandHandler("cancel", cancel))
    admin_bot.add_handler(CallbackQueryHandler(button_handler))
    from telegram.ext import MessageHandler, filters
    admin_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start",   "🛡️ Admin home & dashboard"),
            BotCommand("pending", "📋 Review queued payments"),
            BotCommand("stats",   "📊 Subscription overview"),
            BotCommand("cancel",  "🚫 Abort current approval"),
        ])
        logger.info("✅ Admin bot commands registered")

    admin_bot.post_init = post_init

    webhook_thread = threading.Thread(target=run_webhook_server, daemon=True)
    webhook_thread.start()
    
    logger.info("🚀 Admin bot + webhook server running...")
    admin_bot.run_polling()

if __name__ == "__main__":
    main()
