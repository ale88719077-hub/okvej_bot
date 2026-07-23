OKVEJ Bot v20.2 — безопасные ошибки и тайм-ауты

Исправлено:
- Telegram больше не падает на ошибках вида <HttpError ...>;
- текст ошибок Google и Horoshop экранируется через html.escape;
- добавлен тайм-аут 60 секунд для /seo и /stats;
- при тайм-ауте бот отправляет понятное сообщение;
- сохранены /diag, /seo, /stats, /panel и весь функционал v20.1.

Railway Start Command:
python bot.py

После Deploy проверить:
/version
/diag
/seo
/stats
