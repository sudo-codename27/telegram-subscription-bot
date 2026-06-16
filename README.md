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
Admin Bot (Port 5001) ← Webhook
      ↓
📱 Telegram Notification to Admin
      ↓
Admin clicks "✅ Approve"
      ↓
Admin Bot → Secure API → Main Bot (Port 5000)
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

## 🚂 Railway Deployment (Recommended for Production)

### 1️⃣ Deploy to Railway

1. **Create Railway Account**: https://railway.app/
2. **Create New Project** → **Deploy from GitHub**
3. Select repository: `ankit-mandal/final_telegram`
4. Railway will auto-detect Python and deploy!

### 2️⃣ Configure Main Bot Service

In Railway dashboard → Your service → **Variables** tab:

```bash
BOT_TOKEN=your_main_bot_token_here
ADMIN_USER_ID=your_telegram_user_id
RAZORPAY_BRONZE_PAGE=https://rzp.io/rzp/YOUR_BRONZE_PAGE
RAZORPAY_GOLD_PAGE=https://rzp.io/rzp/YOUR_GOLD_PAGE
RAZORPAY_WEBHOOK_SECRET=whsec_your_webhook_secret
SECRET_API_KEY=generate_random_secure_key_here
PORT=5000
```

**Railway will provide you a URL like:** `https://your-main-bot.up.railway.app`

### 3️⃣ Deploy Admin Bot (Second Service)

1. Click **"New"** → **"GitHub Repo"**
2. Select **same repository**: `ankit-mandal/final_telegram`
3. Go to **Settings** → **Start Command**:
   ```
   python admin_bot.py
   ```
4. Add environment variables:

```bash
ADMIN_BOT_TOKEN=your_admin_bot_token_here
ADMIN_USER_ID=your_telegram_user_id
ADMIN_PORT=5001
SECRET_API_KEY=same_as_main_bot_secret_key
MAIN_BOT_API_URL=https://your-main-bot.up.railway.app
RAZORPAY_WEBHOOK_SECRET=same_as_main_bot_webhook_secret
```

**Railway will provide you a URL like:** `https://your-admin-bot.up.railway.app`

### 4️⃣ Configure Razorpay Webhook

1. Go to [Razorpay Dashboard](https://dashboard.razorpay.com/)
2. Navigate to **Settings → Webhooks**
3. **Webhook URL**: `https://your-admin-bot.up.railway.app/webhook/razorpay`
4. **Active Events**: Select `payment.captured`
5. **Secret**: Use the same value as `RAZORPAY_WEBHOOK_SECRET`
6. Save

### 5️⃣ Test Your Deployment

1. Message your main bot: `/start`
2. Select a tier and payment method
3. Complete a test payment
4. Admin bot should notify you!

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
RAZORPAY_BRONZE_PAGE=https://rzp.io/rzp/bronze_page
RAZORPAY_GOLD_PAGE=https://rzp.io/rzp/gold_page
RAZORPAY_WEBHOOK_SECRET=whsec_secret
SECRET_API_KEY=your_secret_key
PORT=5000
```

Create `.env.admin` file:

```bash
ADMIN_BOT_TOKEN=your_admin_bot_token
ADMIN_USER_ID=your_user_id
ADMIN_PORT=5001
SECRET_API_KEY=same_as_main_bot
MAIN_BOT_API_URL=http://localhost:5000
RAZORPAY_WEBHOOK_SECRET=same_as_main_bot
```

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
ngrok http 5001
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
5. Admin bot tells main bot to grant subscription
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

Edit these in `bot.py` (around line 50-80):

```python
# Telegram Stars Prices
TIER_PRICES = {
    "bronze": 100,  # 100 stars
    "gold":   250,  # 250 stars
}

# Razorpay Prices (in paise)
RAZORPAY_PRICES = {
    "bronze": 8000,   # ₹80.00
    "gold": 20000,    # ₹200.00
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
- ✅ Secure API key for bot-to-bot communication
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

### API communication failing

- Ensure `SECRET_API_KEY` is **identical** in both bots
- Check `MAIN_BOT_API_URL` points to correct Railway URL
- Verify both services are running in Railway dashboard

---

## 📁 Project Structure

```
final_telegram/
│
├── bot.py                 # Main user-facing bot
├── admin_bot.py          # Admin payment approval bot
├── requirements.txt       # Python dependencies
├── Procfile              # Railway start command
├── runtime.txt           # Python version for Railway
├── railway.json          # Railway configuration
├── .gitignore           # Git ignore rules
├── README.md            # This file
├── subscriptions.db     # Main bot database (auto-created)
└── admin_payments.db    # Admin bot database (auto-created)
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
