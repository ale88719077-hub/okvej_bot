OKVEJ bot v18.4 — уведомления о новых заказах владельцу и менеджеру

Замените bot.py и добавьте order_notifier.py в проект Railway.

Railway Variables:
ORDER_NOTIFY_CHAT_IDS=ВАШ_CHAT_ID,CHAT_ID_МЕНЕДЖЕРА
ORDER_POLL_SECONDS=45
ORDER_STATE_FILE=/data/order_notifier_state.json
HOROSHOP_ORDERS_ENDPOINT=orders/export
HOROSHOP_ORDERS_ADMIN_URL=https://okvej.com.ua/admin/orders/

Уже существующие переменные должны остаться:
TELEGRAM_BOT_TOKEN
HOROSHOP_DOMAIN
HOROSHOP_LOGIN
HOROSHOP_PASSWORD

Каждый получатель должен открыть @okvej_shop_bot, нажать Start и отправить /myid.
При первом запуске старые заказы не отправляются; бот начинает уведомлять только о новых.
