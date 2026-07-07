import asyncio
import logging
import os
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
dp = Dispatcher()

shop = HoroshopAPI(
    domain=os.getenv("HOROSHOP_DOMAIN", "okvej.com.ua"),
    login=os.getenv("HOROSHOP_LOGIN"),
    password=os.getenv("HOROSHOP_PASSWORD"),
)

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@okvej")
SITE_URL = "https://okvej.com.ua"


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


def localized(value):
    if isinstance(value, dict):
        return value.get("ua") or value.get("uk") or value.get("ru") or next(iter(value.values()), "")
    return value or ""


def value_to_text(value) -> str:
    if isinstance(value, dict):
        return " ".join(value_to_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(value_to_text(v) for v in value)
    return str(value or "")


def is_in_stock(product: dict) -> bool:
    candidates = [
        product.get("presence"),
        product.get("available"),
        product.get("availability"),
        product.get("stock"),
        product.get("quantity"),
        product.get("amount"),
        product.get("residue"),
    ]

    for presence in candidates:
        if isinstance(presence, dict):
            presence = localized(presence) or value_to_text(presence)

        if presence is True or presence == 1:
            return True

        if presence is False or presence == 0:
            continue

        if presence is None:
            continue

        value = str(presence).strip().lower()

        negative_values = [
            "",
            "0",
            "false",
            "no",
            "none",
            "null",
            "not_available",
            "out_of_stock",
            "unavailable",
            "немає",
            "немає в наявності",
            "нет",
            "нет в наличии",
            "відсутній",
            "отсутствует",
            "під замовлення",
            "под заказ",
        ]

        positive_values = [
            "1",
            "true",
            "yes",
            "available",
            "in_stock",
            "в наявності",
            "є в наявності",
            "есть в наличии",
            "наявний",
            "доступний",
        ]

        if value in negative_values:
            continue

        if any(word in value for word in positive_values):
            return True

        try:
            if float(value.replace(",", ".")) > 0:
                return True
        except ValueError:
            pass

    return False


def product_link(product: dict) -> str:
    link = localized(product.get("link"))
    if not link:
        return ""
    link = str(link).strip()
    if link.startswith("http://") or link.startswith("https://"):
        return link
    if not link.startswith("/"):
        link = "/" + link
    return SITE_URL + link


def first_image_url(product: dict) -> str:
    images = product.get("images") or product.get("image") or []

    if isinstance(images, str):
        return images

    if isinstance(images, dict):
        for key in ("url", "src", "image", "original", "big", "thumb"):
            value = images.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        images = list(images.values())

    if isinstance(images, list):
        for image in images:
            if isinstance(image, str) and image.startswith("http"):
                return image
            if isinstance(image, dict):
                for key in ("url", "src", "image", "original", "big", "thumb"):
                    value = image.get(key)
                    if isinstance(value, str) and value.startswith("http"):
                        return value

    return ""


def product_post_text(product: dict) -> str:
    title = escape(str(localized(product.get("title"))))
    price = escape(str(localized(product.get("price")) or "-"))
    link = product_link(product)

    text = f"🍬 <b>{title}</b>\n\n💰 {price} грн"
    if link:
        text += f"\n\n🔗 <a href=\"{escape(link)}\">Дивитися товар</a>"
    return text


async def load_all_products(limit: int = 500, max_pages: int = 50):
    all_products = []
    offset = 0

    for _ in range(max_pages):
        products = await shop.get_products(limit=limit, offset=offset)
        if not products:
            break

        all_products.extend(products)

        if len(products) < limit:
            break

        offset += limit

    return all_products


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


@dp.message(Command("publish_catalog"))
async def publish_catalog(message: Message):
    await message.answer(f"Починаю публікацію товарів у канал {CHANNEL_USERNAME}...")

    try:
        products = await load_all_products()
        in_stock_products = [p for p in products if is_in_stock(p)]

        if not in_stock_products:
            await message.answer("Не знайшов товарів у наявності для публікації.")
            return

        published = 0
        failed = 0
        seen = set()

        for product in in_stock_products:
            title = str(localized(product.get("title"))).strip()
            link = product_link(product)
            article = str(localized(product.get("article"))).strip()
            unique_key = article or link or title

            if unique_key in seen:
                continue
            seen.add(unique_key)

            text = product_post_text(product)
            image_url = first_image_url(product)

            try:
                if image_url:
                    await bot.send_photo(
                        chat_id=CHANNEL_USERNAME,
                        photo=image_url,
                        caption=text,
                        parse_mode="HTML",
                    )
                else:
                    await bot.send_message(
                        chat_id=CHANNEL_USERNAME,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                    )
                published += 1
                await asyncio.sleep(0.6)
            except Exception as e:
                failed += 1
                logging.exception("Failed to publish product %s: %s", title, e)
                await asyncio.sleep(0.6)

        await message.answer(
            f"Готово ✅\n"
            f"Опубліковано: {published}\n"
            f"Помилок: {failed}\n"
            f"Канал: {CHANNEL_USERNAME}"
        )

    except Exception as e:
        await message.answer(f"❌ Помилка публікації: {e}")


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
    query = (message.text or "").strip().lower()

    if not query:
        await message.answer("Введіть назву товару текстом 👇")
        return

    try:
        products = await shop.get_products(limit=500)
        results = []

        for product in products:
            if not is_in_stock(product):
                continue

            title = localized(product.get("title"))
            if query in title.lower():
                results.append(product)

        if not results:
            await message.answer("😔 Нічого не знайдено в наявності.")
        else:
            text = "🍬 Знайдені товари в наявності:\n\n"

            for p in results[:10]:
                title = escape(str(localized(p.get("title"))))
                price = escape(str(localized(p.get("price")) or "-"))
                link = product_link(p)

                text += f"• <b>{title}</b>\n💰 {price} грн"
                if link:
                    text += f"\n🔗 {link}"
                text += "\n\n"

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
