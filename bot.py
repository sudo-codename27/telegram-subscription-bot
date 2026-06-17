import logging
import os
import psycopg2
from psycopg2 import pool as pg_pool
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
from apscheduler.triggers.cron import CronTrigger
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

# Shared secret used to authenticate admin_bot.py -> bot.py API calls.
SECRET_API_KEY = os.environ.get("SECRET_API_KEY", "")

# PostgreSQL (Neon) connection string. Required on Railway.
# Format: postgresql://user:pass@host/dbname?sslmode=require
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise ValueError(
        "❌ DATABASE_URL not set!\n"
        "Set the Neon connection string as an environment variable:\n"
        "DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require"
    )

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
    "bronze": "🥉 Bronze",
    "gold":   "🥇 Gold",
}

CHANNEL_TO_TIER = {v: k for k, v in TIER_CHANNELS.items()}

# Razorpay configuration
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_BRONZE_PAGE = os.environ.get("RAZORPAY_BRONZE_PAGE", "")
RAZORPAY_GOLD_PAGE = os.environ.get("RAZORPAY_GOLD_PAGE", "")
WEBHOOK_PORT = int(os.environ.get("PORT", 8080))

RAZORPAY_PRICES = {
    "bronze": 24900,  # ₹249 for 2 months
    "gold": 50900,    # ₹509 for 2 months
}

RAZORPAY_PAYMENT_PAGES = {
    "bronze": RAZORPAY_BRONZE_PAGE,
    "gold": RAZORPAY_GOLD_PAGE,
}

USER_COOLDOWNS = {}
COOLDOWN_SECONDS = 3

# Pre-expiry reminder schedule: send a DM N days before subscription expires.
# Two reminders only (per user preference: 7 + 1, no spam).
REMINDER_DAYS_BEFORE = (7, 1)

# Schedule: midnight IST sweep + 9 AM IST reminders.
SCHEDULE_TIMEZONE = "Asia/Kolkata"

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  DATABASE (PostgreSQL / Neon)
# ─────────────────────────────────────────────

# Lazily-initialised connection pool. Neon is serverless and may close idle
# connections, so we keep the pool small and rely on getconn/putconn to
# transparently re-establish broken connections.
_db_pool = None
_db_pool_lock = threading.Lock()


def _build_pool():
    return pg_pool.SimpleConnectionPool(
        minconn=1,
        maxconn=5,
        dsn=DATABASE_URL,
    )


def _get_pool():
    global _db_pool
    if _db_pool is None:
        with _db_pool_lock:
            if _db_pool is None:
                _db_pool = _build_pool()
    return _db_pool


def _reset_pool():
    """Discard the current pool so the next getconn opens a fresh connection.

    Neon recycles idle connections aggressively; once one in our pool turns
    stale, every subsequent checkout against it fails until we rebuild.
    """
    global _db_pool
    with _db_pool_lock:
        old = _db_pool
        _db_pool = None
    if old is not None:
        try:
            old.closeall()
        except Exception:
            pass
    logger.warning("DB pool reset; next call will open a fresh Neon connection")


# Errors that mean "this socket is dead, throw the whole pool away".
_FATAL_DB_ERRORS = (
    psycopg2.OperationalError,
    psycopg2.InterfaceError,
)


class _PooledConnection:
    """Context manager that hands a connection back to the pool on exit.

    Also commits on successful exit and rolls back on exception. If the
    underlying connection is broken (e.g. Neon recycled it), we discard it
    and rebuild the pool so the next call picks up a fresh connection.
    """

    def __enter__(self):
        pool = _get_pool()
        self._pool = pool
        self.conn = pool.getconn()
        # Make sure stale transactions don't leak between checkouts.
        try:
            self.conn.rollback()
        except Exception:
            pass
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        broken = exc_type is not None and issubclass(exc_type, _FATAL_DB_ERRORS)

        if exc_type is None:
            try:
                self.conn.commit()
            except Exception as e:
                logger.error(f"DB commit failed: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                broken = isinstance(e, _FATAL_DB_ERRORS)
        else:
            try:
                self.conn.rollback()
            except Exception:
                # If we can't even rollback, the connection is dead.
                broken = True

        if broken:
            # Don't return this corpse to the pool; throw the pool out so
            # the next caller gets a fresh socket from Neon.
            try:
                self._pool.putconn(self.conn, close=True)
            except Exception:
                pass
            _reset_pool()
        else:
            try:
                self._pool.putconn(self.conn)
            except Exception as e:
                logger.warning(f"Returning conn to pool failed: {e}")
                _reset_pool()
        return False  # propagate exceptions


def db_connect():
    """Acquire a pooled PostgreSQL connection (use as context manager).

    Usage:
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute("SELECT 1")
    """
    return _PooledConnection()


def _with_db_retry(fn, *args, **kwargs):
    """Run a DB callable, retry once on a stale-connection error."""
    try:
        return fn(*args, **kwargs)
    except _FATAL_DB_ERRORS as e:
        logger.warning(f"DB connection lost during {fn.__name__}: {e}; retrying once")
        _reset_pool()
        return fn(*args, **kwargs)

# ─────────────────────────────────────────────
#  FLASK WEBHOOK SERVER
# ─────────────────────────────────────────────

webhook_app = Flask(__name__)
bot_app = None


def grant_subscription_sync(user_id: int, tier: str, payment_id: str, paid_amount_rupees: int, status: str):
    """Grant a subscription and send the invite link, from a sync (Flask) context.

    Shared by the Razorpay webhook and the /api/grant_subscription endpoint so
    both paths behave identically. Returns a (body_dict, http_status) tuple.

    Body shape so the admin bot can show honest status:
        {"status": "success", "invite_sent": true}                # full happy path
        {"status": "success", "invite_sent": false, "error": "…"} # subscription saved, link failed
        {"status": "already_processed"}                            # duplicate webhook
    """
    if charge_id_already_used(payment_id):
        logger.warning(f"Duplicate payment {payment_id}")
        return {"status": "already_processed"}, 200

    log_payment(user_id, tier, payment_id, paid_amount_rupees, status)

    existing = get_active_subscription(user_id, tier)
    if existing:
        old_expiry = datetime.fromisoformat(existing[1])
        base_date = max(old_expiry, datetime.now())
    else:
        base_date = datetime.now()

    expiry = base_date + timedelta(days=TIER_DURATION_DAYS[tier])
    save_subscription(user_id, tier, expiry, payment_id)

    logger.info(f"✅ Subscription granted: user {user_id}, tier {tier}, payment {payment_id} ({status})")

    invite_sent = False
    invite_error = None
    if bot_app:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            invite_sent, invite_error = loop.run_until_complete(
                send_invite_link(user_id, tier, expiry, payment_id)
            )
        finally:
            try:
                loop.close()
            except Exception:
                pass

    body = {"status": "success", "invite_sent": bool(invite_sent)}
    if not invite_sent and invite_error:
        body["error"] = invite_error
    return body, 200


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
            
            body, status_code = grant_subscription_sync(
                user_id, tier, payment_id, amount // 100, "razorpay_captured"
            )
            return jsonify(body), status_code
        
        else:
            logger.info(f"Received Razorpay event: {event_type}")
            return jsonify({"status": "ignored"}), 200
    
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@webhook_app.route('/api/grant_subscription', methods=['POST'])
def api_grant_subscription():
    """Grant a subscription on behalf of the admin bot (manual approval flow).

    admin_bot.py calls this after an admin approves a Razorpay payment and
    replies with `<user_id> <tier>`. Authenticated via SECRET_API_KEY.
    """
    try:
        data = request.get_json(silent=True) or {}

        # 1. Authenticate.
        provided_key = data.get("api_key", "")
        if not SECRET_API_KEY or not hmac.compare_digest(str(provided_key), SECRET_API_KEY):
            logger.warning("Unauthorized /api/grant_subscription request (bad or missing api_key)")
            return jsonify({"error": "Unauthorized"}), 401

        # 2. Validate input.
        user_id = data.get("user_id")
        tier = data.get("tier")
        payment_id = data.get("payment_id")

        if user_id is None or tier is None:
            return jsonify({"error": "Missing user_id or tier"}), 400

        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid user_id"}), 400

        tier = str(tier).lower()
        if tier not in TIER_DURATION_DAYS:
            return jsonify({"error": f"Invalid tier: {tier}"}), 400

        if not payment_id:
            # Synthesize a stable id so duplicate-detection still works.
            payment_id = f"manual_{user_id}_{tier}_{int(time())}"

        # 3-6. Grant + send invite link (shared logic).
        paid_amount_rupees = RAZORPAY_PRICES.get(tier, 0) // 100
        body, status_code = grant_subscription_sync(
            user_id, tier, payment_id, paid_amount_rupees, "razorpay_admin_approved"
        )
        return jsonify(body), status_code

    except Exception as e:
        logger.error(f"/api/grant_subscription error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@webhook_app.route('/api/resend_invite', methods=['POST'])
def api_resend_invite():
    """Re-send a fresh channel invite link to an existing subscriber.

    Called by admin_bot.py when an admin runs /resend_invite on the admin bot.
    Validates that the user has an active subscription for the requested tier
    (so we don't accidentally invite a non-paying user) and then reuses
    send_invite_link() so the message format matches the post-payment one.

    Authenticated via SECRET_API_KEY (same shared secret as /api/grant_subscription).
    """
    try:
        data = request.get_json(silent=True) or {}

        provided_key = data.get("api_key", "")
        if not SECRET_API_KEY or not hmac.compare_digest(str(provided_key), SECRET_API_KEY):
            logger.warning("Unauthorized /api/resend_invite request (bad or missing api_key)")
            return jsonify({"error": "Unauthorized"}), 401

        user_id = data.get("user_id")
        tier = data.get("tier")

        if user_id is None or tier is None:
            return jsonify({"error": "Missing user_id or tier"}), 400

        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid user_id"}), 400

        tier = str(tier).lower()
        if tier not in TIER_CHANNELS:
            return jsonify({"error": f"Invalid tier: {tier}"}), 400

        sub = get_active_subscription(user_id, tier)
        if not sub:
            return jsonify({
                "status": "no_active_subscription",
                "error": f"User {user_id} has no active {tier} subscription",
            }), 404

        try:
            expiry = datetime.fromisoformat(sub[1])
        except Exception:
            return jsonify({"error": "Stored expiry is not a valid ISO timestamp"}), 500

        payment_id = f"resend_{user_id}_{tier}_{int(time())}"

        if not bot_app:
            return jsonify({"error": "Bot app not initialized"}), 503

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            invite_sent, invite_error = loop.run_until_complete(
                send_invite_link(user_id, tier, expiry, payment_id)
            )
        finally:
            try:
                loop.close()
            except Exception:
                pass

        body = {
            "status": "success" if invite_sent else "failed",
            "invite_sent": bool(invite_sent),
            "expiry": expiry.isoformat(),
        }
        if not invite_sent and invite_error:
            body["error"] = invite_error
        return jsonify(body), (200 if invite_sent else 502)

    except Exception as e:
        logger.error(f"/api/resend_invite error: {e}", exc_info=True)
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
            "grant_subscription": "/api/grant_subscription",
            "resend_invite": "/api/resend_invite",
            "health": "/health"
        }
    }), 200

# ─────────────────────────────────────────────
#  DATABASE FUNCTIONS
# ─────────────────────────────────────────────

def init_db():
    """Create tables if they don't exist. Idempotent — safe to call on every boot."""
    with db_connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id     BIGINT,
                    tier        TEXT,
                    expiry      TEXT,
                    charge_id   TEXT,
                    started_at  TEXT,
                    PRIMARY KEY (user_id, tier)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments_log (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT,
                    tier        TEXT,
                    charge_id   TEXT UNIQUE,
                    stars       INTEGER,
                    paid_at     TEXT,
                    status      TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blacklist (
                    user_id     BIGINT PRIMARY KEY,
                    reason      TEXT,
                    banned_at   TEXT,
                    banned_by   BIGINT
                )
            """)
            # last_reminder_day prevents duplicate DMs in the same window
            # if the scheduler fires twice. Cheap text column, nullable.
            cur.execute("""
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS last_reminder_day INTEGER
            """)
    logger.info("✅ Database tables ready (PostgreSQL/Neon)")

def get_active_subscription(user_id: int, tier: str):
    try:
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """SELECT tier, expiry, started_at FROM subscriptions
                       WHERE user_id = %s AND tier = %s AND expiry > %s""",
                    (user_id, tier, datetime.now().isoformat()),
                )
                return cur.fetchone()
    except Exception as e:
        logger.error(f"Database error in get_active_subscription: {e}")
        return None

def get_all_active_subscriptions(user_id: int):
    try:
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """SELECT tier, expiry, started_at FROM subscriptions
                       WHERE user_id = %s AND expiry > %s""",
                    (user_id, datetime.now().isoformat()),
                )
                return cur.fetchall()
    except Exception as e:
        logger.error(f"Database error in get_all_active_subscriptions: {e}")
        return []

def save_subscription(user_id: int, tier: str, expiry: datetime, charge_id: str):
    try:
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """INSERT INTO subscriptions
                       (user_id, tier, expiry, charge_id, started_at, last_reminder_day)
                       VALUES (%s, %s, %s, %s, %s, NULL)
                       ON CONFLICT (user_id, tier) DO UPDATE SET
                         expiry            = EXCLUDED.expiry,
                         charge_id         = EXCLUDED.charge_id,
                         started_at        = EXCLUDED.started_at,
                         last_reminder_day = NULL""",
                    (user_id, tier, expiry.isoformat(), charge_id, datetime.now().isoformat()),
                )
    except Exception as e:
        logger.error(f"Database error in save_subscription: {e}")

def delete_subscription(user_id: int, tier: str):
    try:
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "DELETE FROM subscriptions WHERE user_id = %s AND tier = %s",
                    (user_id, tier),
                )
    except Exception as e:
        logger.error(f"Database error in delete_subscription: {e}")

def get_expired_subscriptions():
    try:
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT user_id, tier FROM subscriptions WHERE expiry <= %s",
                    (datetime.now().isoformat(),),
                )
                return cur.fetchall()
    except Exception as e:
        logger.error(f"Database error in get_expired_subscriptions: {e}")
        return []


def get_subscriptions_expiring_in(days: int):
    """Rows whose expiry falls in the [now+days-12h, now+days+12h] window.

    A 24-hour window sized around the target day means we tolerate the cron
    firing slightly before/after midnight without missing or double-firing.
    """
    try:
        target_low = (datetime.now() + timedelta(days=days, hours=-12)).isoformat()
        target_high = (datetime.now() + timedelta(days=days, hours=12)).isoformat()
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """SELECT user_id, tier, expiry, last_reminder_day
                       FROM subscriptions
                       WHERE expiry >= %s AND expiry <= %s""",
                    (target_low, target_high),
                )
                return cur.fetchall()
    except Exception as e:
        logger.error(f"Database error in get_subscriptions_expiring_in: {e}")
        return []


def mark_reminder_sent(user_id: int, tier: str, days_before: int):
    try:
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """UPDATE subscriptions SET last_reminder_day = %s
                       WHERE user_id = %s AND tier = %s""",
                    (days_before, user_id, tier),
                )
    except Exception as e:
        logger.error(f"Database error in mark_reminder_sent: {e}")

def charge_id_already_used(charge_id: str) -> bool:
    def _query():
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT id FROM payments_log WHERE charge_id = %s",
                    (charge_id,),
                )
                return cur.fetchone() is not None

    try:
        return _with_db_retry(_query)
    except Exception as e:
        logger.error(f"Database error in charge_id_already_used: {e}")
        # Conservative default: if we can't tell, assume it's a fresh payment.
        # The unique constraint on payments_log.charge_id is the real backstop.
        return False

def log_payment(user_id: int, tier: str, charge_id: str, stars: int, status: str):
    try:
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """INSERT INTO payments_log
                       (user_id, tier, charge_id, stars, paid_at, status)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (charge_id) DO NOTHING""",
                    (user_id, tier, charge_id, stars, datetime.now().isoformat(), status),
                )
    except Exception as e:
        logger.warning(f"Could not log payment: {e}")

def is_blacklisted(user_id: int) -> bool:
    try:
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM blacklist WHERE user_id = %s",
                    (user_id,),
                )
                return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"Database error in is_blacklisted: {e}")
        return False

def add_to_blacklist(user_id: int, reason: str, banned_by: int):
    try:
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """INSERT INTO blacklist (user_id, reason, banned_at, banned_by)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET
                         reason    = EXCLUDED.reason,
                         banned_at = EXCLUDED.banned_at,
                         banned_by = EXCLUDED.banned_by""",
                    (user_id, reason, datetime.now().isoformat(), banned_by),
                )
    except Exception as e:
        logger.error(f"Database error in add_to_blacklist: {e}")

def remove_from_blacklist(user_id: int):
    try:
        with db_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "DELETE FROM blacklist WHERE user_id = %s",
                    (user_id,),
                )
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
        status = "🚨 Expires today!"

    return (
        f"{emoji} *{tier.title()} — Active*\n"
        f"   Started : {datetime.fromisoformat(started_str).strftime('%d %b %Y')}\n"
        f"   Expires : {exp_dt.strftime('%d %b %Y')}\n"
        f"   Status  : {status}"
    )

async def send_invite_link(user_id: int, tier: str, expiry: datetime, payment_id: str):
    """Send channel invite link after successful payment.

    Returns (sent: bool, error_message: Optional[str]) so callers can report
    honest status in admin notifications.
    """
    if not bot_app:
        msg = "Bot app not initialized"
        logger.error(msg)
        return False, msg

    try:
        link = await bot_app.bot.create_chat_invite_link(
            chat_id=TIER_CHANNELS[tier],
            creates_join_request=True,
            expire_date=datetime.now() + timedelta(minutes=20),
            # NOTE: member_limit cannot be combined with creates_join_request=True;
            # Telegram returns 400 "Member limit can't be specified for links
            # requiring administrator approval". The join-request handler is the
            # real gatekeeper — see approve_join_request() — so the link being
            # reusable for ~20 minutes is fine.
            name=f"sub:{tier}:{user_id}",
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
                f"*(Valid for 20 minutes — only you will be approved)*\n\n"
                f"{link.invite_link}\n\n"
                f"Use /membership anytime to check your status."
            ),
            parse_mode="Markdown"
        )

        logger.info(f"Invite link sent to user {user_id} ({tier})")
        return True, None

    except Exception as e:
        err = str(e)
        logger.error(f"Failed to send invite link to {user_id} ({tier}): {err}")
        return False, err

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
        "Get access to premium content by choosing a plan below.\n\n"
        "Payment is made with ⭐*Telegram Stars* or 💳*Razorpay*\n\n"
        "🥉 *Bronze Tier*\n" 
        "⭐ 100 or ₹249 *(for 2 months)*\n\n"
        "🥇 *Gold Tier*\n"
        "⭐ 250 or ₹509 *(for 2 months)*\n\n"
        "👇 *SELECT YOUR DESIRE* 🤤🮦",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

@check_user_access
async def membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    subs = get_all_active_subscriptions(user_id)

    if not subs:
        await update.message.reply_text(
            "❌ 🙅‍♂️ *You have no active subscriptions.*\n\n"
            "Use /start to get access to exclusive content!",
            parse_mode="Markdown",
        )
        return

    lines = [format_subscription_card(tier, expiry, started) for tier, expiry, started in subs]
    await update.message.reply_text(
        "📱 *Your Subscriptions:*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown",
    )

@check_user_access
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⭐ *Telegram Stars Balance*\n\n"
        "To check your Stars balance open Telegram and go to:\n"
        "*Settings → My Stars*\n\n"
        "Your balance is shown at the top of that screen.\n\n"
        "📜 *Our prices:*\n"
        "🥉 *Bronze Tier*\n" 
        "⭐ 100 or ₹249 *(for 2 months)*\n\n"
        "🥇 *Gold Tier*\n"
        "⭐ 250 or ₹509 *(for 2 months)*\n\n"
        "Use /start when you're ready!",
        parse_mode="Markdown",
    )

async def select_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE, tier: str):
    keyboard = [
        [InlineKeyboardButton("⭐ *TELEGRAM STARS*", callback_data=f"pay_stars_{tier}")],
        [InlineKeyboardButton("💳 *RAZORPAY* (UPI/Card/NetBanking)", callback_data=f"pay_razorpay_{tier}")],
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
            "Get access to premium content by choosing a plan below.\n\n"
            "Payment is made with ⭐*Telegram Stars* or 💳*Razorpay*\n\n"
            "🥉 *Bronze Tier*\n" 
            "⭐ 100 or ₹249 *(for 2 months)*\n\n"
            "🥇 *Gold Tier*\n"
            "⭐ 250 or ₹509 *(for 2 months)*\n\n"
            "👇 *SELECT YOUR DESIRE* 🤤🮦",
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
                    f"♉️ *You already have an active {tier.title()} subscription!*\n\n"
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
                "❌ Failed to generate payment invoice. Please try again or contact support.\n"
                "Admin: *@desiremusings*"
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
            "📢❗🚨 This payment was already processed.\n"
            "ℹ️ Use /membership to check your subscription status."
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
            # See note in send_invite_link() — member_limit is incompatible
            # with creates_join_request=True (Telegram 400). approve_join_request
            # is the real gatekeeper.
            name=f"sub:{tier}:{user_id}",
        )

        await update.message.reply_text(
            f"✅ *Payment Successful! Thank you!*\n\n"
            f"⭐ Stars paid : *{stars_paid}*\n"
            f"📦 Plan       : *{tier.title()}*\n"
            f"📅 Active until: *{expiry.strftime('%d %b %Y')}*\n"
            f"🔖 Ref        : `{charge_id}`\n\n"
            f"👇 Tap the link and press *'Request to Join'*\n"
            f"*(Valid for 20 minutes — only you will be approved)*\n\n"
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
        await update.message.reply_text("❋🎻🚿⛔️ Invalid user ID. Must be a number.")
    except Exception as e:
        logger.error(f"Error in check command: {e}")
        await update.message.reply_text(f"❔ Error: {e}")

# ─────────────────────────────────────────────
#  EXPIRY SWEEP & PRE-EXPIRY REMINDERS
# ─────────────────────────────────────────────

async def expiry_sweep_job(application):
    """Daily midnight-IST sweep: kick expired users, DM them, delete row.

    Strict 0-day grace per user preference. Row is deleted (not marked
    inactive) to keep Neon free-tier footprint tiny.
    """
    logger.info("Running daily expiry sweep…")
    expired = get_expired_subscriptions()

    if not expired:
        logger.info("No expired subscriptions today.")
        return

    for user_id, tier in expired:
        channel_id = TIER_CHANNELS.get(tier)
        if not channel_id:
            # Tier renamed/removed — just clean up the orphan row.
            delete_subscription(user_id, tier)
            continue
        try:
            # ban + unban = "kick" without long-term banishment, so they can
            # rejoin if they pay again later.
            await application.bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
            await application.bot.unban_chat_member(chat_id=channel_id, user_id=user_id)

            try:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"⏰ *Your {tier.title()} subscription has expired.*\n\n"
                        f"You have been removed from the {tier.title()} channel.\n\n"
                        f"Use /start to renew and get access again! 🔄"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as dm_err:
                # User may have blocked the bot — kick still succeeded.
                logger.warning(f"Could not DM expired user {user_id}: {dm_err}")

            logger.info(f"Kicked expired user {user_id} from {tier}")
        except Exception as e:
            logger.warning(f"Could not kick user {user_id} from {tier}: {e}")
        finally:
            delete_subscription(user_id, tier)


async def reminder_job(application):
    """Daily 9 AM IST job: send 7-day and 1-day pre-expiry reminders.

    Iterates the (7, 1) schedule. last_reminder_day on the row prevents
    re-sending the same reminder if the cron fires twice or the bot reboots.
    """
    logger.info("Running pre-expiry reminder pass…")
    sent_total = 0

    for days_before in REMINDER_DAYS_BEFORE:
        rows = get_subscriptions_expiring_in(days_before)

        for user_id, tier, expiry_str, last_reminder in rows:
            # Don't re-send reminders we've already sent.
            if last_reminder is not None and last_reminder <= days_before:
                continue
            try:
                exp_dt = datetime.fromisoformat(expiry_str)
            except Exception:
                continue

            try:
                if days_before == 1:
                    msg = (
                        f"⚠️ *{tier.title()} subscription expires tomorrow*\n\n"
                        f"📅 Expires: *{exp_dt.strftime('%d %b %Y')}*\n\n"
                        f"Renew now via /start to avoid losing access. 🚀"
                    )
                else:
                    msg = (
                        f"🔔 *{tier.title()} subscription expires in {days_before} days*\n\n"
                        f"📅 Expires: *{exp_dt.strftime('%d %b %Y')}*\n\n"
                        f"Beat the rush — tap /start to renew when you're ready."
                    )
                await application.bot.send_message(
                    chat_id=user_id,
                    text=msg,
                    parse_mode="Markdown",
                )
                mark_reminder_sent(user_id, tier, days_before)
                sent_total += 1
            except Exception as e:
                # Most common: user blocked the bot.
                logger.info(f"Reminder skipped for {user_id} ({tier}, T-{days_before}d): {e}")

    logger.info(f"Reminder pass done; {sent_total} DM(s) sent.")

# ─────────────────────────────────────────────
#  RUN WEBHOOK SERVER
# ─────────────────────────────────────────────

def run_webhook_server():
    global WEBHOOK_PORT
    logger.info(f"Starting webhook server on port {WEBHOOK_PORT}...")
    webhook_app.run(host='0.0.0.0', port=WEBHOOK_PORT, debug=False, use_reloader=False)

# ─────────────────────────────────────────────
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
        global WEBHOOK_PORT
        await application.bot.set_my_commands([
            BotCommand("start", "🚀*SUBSCRIPTION PLAN*"),
            BotCommand("membership", "✅*CHECK ACTIVE SUBSCRIPTION*"),
            BotCommand("balance", "*STEPS TO CHECK ⭐-BALANCE*"),
        ])

        scheduler = AsyncIOScheduler(timezone=SCHEDULE_TIMEZONE)

        # Midnight IST: kick expired subscribers.
        scheduler.add_job(
            expiry_sweep_job,
            trigger=CronTrigger(hour=0, minute=0, timezone=SCHEDULE_TIMEZONE),
            args=[application],
            id="expiry_sweep",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        # 9 AM IST: nudge users 7 days and 1 day before expiry.
        scheduler.add_job(
            reminder_job,
            trigger=CronTrigger(hour=9, minute=0, timezone=SCHEDULE_TIMEZONE),
            args=[application],
            id="reminder_job",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        scheduler.start()
        logger.info(
            "✅ Scheduled jobs registered: expiry_sweep@00:00 IST, reminder_job@09:00 IST"
        )

        logger.info("✅ Bot started successfully!")
        logger.info(f"✅ Webhook server running on port {WEBHOOK_PORT}")

    bot_app.post_init = post_init

    webhook_thread = threading.Thread(target=run_webhook_server, daemon=True)
    webhook_thread.start()
    
    logger.info("🚀 Bot + Webhook server running... Press Ctrl+C to stop.")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
