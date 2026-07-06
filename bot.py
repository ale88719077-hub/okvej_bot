import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "okvej_manager")
SITE_URL = os.getenv("SITE_URL", "https://okvej.com.ua")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/simeinatsukerniaa")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍬 Каталог", url=SITE_URL)],
        [InlineKeyboardButton(text="🔍 Пошук товару", callback_data="search")],
        [InlineKeyboardButton(text="🎁 Подарункові набори", url=f"{SITE_URL}/ua/podarochnye-nabory/")],
        [InlineKeyboardButton(text="🔥 Акції", url=f"{SITE_URL}/ua/sale/")],
        [InlineKeyboardButton(text="💬 Менеджер", url=f"https://t.me/{MANAGER_USERNAME}")],
        [InlineKeyboardButton(text="📢 Канал OKVEJ", url=CHANNEL_URL)],
    ])


@dp.message(CommandStart())
async def start(message: types.Message):
    text = (
        "🍬 <b>Вітаємо в OKVEJ!</b>\n\n"
        "Тут можна швидко знайти солодощі, подарункові набори та перейти до оформлення замовлення.\n\n"
        "🍫 Цукерки, шоколад, печиво\n"
        "🎁 Подарункові набори\n"
        "🚚 Доставка по всій Україні\n"
        "🏠 Курʼєр по Києву\n\n"
        "Оберіть дію нижче 👇"
    )
    await message.answer(text, reply_markup=main_menu(), parse_mode="HTML")


@dp.callback_query(lambda c: c.data == "search")
async def search_info(callback: types.CallbackQuery):
    await callback.message.answer(
        "🔍 Пошук товарів скоро буде працювати прямо в боті.\n\n"
        "Поки що напишіть менеджеру, що шукаєте, або відкрийте каталог на сайті.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Відкрити каталог", url=SITE_URL)],
            [InlineKeyboardButton(text="💬 Написати менеджеру", url=f"https://t.me/{MANAGER_USERNAME}")],
        ])
    )
    await callback.answer()


@dp.message()
async def any_message(message: types.Message):
    await message.answer(
        "Я бот магазину OKVEJ 🍬\n\n"
        "Натисніть /start, щоб відкрити меню."
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
