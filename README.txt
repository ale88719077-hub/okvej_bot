OKVEJ BOT v18.8 — новые заказы через Zapier webhook

ПОЧЕМУ ТАК:
Метод /api/orders/export/ в вашем магазине отвечает UNDEFINED_FUNCTION.
Поэтому стабильный вариант — официальная интеграция Хорошоп с Zapier.

1. Загрузите в Railway:
   - bot.py
   - horoshop_api.py
   - requirements.txt

2. В Railway Variables добавьте:
   HOROSHOP_ORDER_WEBHOOK_SECRET=придумайте_длинный_секрет

Например:
   HOROSHOP_ORDER_WEBHOOK_SECRET=OKVEJ_2026_orders_8f72Kp91

3. Получатели берутся автоматически из:
   ADMIN_USER_ID
   MANAGER_CHAT_ID

4. После Redeploy проверьте адрес:
   https://okvejbot-production.up.railway.app/api/horoshop-order/health

Должен появиться JSON:
   {"ok":true,"configured":true,"recipients":2,"version":"18.8"}

5. В Хорошоп откройте Настройки → Zapier и выберите Zap для нового заказа.
В Zapier добавьте действие Webhooks by Zapier → POST.

URL:
   https://okvejbot-production.up.railway.app/api/horoshop-order?secret=ВАШ_СЕКРЕТ

Payload Type: json
Data: передайте все поля заказа из шага Хорошоп.

6. Сделайте тест в Zapier. Сообщение должно прийти владельцу и менеджеру.

ВАЖНО:
- order_notifier.py больше не нужен — удалите его.
- ORDER_NOTIFY_CHAT_IDS не обязателен.
- Старый цикл запросов orders/export удалён, поэтому ошибка UNDEFINED_FUNCTION исчезнет.
