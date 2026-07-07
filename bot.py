import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from horoshop_api import HoroshopAPI
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
print("ENV LOGIN =", os.getenv("HOROSHOP_LOGIN"))
print("ENV PASSWORD =", os.getenv("HOROSHOP_PASSWORD"))
print("ENV DOMAIN =", os.getenv("HOROSHOP_DOMAIN"))
shop = HoroshopAPI(
    domain=os.getenv("HOROSHOP_DOMAIN", "okvej.com.ua"),
    login=os.getenv("HOROSHOP_LOGIN"),
    password=os.getenv("HOROSHOP_PASSWORD"),
)
dp = Dispatcher()
class SearchState(StatesGroup):
    waiting_query = State()
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍬 Каталог"), KeyboardButton(text="🔥 Акції")],
        [KeyboardButton(text="🔍 Пошук товару"), KeyboardButton(text="🛒 Кошик")],
        [KeyboardButton(text="🌐 Сайт"), KeyboardButton(text="💬 Менеджер")],
        [KeyboardButton(text="📢 Канал OKVEJ")],
    ],
    resize_keyboard=True,
)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🍬 <b>Вітаємо в OKVEJ!</b>\n\n"
        "Тут ви зможете швидко знайти солодощі, переглянути каталог "
        "та оформити замовлення.\n\n"
        "Оберіть потрібний розділ 👇",
        reply_markup=main_menu,
        parse_mode="HTML",
    )


@dp.message(F.text == "📢 Канал OKVEJ")
async def channel(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Перейти в канал OKVEJ", url="https://t.me/okvej")]
        ]
    )

    await message.answer(
        "📢 Наш Telegram-канал OKVEJ:\n\nhttps://t.me/okvej",
        reply_markup=keyboard,
    )


@dp.message(F.text == "🍬 Каталог")
async def catalog(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🍫 Перейти в каталог", url="https://okvej.com.ua/")],
            [InlineKeyboardButton(text="🎁 Подарункові набори", url="https://okvej.com.ua/")],
        ]
    )

    await message.answer(
        "🍬 <b>Каталог OKVEJ</b>\n\n"
        "Поки каталог відкривається на сайті.\n"
        "Наступним етапом ми підключимо товари прямо в Telegram через API Хорошоп.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@dp.message(F.text == "🔥 Акції")
async def sales(message: Message):
    await message.answer(
        "🔥 <b>Акції OKVEJ</b>\n\n"
        "Скоро тут будуть спеціальні пропозиції, знижки та новинки.\n\n"
        "А поки можна переглянути товари на сайті:\n"
        "https://okvej.com.ua",
        parse_mode="HTML",
    )


@dp.message(F.text == "🔍 Пошук товару")
async def search(message: Message, state: FSMContext):
    await state.set_state(SearchState.waiting_query)
    await message.answer(
        "🔍 Введіть назву товару, наприклад:\n\n"
        "• марципан\n"
        "• печиво\n"
        "• шоколад"
    )


@dp.message(SearchState.waiting_query)
async def process_search(message: Message, state: FSMContext):
    query = message.text.lower()

    try:
        products = await shop.get_products(limit=300)

        results = []

        for product in products:
            title = product.get("title", "")
            if query in title.lower():
                results.append(product)

        if not results:
            await message.answer("😔 Нічого не знайдено.")
        else:
            text = "🍬 Знайдені товари:\n\n"

            for p in results[:10]:
                text += (
                    f"• <b>{p['title']}</b>\n"
                    f"💰 {p.get('price', '-') } грн\n"
                    f"🔗 {p.get('link', '')}\n\n"
                )

            await message.answer(text, parse_mode="HTML")

    except Exception as e:
        await message.answer(f"❌ Помилка: {e}")

    await state.clear()
    

@dp.message(F.text == "🛒 Кошик")
async def cart(message: Message):
    await message.answer(
        "🛒 <b>Кошик</b>\n\n"
        "Кошик поки порожній.\n\n"
        "Після підключення каталогу тут можна буде переглядати обрані товари "
        "та оформлювати замовлення прямо в Telegram.",
        parse_mode="HTML",
    )


@dp.message(F.text == "🌐 Сайт")
async def site(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Відкрити OKVEJ", url="https://okvej.com.ua/")]
        ]
    )

    await message.answer(
        "🌐 Наш сайт:\n\nhttps://okvej.com.ua",
        reply_markup=keyboard,
    )


@dp.message(F.text == "💬 Менеджер")
async def manager(message: Message):
    await message.answer(
        "💬 <b>Зв'язатися з менеджером</b>\n\n"
        "Напишіть сюди:\n"
        "@okvej_manager",
        parse_mode="HTML",
    )


@dp.message()
async def unknown_message(message: Message):
    await message.answer(
        "Я вас зрозумів 👍\n\n"
        "Оберіть дію з меню нижче 👇",
        reply_markup=main_menu,
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
