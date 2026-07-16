OKVEJ Bot v15.0.1

Исправление:
- устранена ошибка NameError: clean_text is not defined в AI-помощнике;
- для описания товара теперь используется существующая функция clean_product_description;
- версия обновлена до 15.0.1.

После загрузки файлов:
1. Commit в GitHub.
2. Redeploy в Railway.
3. Убедитесь, что запущен только один экземпляр бота, иначе Telegram покажет Conflict: terminated by other getUpdates request.
4. Проверьте /version и AI-помощника.
