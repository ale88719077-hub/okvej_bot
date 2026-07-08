import asyncio
import logging
import os
from urllib.parse import urljoin

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
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

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@okvej")
SITE_URL = "https://okvej.com.ua/"
BOT_URL = "https://t.me/okvej_shop_bot"
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "sv000svbdd")

bot = Bot(token=TOKEN)
dp = Dispatcher()

shop = HoroshopAPI(
    domain=os.getenv("HOROSHOP_DOMAIN", "okvej.com.ua"),
    login=os.getenv("HOROSHOP_LOGIN"),
    password=os.getenv("HOROSHOP_PASSWORD"),
)


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


def catalog_buttons():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🍬 Цукерки вагові", url="https://okvej.com.ua/ua/konfety-vesovye/")],
            [InlineKeyboardButton(text="🍭 Карамель", url="https://okvej.com.ua/ua/karamel-v-miahkoi-upakovke/")],
            [InlineKeyboardButton(text="🎁 Подарунки", url="https://okvej.com.ua/ua/nabory-podarochnykh-konfet/")],
            [InlineKeyboardButton(text="🍪 Печиво", url="https://okvej.com.ua/ua/pechene-y-muchnye-yzdelyia/")],
            [InlineKeyboardButton(text="☁️ Зефір та мармелад", url="https://okvej.com.ua/ua/zefyr-y-marmelad/")],
            [InlineKeyboardButton(text="🍫 Шоколад", url="https://okvej.com.ua/ua/shokolad/")],
            [InlineKeyboardButton(text="💬 Менеджер", url=f"https://t.me/{MANAGER_USERNAME.lstrip('@')}")],
            [InlineKeyboardButton(text="🌐 Сайт", url=SITE_URL)],
            [InlineKeyboardButton(text="⭐ Відгуки", url="https://okvej.com.ua/ua/otzyvy-o-magazine/")],
            [InlineKeyboardButton(text="🤖 Відкрити бота", url=BOT_URL)],
        ]
    )


def localize(value):
    if isinstance(value, dict):
        return (
            value.get("ua")
            or value.get("uk")
            or value.get("ru")
            or value.get("ru_RU")
            or value.get("uk_UA")
            or next(iter(value.values()), "")
        )
    return value or ""


def product_link(product: dict) -> str:
    link = localize(product.get("link") or product.get("url") or "")
    if not link:
        return SITE_URL
    if link.startswith("http"):
        return link
    return urljoin(SITE_URL, link.lstrip("/"))


def product_price(product: dict) -> str:
    price = product.get("price") or product.get("price_old") or product.get("cost") or "-"
    if isinstance(price, dict):
        price = price.get("value") or price.get("price") or next(iter(price.values()), "-")
    return str(price)


def get_image_url(product: dict) -> str | None:
    images = product.get("images") or product.get("image") or product.get("photo")

    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            url = first.get("url") or first.get("src") or first.get("image") or first.get("big")
        else:
            url = str(first)
    elif isinstance(images, dict):
        url = images.get("url") or images.get("src") or images.get("image") or images.get("big")
    elif isinstance(images, str):
        url = images
    else:
        url = None

    if not url:
        return None
    if url.startswith("http"):
        return url
    return urljoin(SITE_URL, url.lstrip("/"))


def is_in_stock(product: dict) -> bool:
    candidates = [
        product.get("presence"),
        product.get("available"),
        product.get("in_stock"),
        product.get("stock"),
        product.get("quantity"),
        product.get("count"),
        product.get("balance"),
    ]

    positive = {
        "1", "true", "yes", "available", "in_stock", "instock",
        "в наявності", "є в наявності", "наявний", "есть в наличии",
        "доступно", "available_for_order"
    }

    negative = {
        "0", "false", "no", "none", "null", "not_available", "out_of_stock",
        "немає", "немає в наявності", "нет", "нет в наличии", "відсутній",
        "отсутствует", "не в наличии"
    }

    for value in candidates:
        value = localize(value)

        if value is None or value == "":
            continue

        if isinstance(value, bool):
            return value

        if isinstance(value, (int, float)):
            return value > 0

        text = str(value).strip().lower()

        if text in negative:
            return False

        if text in positive:
            return True

        try:
            return float(text.replace(",", ".")) > 0
        except ValueError:
            pass

    return False


async def get_all_products(max_items: int = 2000, batch_size: int = 500) -> list[dict]:
    products: list[dict] = []
    offset = 0

    while len(products) < max_items:
        batch = await shop.get_products(limit=batch_size, offset=offset)
        if not batch:
            break

        products.extend(batch)

        if len(batch) < batch_size:
            break

        offset += batch_size
        await asyncio.sleep(0.4)

    return products[:max_items]


def product_post_text(product: dict) -> str:
    title = localize(product.get("title"))
    price = product_price(product)
    link = product_link(product)

    return (
        f"🍬 <b>{title}</b>\n\n"
        f"✅ В наявності\n"
        f"💰 Ціна: <b>{price} грн</b>\n\n"
        f"🔗 Замовити:\n{link}\n\n"
        f"🤖 Бот магазину: {BOT_URL}"
    )


async def send_product_to_channel(product: dict) -> bool:
    title = localize(product.get("title"))
    if not title:
        return False

    text = product_post_text(product)
    image_url = get_image_url(product)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купити", url=product_link(product))],
            [InlineKeyboardButton(text="🤖 Відкрити бота", url=BOT_URL)],
        ]
    )

    try:
        if image_url:
            await bot.send_photo(
                CHANNEL_USERNAME,
                photo=image_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await bot.send_message(
                CHANNEL_USERNAME,
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        return True
    except Exception as e:
        logging.exception("Cannot publish product: %s", e)
        return False


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


@dp.message(Command("pin_menu"))
async def pin_menu(message: Message):
    await bot.send_message(
        CHANNEL_USERNAME,
        "🍬 <b>OKVEJ | Солодощі та подарунки</b>\n\n"
        "✅ Оптові та роздрібні замовлення\n"
        "🌐 Наш сайт: https://okvej.com.ua\n\n"
        "Оберіть потрібний розділ 👇",
        parse_mode="HTML",
        reply_markup=catalog_buttons(),
    )

    await message.answer("✅ Меню з активними кнопками опубліковано в канал. Тепер закріпіть його вручну.")


@dp.message(Command("publish_catalog"))
async def publish_catalog(message: Message):
    await message.answer(
        "🚀 Починаю публікацію товарів у канал.\n"
        "Публікую тільки товари в наявності."
    )

    products = await get_all_products(max_items=2000, batch_size=500)
    in_stock_products = [p for p in products if is_in_stock(p)]

    if not in_stock_products:
        await message.answer("😔 Не знайшов товарів у наявності. Треба перевірити поле наявності в API.")
        return

    published = 0
    failed = 0

    for product in in_stock_products:
        ok = await send_product_to_channel(product)
        if ok:
            published += 1
        else:
            failed += 1
        await asyncio.sleep(1.2)

    await message.answer(
        f"✅ Публікацію завершено.\n\n"
        f"Опубліковано: {published}\n"
        f"Помилок: {failed}"
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
    await message.answer(
        "🍬 <b>Каталог OKVEJ</b>\n\n"
        "Оберіть потрібну категорію 👇",
        reply_markup=catalog_buttons(),
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
        products = await get_all_products(max_items=1000, batch_size=500)
        results = []

        for product in products:
            if not is_in_stock(product):
                continue

            title = localize(product.get("title"))
            if query in title.lower():
                results.append(product)

        if not results:
            await message.answer("😔 Нічого не знайдено в наявності.")
        else:
            text = "🍬 Знайдені товари в наявності:\n\n"

            for p in results[:10]:
                title = localize(p.get("title"))
                price = product_price(p)
                link = product_link(p)

                text += (
                    f"• <b>{title}</b>\n"
                    f"💰 {price} грн\n"
                    f"🔗 {link}\n\n"
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
    await message.answer(
        "🌐 Наш сайт:\n\nhttps://okvej.com.ua",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🌐 Відкрити OKVEJ", url=SITE_URL)]
            ]
        ),
    )


@dp.message(F.text == "💬 Менеджер")
async def manager(message: Message):
    await message.answer(
        "💬 <b>Зв'язатися з менеджером</b>\n\n"
        f"Напишіть сюди:\n@{MANAGER_USERNAME.lstrip('@')}",
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
