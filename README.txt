OKVEJ — Хорошоп → Gmail → собственный Telegram-бот
===================================================

Это бесплатная схема без Zapier.

ВАЖНО СНАЧАЛА
-------------
В Railway переменная HOROSHOP_ORDER_WEBHOOK_SECRET уже должна содержать непустое
значение. После добавления/изменения переменной обязательно нажмите Redeploy.

Проверка:
https://okvejbot-production.up.railway.app/api/horoshop-order/health

Ожидаемый ответ:
{"ok":true,"configured":true,"recipients":2,"version":"18.8"}

Если configured=false — новый деплой ещё не получил переменную.

ШАГ 1. Gmail
------------
1. В Gmail создайте ярлык: OKVEJ_NEW_ORDER
2. Создайте фильтр:
   Содержит слова / тема: Оформлен новый заказ
3. Включите действие: применить ярлык OKVEJ_NEW_ORDER.

Официальная инструкция Хорошоп также использует письма о новых заказах и Gmail-фильтр.

ШАГ 2. Google Apps Script
-------------------------
1. Откройте script.google.com
2. Создайте новый проект.
3. Удалите стандартный код и вставьте содержимое Code.gs.
4. Откройте Project Settings → Script Properties.
5. Добавьте:
   WEBHOOK_URL=https://okvejbot-production.up.railway.app/api/horoshop-order
   WEBHOOK_SECRET=то же значение, что HOROSHOP_ORDER_WEBHOOK_SECRET в Railway
6. Сохраните.

ШАГ 3. Тест
-----------
1. В редакторе выберите функцию testWebhook.
2. Нажмите Run.
3. Разрешите доступ к Gmail и внешним запросам.
4. В Telegram должно прийти тестовое уведомление владельцу и менеджеру.

ШАГ 4. Автоматическая проверка
------------------------------
1. Откройте Triggers (значок часов).
2. Add Trigger.
3. Function: checkHoroshopOrders
4. Event source: Time-driven
5. Type: Minutes timer
6. Every 5 minutes

После этого новые письма Хорошоп будут автоматически пересылаться в ваш бот.
Обработанные письма получают ярлык OKVEJ_ORDER_SENT и повторно не отправляются.

СТАРЫЕ ПЕРЕМЕННЫЕ
-----------------
Эти переменные больше не нужны для схемы webhook/email:
ORDER_POLL_SECONDS
ORDER_STATE_FILE
HOROSHOP_ORDERS_ENDPOINT
HOROSHOP_ORDERS_ADMIN_URL

ORDER_NOTIFY_CHAT_IDS можно удалить, если ADMIN_USER_ID и MANAGER_CHAT_ID заполнены.
