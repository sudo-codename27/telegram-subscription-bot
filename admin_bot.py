import logging
import os
import sqlite3
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

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
MAIN_BOT_API_URL = os.environ.get("MAIN_BOT_API_URL", "http://localhost:8080")
SECRET_API_KEY = os.environ.get("SECRET_API_KEY", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
WEBHOOK_PORT = int(os.environ.get("ADMIN_PORT", 8081))

TIER_PRICES = {
    "bronze": 100,  # ₹1 for testing
    "gold": 200,    # ₹2 for testing
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
#  DATABASE
# ─────────────────────────────────────────────

def init_db():
    con = sqlite3.connect("admin_payments.db")
    con.execute("""
        CREATE TABLE IF NOT EXISTS pending_payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id  TEXT UNIQUE,
            amount      INTEGER,
            email       TEXT,
            contact     TEXT,
            user_id     INTEGER,
            tier        TEXT,
            received_at TEXT,
            status      TEXT DEFAULT 'pending'
        )
    """)
    con.commit()
    con.close()

def save_payment(payment_id, amount, email, contact, user_id, tier):
    try:
        con = sqlite3.connect("admin_payments.db")
        con.execute(
            """INSERT OR IGNORE INTO pending_payments
               (payment_id, amount, email, contact, user_id, tier, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (payment_id, amount, email, contact, user_id, tier, datetime.now().isoformat())
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.error(f"Database error: {e}")

def mark_approved(payment_id):
    try:
        con = sqlite3.connect("admin_payments.db")
        con.execute(
            "UPDATE pending_payments SET status = 'approved' WHERE payment_id = ?",
            (payment_id,)
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.error(f"Database error: {e}")

def get_pending_payments():
    try:
        con = sqlite3.connect("admin_payments.db")
        rows = con.execute(
            """SELECT payment_id, amount, email, contact, user_id, tier, received_at
               FROM pending_payments WHERE status = 'pending'
               ORDER BY received_at DESC LIMIT 20"""
        ).fetchall()
        con.close()
        return rows
    except Exception as e:
        logger.error(f"Database error: {e}")
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
