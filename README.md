# OKVEJ Telegram Bot v4

Полная папка проекта для загрузки через GitHub Desktop.

## Что работает

- каталог из Horoshop API;
- только товары со статусом `presence.id == 1`;
- категории с количеством товаров;
- пагинация по 8 товаров;
- карточки товаров;
- поиск;
- корзина;
- оформление заказа;
- команда `/пост` сохранена;
- команда `/debug_stock` удалена;
- `.DS_Store` исключён через `.gitignore`.

## Railway Variables

- `TELEGRAM_BOT_TOKEN`
- `HOROSHOP_DOMAIN`
- `HOROSHOP_LOGIN`
- `HOROSHOP_PASSWORD`
- `MANAGER_USERNAME`
- `MANAGER_CHAT_ID`
- `CHANNEL_USERNAME`

## Запуск

```bash
pip install -r requirements.txt
python bot.py
```

Railway запускает:

```text
worker: python bot.py
```
