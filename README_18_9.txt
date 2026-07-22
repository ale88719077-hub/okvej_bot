OKVEJ BOT v18.9 — прямые уведомления о заказах Хорошоп
=======================================================

Что изменено
------------
- Исправлен API-метод получения заказов: /api/orders/get/
- Новые заказы проверяются каждые 45 секунд.
- Уведомления приходят ADMIN_USER_ID и MANAGER_CHAT_ID.
- При первом запуске старые заказы запоминаются и не отправляются.
- Добавлены кнопки смены статуса:
  В обработку / Доставляется / Доставлено / Не доставлено.
- Добавлена кнопка «Позначити оплаченим».

Установка
---------
1. Замените в проекте Railway два файла:
   - bot.py
   - horoshop_api.py
2. requirements.txt оставьте из архива.
3. Старые переменные HOROSHOP_ORDERS_ENDPOINT и HOROSHOP_ORDER_WEBHOOK_SECRET
   для прямой проверки не нужны, но могут остаться — работе не мешают.
4. Обязательные переменные:
   TELEGRAM_BOT_TOKEN
   HOROSHOP_DOMAIN
   HOROSHOP_LOGIN
   HOROSHOP_PASSWORD
   ADMIN_USER_ID
   MANAGER_CHAT_ID
5. Railway Volume должен быть подключен к /data, чтобы история отправленных
   заказов сохранялась после перезапуска.
6. Нажмите Redeploy.

Ожидаемые строки в логах
------------------------
Starting OKVEJ bot v18.9
Horoshop orders/get polling started: interval=45s recipients=2
Horoshop orders initialized with ... existing orders

После строки initialized оформите новый тестовый заказ. Он должен прийти
в Telegram в течение примерно 45 секунд.

Дополнительно
-------------
ORDER_POLL_SECONDS=45   (можно изменить, минимум 20 секунд)
ORDER_STATE_FILE=/data/order_api_state.json
