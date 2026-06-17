import logging
import os
import psycopg2
from psycopg2 import pool as pg_pool
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
        
        message = (
            f"💳 *New Razorpay Payment*\n\n"
            f"Payment ID: `{payment_id}`\n"
            f"Amount: *₹{amount // 100}*\n"
            f"Email: {email}\n"
            f"Contact: {contact}\n\n"
            f"User ID: `{user_id or 'Not provided'}`\n"
            f"Tier: {tier or 'Not provided'}\n\n"
            f"⚠️ Action required!"
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
#  BOT HANDLERS
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    
    await update.message.reply_text(
        "🔐 *Admin Payment Approval Bot*\n\n"
        "I'll notify you when Razorpay payments are received.\n\n"
        "Commands:\n"
        "/pending - View pending payments\n"
        "/start - This message",
        parse_mode="Markdown"
    )

async def pending_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    
    pending = get_pending_payments()
    
    if not pending:
        await update.message.reply_text("✅ No pending payments.")
        return
    
    response = "📋 *Pending Payments:*\n\n"
    
    for payment_id, amount, email, contact, user_id, tier, received_at in pending:
        response += (
            f"💳 `{payment_id}`\n"
            f"   ₹{amount // 100} | {tier or 'N/A'}\n"
            f"   User: `{user_id or 'N/A'}`\n"
            f"   {datetime.fromisoformat(received_at).strftime('%d %b %H:%M')}\n\n"
        )
    
    await update.message.reply_text(response, parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id != ADMIN_USER_ID:
        return
    
    data = query.data
    
    if data.startswith("approve_"):
        payment_id = data.replace("approve_", "")
        
        # Ask for user_id and tier
        await query.message.reply_text(
            f"💳 Approving payment: `{payment_id}`\n\n"
            f"Please reply with:\n"
            f"`<user_id> <tier>`\n\n"
            f"Example: `123456789 bronze`",
            parse_mode="Markdown"
        )
        context.user_data['approving_payment'] = payment_id
        
    elif data.startswith("reject_"):
        payment_id = data.replace("reject_", "")
        mark_approved(payment_id)  # Mark as processed
        
        await query.message.edit_text(
            f"❌ Payment rejected: `{payment_id}`",
            parse_mode="Markdown"
        )

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
            
            await update.message.reply_text(
                f"✅ *Subscription Granted!*\n\n"
                f"User: `{user_id}`\n"
                f"Tier: {tier.title()}\n"
                f"Payment: `{payment_id}`\n\n"
                f"User has been notified and given access.",
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
    admin_bot.add_handler(CallbackQueryHandler(button_handler))
    from telegram.ext import MessageHandler, filters
    admin_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    webhook_thread = threading.Thread(target=run_webhook_server, daemon=True)
    webhook_thread.start()
    
    logger.info("🚀 Admin bot + webhook server running...")
    admin_bot.run_polling()

if __name__ == "__main__":
    main()
