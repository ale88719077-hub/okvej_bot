OKVEJ Bot v20.1 — Google diagnostics

Изменения:
- безопасная диагностика Google-переменных без вывода ключа;
- лог метода загрузки: FILE / JSON / BASE64 / NONE;
- лог длины переменной, но не её содержимого;
- команда /diag для администратора;
- сохранены /seo, /stats и /panel;
- версия бота 20.1.

Railway Start Command:
python bot.py

После Deploy в логах ищите:
Google config: method=...

Проверка в Telegram:
/version
/diag
/seo
/stats

Важно: JSON-ключ Google нельзя хранить в GitHub.
