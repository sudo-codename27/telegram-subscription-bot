# 🤖 Telegram Subscription Bot

A Telegram bot that manages paid subscriptions with **Telegram Stars** and **Razorpay** payment integration.

## ✨ Features

- 💳 **Dual Payment Options**: Telegram Stars & Razorpay (UPI/Card/NetBanking)
- 🔐 **Secure Webhooks**: HMAC signature verification for Razorpay
- 👥 **Multi-tier Subscriptions**: Bronze & Gold tiers
- ⏰ **Auto-expiry Management**: Daily cleanup of expired subscriptions
- 🛡️ **Admin Controls**: Ban/unban users, check subscription status
- 📊 **Payment Logging**: Complete audit trail of all transactions
- 🚫 **Anti-fraud**: Duplicate payment detection
- 🔐 **Two-Bot Architecture**: Separate admin bot for payment approvals
- 🚂 **Railway Ready**: Configured for easy cloud deployment

## 🏗️ Architecture

```
Razorpay Payment
      ↓
Admin Bot ← Webhook
      ↓
📱 Telegram Notification to Admin
      ↓
Admin clicks "✅ Approve" and replies `<user_id> <tier>`
      ↓
Admin Bot → POST /api/grant_subscription (Secret API key) → Main Bot
      ↓
Main Bot grants subscription
      ↓
User gets invite link!
```

## 📋 Prerequisites

- Python 3.11+
- Two Telegram Bot Tokens (Main bot + Admin bot from [@BotFather](https://t.me/botfather))
- Razorpay Account (for payment pages)
- Railway Account (for deployment) **OR** Ngrok (for local testing)

---

## 🔐 Environment Variables

> **Never commit real secrets.** `.env` and `.env.admin` are git-ignored and
> are only used for local development. On Railway, set every value below in the
> service's **Variables** tab — do **not** upload a `.env` file.

### Main bot (`bot.py`)

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ | Main bot token from @BotFather. |
| `ADMIN_USER_ID` | ✅ | Your numeric Telegram user ID (admin commands). |
| `SECRET_API_KEY` | ✅ | Shared secret that authenticates the admin bot's `/api/grant_subscription` calls. **Must be identical** in both services. |
| `RAZORPAY_BRONZE_PAGE` | ✅ | Razorpay payment page URL for the Bronze tier. |
| `RAZORPAY_GOLD_PAGE` | ✅ | Razorpay payment page URL for the Gold tier. |
| `RAZORPAY_WEBHOOK_SECRET` | optional | HMAC secret to verify Razorpay webhooks (if the main bot receives them directly). |
| `PORT` | auto | Web server port. Railway injects this automatically. |
| `DB_PATH` | optional | Path to subscriptions database. Defaults to `subscriptions.db`. On Railway with volume, set to `/data/subscriptions.db`. |

### Admin bot (`admin_bot.py`)

| Variable | Required | Description |
|----------|----------|-------------|
| `ADMIN_BOT_TOKEN` | ✅ | Admin bot token from @BotFather (a second bot). |
| `ADMIN_USER_ID` | ✅ | Your numeric Telegram user ID. |
| `SECRET_API_KEY` | ✅ | **Same value** as the main bot's `SECRET_API_KEY`. |
| `MAIN_BOT_API_URL` | ✅ | Public URL of the main bot service, e.g. `https://your-main-bot.up.railway.app`. |
| `RAZORPAY_WEBHOOK_SECRET` | ✅ | Same value configured in the Razorpay dashboard. |
| `PORT` | auto | Web server port. Railway injects this; falls back to `ADMIN_PORT`, then `8081` locally. |

---

## 🚂 Railway Deployment (Recommended for Production)

### 1️⃣ Deploy to Railway

1. **Create Railway Account**: https://railway.app/
2. **Create New Project** → **Deploy from GitHub**
3. Select repository: `ankit-mandal/final_telegram`
4. Railway will auto-detect Python and deploy!

### 2️⃣ Configure Main Bot Service

In Railway dashboard → Your service → **Variables** tab (placeholders — use your real values):

```bash
BOT_TOKEN=your_main_bot_token_here
ADMIN_USER_ID=your_telegram_user_id
SECRET_API_KEY=generate_random_secure_key_here
RAZORPAY_BRONZE_PAGE=https://rzp.io/rzp/YOUR_BRONZE_PAGE
RAZORPAY_GOLD_PAGE=https://rzp.io/rzp/YOUR_GOLD_PAGE
RAZORPAY_WEBHOOK_SECRET=whsec_your_webhook_secret
```

> You do **not** need to set `PORT` — Railway provides it automatically.
> Under **Settings → Networking**, click **Generate Domain** to get a public URL
> like `https://your-main-bot.up.railway.app`. Copy it — the admin bot needs it.

### 3️⃣ Deploy Admin Bot (Second Service)

1. Click **"New"** → **"GitHub Repo"**
2. Select **same repository**: `ankit-mandal/final_telegram`
3. Go to **Settings** → **Custom Start Command**:
   ```
   python admin_bot.py
   ```
4. Add environment variables (placeholders — use your real values):

```bash
ADMIN_BOT_TOKEN=your_admin_bot_token_here
ADMIN_USER_ID=your_telegram_user_id
SECRET_API_KEY=same_as_main_bot_secret_key
MAIN_BOT_API_URL=https://your-main-bot.up.railway.app
RAZORPAY_WEBHOOK_SECRET=same_as_main_bot_webhook_secret
```

> The admin web server binds to Railway's `$PORT` automatically. Under
> **Settings → Networking**, **Generate Domain** to get a URL like
> `https://your-admin-bot.up.railway.app`.

### 4️⃣ Configure Razorpay Webhook

1. Go to [Razorpay Dashboard](https://dashboard.razorpay.com/)
2. Navigate to **Settings → Webhooks**
3. **Webhook URL**: `https://your-admin-bot.up.railway.app/webhook/razorpay`
4. **Active Events**: Select `payment.captured`
5. **Secret**: Use the same value as `RAZORPAY_WEBHOOK_SECRET`
6. Save

### 5️⃣ Persisting subscription data (Railway volume)

The main bot's `subscriptions.db` holds paid subscriptions, so it must survive
redeploys. On the **main bot service**:

1. **Settings → Volumes → New Volume**, mount path: `/data`
2. **Variables** tab, add: `DB_PATH=/data/subscriptions.db`
3. Redeploy.

A subscriptions DB is only KBs–MBs, so it fits comfortably within the Free
tier's **0.5 GB volume** allowance.

> The admin bot's `admin_payments.db` is a transient approval queue and does
> **not** need a volume — it repopulates from incoming Razorpay webhooks.

### 6️⃣ Test Your Deployment

1. Message your main bot: `/start`
2. Select a tier and payment method
3. Complete a test payment
4. Admin bot notifies you → click **✅ Approve** → reply `<user_id> <tier>`
5. The user receives their invite link!

---

## 💻 Local Development (Optional)

### 1️⃣ Clone Repository

```bash
git clone https://github.com/ankit-mandal/final_telegram.git
cd final_telegram
```

### 2️⃣ Create Virtual Environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### 3️⃣ Install Dependencies

```bash
pip install -r requirements.txt
```

### 4️⃣ Configure Environment

Create `.env` file:

```bash
BOT_TOKEN=your_main_bot_token
ADMIN_USER_ID=your_user_id
SECRET_API_KEY=your_secret_key
RAZORPAY_BRONZE_PAGE=https://rzp.io/rzp/bronze_page
RAZORPAY_GOLD_PAGE=https://rzp.io/rzp/gold_page
RAZORPAY_WEBHOOK_SECRET=whsec_secret
PORT=8080
```

Create `.env.admin` file:

```bash
ADMIN_BOT_TOKEN=your_admin_bot_token
ADMIN_USER_ID=your_user_id
SECRET_API_KEY=same_as_main_bot
MAIN_BOT_API_URL=http://localhost:8080
RAZORPAY_WEBHOOK_SECRET=same_as_main_bot
ADMIN_PORT=8081
```

> Locally, set `PORT` (main bot) and `ADMIN_PORT` (admin bot) to different
> values so the two Flask servers don't collide. On Railway each service gets
> its own `$PORT`, so this is handled automatically.

### 5️⃣ Start with Ngrok (for local testing)

**Terminal 1 - Main Bot:**
```bash
python bot.py
```

**Terminal 2 - Admin Bot:**
```bash
python admin_bot.py
```

**Terminal 3 - Ngrok for Admin Bot:**
```bash
ngrok http 8081
```

Update Razorpay webhook URL to ngrok URL.

---

## 💬 How It Works

### User Flow:
1. User opens main bot
2. Selects a subscription plan (Bronze/Gold)
3. Chooses payment method (Telegram Stars or Razorpay)
4. Completes payment
5. Gets invite link automatically!

### Admin Flow (Razorpay Payments):
1. Admin bot receives webhook from Razorpay
2. Sends you a Telegram message with payment details
3. You click **✅ Approve**
4. Reply with: `<user_id> <tier>` (e.g., `123456789 bronze`)
5. Admin bot calls the main bot's `/api/grant_subscription` endpoint
6. User gets invite link!

---

## 📚 Bot Commands

### Main Bot

**User Commands:**
- `/start` - Main menu with subscription plans
- `/membership` - View active subscriptions
- `/balance` - How to check Telegram Stars balance

**Admin Commands:**
- `/ban <user_id> <reason>` - Ban a user
- `/unban <user_id>` - Unban a user
- `/check <user_id>` - Check user subscription status

### Admin Bot

- `/start` - Welcome message
- `/pending` - View pending Razorpay payments
- Approve/Reject buttons on payment notifications

---

## 💰 Pricing Configuration

Edit these in `bot.py`:

```python
# Telegram Stars Prices
TIER_PRICES = {
    "bronze": 100,  # 100 stars
    "gold":   250,  # 250 stars
}

# Razorpay Prices (in paise)
RAZORPAY_PRICES = {
    "bronze": 24900,   # ₹249.00
    "gold": 50900,     # ₹509.00
}

# Duration (in days)
TIER_DURATION_DAYS = {
    "bronze": 60,  # 2 months
    "gold":   60,  # 2 months
}
```

---

## 🗄️ Database Schema

### Main Bot: `subscriptions.db`

**`subscriptions` table:**
| Column      | Type    | Description                     |
|-------------|---------|---------------------------------|
| user_id     | INTEGER | Telegram user ID                |
| tier        | TEXT    | Subscription tier (bronze/gold) |
| expiry      | TEXT    | ISO format expiry timestamp     |
| charge_id   | TEXT    | Payment charge ID               |
| started_at  | TEXT    | ISO format start timestamp      |

**`payments_log` table:**
| Column    | Type    | Description                        |
|-----------|---------|------------------------------------|
| id        | INTEGER | Auto-increment primary key         |
| user_id   | INTEGER | Telegram user ID                   |
| tier      | TEXT    | Subscription tier                  |
| charge_id | TEXT    | Unique payment ID                  |
| stars     | INTEGER | Amount paid (stars or rupees)      |
| paid_at   | TEXT    | ISO format payment timestamp       |
| status    | TEXT    | Payment status (success/captured)  |

**`blacklist` table:**
| Column    | Type    | Description              |
|-----------|---------|-------------------------|
| user_id   | INTEGER | Banned user ID           |
| reason    | TEXT    | Ban reason               |
| banned_at | TEXT    | ISO format ban timestamp |
| banned_by | INTEGER | Admin user ID            |

### Admin Bot: `admin_payments.db`

**`pending_payments` table:**
| Column      | Type    | Description                |
|-------------|---------|----------------------------|
| id          | INTEGER | Auto-increment primary key |
| payment_id  | TEXT    | Razorpay payment ID        |
| amount      | INTEGER | Amount in paise            |
| email       | TEXT    | Customer email             |
| contact     | TEXT    | Customer phone             |
| user_id     | INTEGER | Telegram user ID (if any)  |
| tier        | TEXT    | Selected tier (if any)     |
| received_at | TEXT    | Timestamp                  |
| status      | TEXT    | pending/approved           |

---

## 🔐 Security Features

- ✅ Webhook signature verification (HMAC-SHA256)
- ✅ Duplicate payment detection
- ✅ Bot account blocking
- ✅ User blacklist system
- ✅ Rate limiting on button presses
- ✅ Single-use invite links with expiry
- ✅ Secret API key for bot-to-bot communication (`/api/grant_subscription`)
- ✅ Separate admin bot (never exposed to users)
- ✅ `.env` file protection via `.gitignore`

---

## 🛠️ Troubleshooting

### Bot doesn't respond

- Check if bot is running in Railway logs
- Verify `BOT_TOKEN` in environment variables
- Check bot token with [@BotFather](https://t.me/botfather)

### Razorpay webhook not working

- Ensure **admin bot** is deployed on Railway
- Check webhook URL points to admin bot service
- Verify `RAZORPAY_WEBHOOK_SECRET` matches Razorpay dashboard
- Check Railway logs for admin bot

### Admin bot not notifying

- Check `ADMIN_USER_ID` is correct
- Start conversation with admin bot first (`/start`)
- Check Railway logs for errors

### "Failed to grant subscription" after approving

- Ensure `SECRET_API_KEY` is **identical** in both services (a mismatch returns `401 Unauthorized`)
- Check `MAIN_BOT_API_URL` points to the main bot's public Railway URL
- Verify both services are running in the Railway dashboard

---

## 📁 Project Structure

```
final_telegram/
│
├── bot.py                 # Main user-facing bot (+ /api/grant_subscription)
├── admin_bot.py           # Admin payment approval bot
├── requirements.txt       # Python dependencies
├── Procfile               # Railway start command
├── runtime.txt            # Python version for Railway
├── railway.json           # Railway configuration
├── .gitignore             # Git ignore rules
├── README.md              # This file
├── subscriptions.db       # Main bot database (auto-created)
└── admin_payments.db      # Admin bot database (auto-created)
```

---

## 🔄 Railway vs Local Development

| Feature | Railway | Local (Ngrok) |
|---------|---------|---------------|
| **Cost** | Free tier available | Free |
| **Uptime** | 24/7 | Only when running |
| **Setup** | Easy (one-click) | Manual (4 terminals) |
| **Webhooks** | Permanent URL | Temporary URL |
| **Suitable for** | Production | Testing |

---

## 📝 License

MIT License - feel free to use and modify!

## 🤝 Contributing

Pull requests welcome! For major changes, please open an issue first.

## 📧 Support

For issues or questions:
- Open a GitHub issue
- Contact: midnightmusings27@gmail.com

## 🙏 Acknowledgments

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [Razorpay](https://razorpay.com/)
- [Railway](https://railway.app/)

---

🚀 **Happy Deploying!**
