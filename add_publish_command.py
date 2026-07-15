from pathlib import Path

BOT_FILE = Path("bot.py")

if not BOT_FILE.exists():
    raise SystemExit("Не найден bot.py. Положите этот файл рядом с bot.py и запустите снова.")

source = BOT_FILE.read_text(encoding="utf-8")
marker = '@dp.message(CommandStart())'

if marker not in source:
    raise SystemExit("Не найден обработчик CommandStart в bot.py.")

if 'async def publish_channel_menu' in source:
    raise SystemExit("Команда /publish уже добавлена.")

block = '''def channel_main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🍬 Цукерки вагові", url="https://okvej.com.ua/ua/konfety-vesovye/")],
            [InlineKeyboardButton(text="🍭 Карамель", url="https://okvej.com.ua/ua/karamel-v-miahkoi-upakovke/")],
            [InlineKeyboardButton(text="🎁 Подарунки", url="https://okvej.com.ua/ua/nabory-podarochnykh-konfet/")],
            [InlineKeyboardButton(text="🍪 Печиво", url="https://okvej.com.ua/ua/pechene-y-muchnye-yzdelyia/")],
            [InlineKeyboardButton(text="☁️ Зефір та мармелад", url="https://okvej.com.ua/ua/zefyr-y-marmelad/")],
            [InlineKeyboardButton(text="🍫 Шоколад", url="https://okvej.com.ua/ua/shokolad/")],
            [InlineKeyboardButton(text="💬 Менеджер", url=f"https://t.me/{MANAGER_USERNAME}")],
            [InlineKeyboardButton(text="🌐 Сайт", url="https://okvej.com.ua/")],
            [InlineKeyboardButton(text="⭐ Відгуки", url="https://okvej.com.ua/ua/reviews/")],
            [InlineKeyboardButton(text="🤖 Відкрити бота", url="https://t.me/okvej_shop_bot?start=menu")],
        ]
    )


@dp.message(Command("publish", "update"))
async def publish_channel_menu(message: Message):
    text = (
        "🍬 <b>OKVEJ | Солодощі та подарунки</b>\\n\\n"
        "✅ Оптові та роздрібні замовлення\\n"
        "🌐 Наш сайт: https://okvej.com.ua\\n\\n"
        "Оберіть потрібний розділ 👇"
    )

    try:
        published = await bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=text,
            parse_mode="HTML",
            reply_markup=channel_main_keyboard(),
            disable_web_page_preview=True,
        )

        try:
            await bot.pin_chat_message(
                chat_id=CHANNEL_USERNAME,
                message_id=published.message_id,
                disable_notification=True,
            )
            pin_status = "\\n📌 Повідомлення закріплено."
        except Exception:
            logging.exception("Не удалось закрепить сообщение")
            pin_status = (
                "\\n⚠️ Повідомлення опубліковано, але не закріплено. "
                "Перевірте право бота «Закріплення повідомлень»."
            )

        await message.answer(
            "✅ Головне повідомлення з кнопками опубліковано заново."
            + pin_status
        )

    except Exception as error:
        logging.exception("Ошибка публикации главного сообщения")
        await message.answer(
            "❌ Не вдалося опублікувати повідомлення.\\n\\n"
            f"Помилка: {error}\\n\\n"
            "Перевірте, що бот доданий адміністратором каналу "
            "і має право публікувати повідомлення."
        )


'''

source = source.replace(marker, block + "\n\n" + marker, 1)
BOT_FILE.write_text(source, encoding="utf-8")
print("Готово: команды /publish и /update добавлены в bot.py")
