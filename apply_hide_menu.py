#!/usr/bin/env python3
from pathlib import Path
import sys

target = Path(sys.argv[1] if len(sys.argv) > 1 else "bot.py")
if not target.exists():
    raise SystemExit(f"Не найден файл: {target}")

text = target.read_text(encoding="utf-8")
original = text

old_import = '''    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)'''
new_import = '''    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton,
)'''
if "ReplyKeyboardRemove" not in text:
    if old_import not in text:
        raise SystemExit("Не удалось найти блок импорта aiogram.types.")
    text = text.replace(old_import, new_import, 1)

old_menu_row = '''        [KeyboardButton(text="📢 Канал OKVEJ")],
    ],
    resize_keyboard=True,
)'''
new_menu_row = '''        [KeyboardButton(text="📢 Канал OKVEJ")],
        [KeyboardButton(text="❌ Сховати меню")],
    ],
    resize_keyboard=True,
)'''
if 'KeyboardButton(text="❌ Сховати меню")' not in text:
    if old_menu_row not in text:
        raise SystemExit("Не удалось найти главное меню.")
    text = text.replace(old_menu_row, new_menu_row, 1)

old_channel_command = '''@dp.message(Command("menu"))
async def publish_menu_command(message: Message):
    await publish_channel_menu(message)
'''
new_channel_command = '''@dp.message(Command("channelmenu"))
async def publish_menu_command(message: Message):
    await publish_channel_menu(message)
'''
if old_channel_command in text:
    text = text.replace(old_channel_command, new_channel_command, 1)

marker = '''@dp.message(CommandStart())
async def start(message: Message):
'''
handlers = '''@dp.message(Command("menu"))
async def show_main_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✅ Головне меню оновлено.",
        reply_markup=main_menu,
    )


@dp.message(Command("hide"))
async def hide_main_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✅ Меню приховано. Щоб повернути його, надішліть /menu.",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(F.text == "❌ Сховати меню")
async def hide_main_menu_button(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✅ Меню приховано. Щоб повернути його, надішліть /menu.",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(Command("commands"))
async def commands_list(message: Message):
    await message.answer(
        "/start — головне меню\\n"
        "/menu — оновити клавіатуру\\n"
        "/hide — сховати клавіатуру\\n"
        "/version — версія бота\\n"
        "/admin — адмін-панель\\n"
        "/пост — опублікувати товар у каналі\\n"
        "/channelmenu — опублікувати меню у каналі\\n"
        "/myid — Telegram ID\\n"
        "/commands — список команд"
    )


'''
if '@dp.message(Command("hide"))' not in text:
    if marker not in text:
        raise SystemExit("Не удалось найти обработчик /start.")
    text = text.replace(marker, handlers + marker, 1)

if text == original:
    print("Изменения уже были применены.")
else:
    backup = target.with_suffix(target.suffix + ".backup")
    backup.write_text(original, encoding="utf-8")
    target.write_text(text, encoding="utf-8")
    print(f"Готово: {target}")
    print(f"Резервная копия: {backup}")
