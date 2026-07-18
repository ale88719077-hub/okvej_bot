# OKVEJ Bot v18.2 — Brand Mini App

Фірмовий Mini App: брендова шапка, каталог, кнопка сайту, подарункові набори та збільшені фото.

# OKVEJ Bot + Telegram Mini App v16.0

## Railway variables
Required:
- TELEGRAM_BOT_TOKEN
- HOROSHOP_LOGIN
- HOROSHOP_PASSWORD
- HOROSHOP_DOMAIN=okvej.com.ua
- MANAGER_CHAT_ID
- ADMIN_USER_ID
- CHANNEL_USERNAME=@okvej

After Railway creates a public domain, add:
- MINI_APP_URL=https://YOUR-RAILWAY-DOMAIN.up.railway.app/miniapp

Then redeploy. The bot menu will show **🛍 Відкрити магазин**.

## Railway
Procfile starts one web service which runs both:
- Telegram polling bot
- Mini App web server on Railway PORT

Health check: `/health`
