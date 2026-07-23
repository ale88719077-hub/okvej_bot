OKVEJ BOT v20.0 — integrated Analytics + SEO
=============================================

Что изменено
------------
1. /stats, /seo и /panel подключены непосредственно внутри bot.py.
2. Роутер регистрируется до остальных обработчиков Telegram.
3. Исправлена проверка администратора: сначала ADMIN_USER_ID, затем
   ADMIN_CHAT_ID и MANAGER_CHAT_ID.
4. К основному меню добавлены кнопки «📊 Аналитика» и «📈 SEO».
5. start.py оставлен только для совместимости со старой командой Railway.
6. Версия бота: 20.0; сборка: 2026-07-23-integrated-analytics-seo.

Установка
---------
Замените в GitHub все файлы из этой папки, кроме секретных ключей.
JSON-ключ Google в GitHub не загружать.

Railway → Settings → Custom Start Command:
python bot.py

Допустимо временно оставить:
python start.py

Переменные Railway
------------------
TELEGRAM_BOT_TOKEN
ADMIN_USER_ID
HOROSHOP_DOMAIN
HOROSHOP_LOGIN
HOROSHOP_PASSWORD
HOROSHOP_ORDERS_ENDPOINT
GSC_SITE_URL
GOOGLE_SERVICE_ACCOUNT_JSON

GOOGLE_SERVICE_ACCOUNT_JSON должен содержать полный НОВЫЙ JSON сервисного
аккаунта. Старый удалённый ключ использовать нельзя.

Доступ Search Console
---------------------
Email сервисного аккаунта из поля client_email нового JSON должен быть добавлен
в Google Search Console как пользователь ресурса GSC_SITE_URL.

Проверка после Deploy
---------------------
В логах должны появиться обе строки:
Analytics/SEO router registered directly in bot.py
Starting OKVEJ bot v20.0 (2026-07-23-integrated-analytics-seo)

Затем проверить в Telegram:
/version
/panel
/stats
/seo

Важно о TelegramConflictError
-----------------------------
Ошибка "terminated by other getUpdates request" означает, что одновременно
работали два экземпляра бота. Остановите старый deployment/контейнер и оставьте
только один активный экземпляр. В Railway можно включить Teardown, чтобы старый
контейнер завершался при запуске нового.
