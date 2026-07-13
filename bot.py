import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()


# =========================
# ГЛАВНОЕ МЕНЮ
# =========================

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🍬 Каталог"),
            KeyboardButton(text="🔥 Акції"),
        ],
        [
            KeyboardButton(text="🔍 Пошук товару"),
            KeyboardButton(text="🛒 Кошик"),
        ],
        [
            KeyboardButton(text="🌐 Сайт"),
            KeyboardButton(text="💬 Менеджер"),
        ],
        [
            KeyboardButton(text="📢 Канал OKVEJ"),
        ],
    ],
    resize_keyboard=True,
)


# =========================
# ТЕСТОВЫЕ ТОВАРЫ
# =========================

products = {
    "candy": [
        {
            "id": "demi_apricot",
            "name": "Цукерки DEMI Family Garden",
            "price": 320,
            "weight": "2,4 кг",
            "description": "Цукерки з абрикосовим джемом у шоколадній глазурі.",
            "url": "https://okvej.com.ua/",
        },
        {
            "id": "mieszko",
            "name": "Марципанки Mieszko Original",
            "price": 450,
            "weight": "1 кг",
            "description": "Польські марципанові цукерки в шоколадній глазурі.",
            "url": "https://okvej.com.ua/",
        },
    ],
    "cookies": [
        {
            "id": "alps",
            "name": "Печиво Альпи Батоша",
            "price": 185,
            "weight": "1,5 кг",
            "description": "Ніжне печиво з насиченим смаком та ароматною начинкою.",
            "url": "https://okvej.com.ua/",
        },
        {
            "id": "oatmeal",
            "name": "Печиво вівсяне Десняночка",
            "price": 340,
            "weight": "4 кг",
            "description": "Класичне вівсяне печиво для роздрібної та оптової торгівлі.",
            "url": "https://okvej.com.ua/",
        },
    ],
    "cakes": [
        {
            "id": "classic_cake",
            "name": "Кекс Хлібчик Класичний",
            "price": 175,
            "weight": "1,3 кг",
            "description": "М'який класичний кекс до кави та чаю.",
            "url": "https://okvej.com.ua/",
        }
    ],
}


# =========================
# КОРЗИНА
# Временно хранится в памяти
# =========================

user_carts: dict[int, list[str]] = {}


# =========================
# КЛАВИАТУРЫ
# =========================

def catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🍫 Цукерки",
                    callback_data="category:candy",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🍪 Печиво",
                    callback_data="category:cookies",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧁 Кекси та випічка",
                    callback_data="category:cakes",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔥 Акційні товари",
                    callback_data="catalog_sales",
                )
            ],
        ]
    )


def products_keyboard(category: str) -> InlineKeyboardMarkup:
    buttons = []

    for product in products.get(category, []):
        buttons.append(
            [
                InlineKeyboardButton(
                    text=product["name"],
                    callback_data=f"product:{category}:{product['id']}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ До категорій",
                callback_data="catalog_back",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def product_keyboard(
    category: str,
    product_id: str,
    product_url: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Додати до кошика",
                    callback_data=f"add:{category}:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌐 Відкрити на сайті",
                    url=product_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад до товарів",
                    callback_data=f"category:{category}",
                )
            ],
        ]
    )


# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================

def find_product(category: str, product_id: str) -> dict | None:
    for product in products.get(category, []):
        if product["id"] == product_id:
            return product

    return None


def find_product_by_id(product_id: str) -> dict | None:
    for category_products in products.values():
        for product in category_products:
            if product["id"] == product_id:
                return product

    return None


# =========================
# ОБРАБОТЧИК /START
# =========================

@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "🍬 Вітаємо в Telegram-магазині OKVEJ!\n\n"
        "Тут ви можете переглядати товари, акції та формувати замовлення.",
        reply_markup=main_menu,
    )


# =========================
# КАТАЛОГ
# =========================

@dp.message(F.text == "🍬 Каталог")
async def catalog_handler(message: Message) -> None:
    await message.answer(
        "🍬 <b>Каталог OKVEJ</b>\n\n"
        "Оберіть категорію товарів:",
        reply_markup=catalog_keyboard(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "catalog_back")
async def catalog_back_handler(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🍬 <b>Каталог OKVEJ</b>\n\n"
        "Оберіть категорію товарів:",
        reply_markup=catalog_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("category:"))
async def category_handler(callback: CallbackQuery) -> None:
    category = callback.data.split(":")[1]

    category_names = {
        "candy": "🍫 Цукерки",
        "cookies": "🍪 Печиво",
        "cakes": "🧁 Кекси та випічка",
    }

    category_name = category_names.get(category, "Товари")

    await callback.message.edit_text(
        f"<b>{category_name}</b>\n\n"
        "Оберіть товар:",
        reply_markup=products_keyboard(category),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================
# КАРТОЧКА ТОВАРА
# =========================

@dp.callback_query(F.data.startswith("product:"))
async def product_handler(callback: CallbackQuery) -> None:
    _, category, product_id = callback.data.split(":")

    product = find_product(category, product_id)

    if not product:
        await callback.answer(
            "Товар не знайдено",
            show_alert=True,
        )
        return

    text = (
        f"🍬 <b>{product['name']}</b>\n\n"
        f"⚖️ Вага: {product['weight']}\n"
        f"💰 Ціна: <b>{product['price']} грн</b>\n\n"
        f"📝 {product['description']}\n\n"
        f"📦 Товар доступний для замовлення."
    )

    await callback.message.edit_text(
        text,
        reply_markup=product_keyboard(
            category=category,
            product_id=product_id,
            product_url=product["url"],
        ),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================
# ДОБАВЛЕНИЕ В КОРЗИНУ
# =========================

@dp.callback_query(F.data.startswith("add:"))
async def add_to_cart_handler(callback: CallbackQuery) -> None:
    _, category, product_id = callback.data.split(":")

    product = find_product(category, product_id)

    if not product:
        await callback.answer(
            "Товар не знайдено",
            show_alert=True,
        )
        return

    user_id = callback.from_user.id

    if user_id not in user_carts:
        user_carts[user_id] = []

    user_carts[user_id].append(product_id)

    await callback.answer(
        f"{product['name']} додано до кошика",
        show_alert=True,
    )


# =========================
# ПРОСМОТР КОРЗИНЫ
# =========================

@dp.message(F.text == "🛒 Кошик")
async def cart_handler(message: Message) -> None:
    user_id = message.from_user.id
    cart = user_carts.get(user_id, [])

    if not cart:
        await message.answer(
            "🛒 Ваш кошик поки що порожній.\n\n"
            "Перейдіть до каталогу та додайте товари."
        )
        return

    cart_lines = []
    total = 0

    for number, product_id in enumerate(cart, start=1):
        product = find_product_by_id(product_id)

        if not product:
            continue

        cart_lines.append(
            f"{number}. {product['name']} — {product['price']} грн"
        )

        total += product["price"]

    text = (
        "🛒 <b>Ваш кошик</b>\n\n"
        + "\n".join(cart_lines)
        + f"\n\n💰 Разом: <b>{total} грн</b>"
    )

    await message.answer(
        text,
        parse_mode="HTML",
    )


# =========================
# ДРУГИЕ КНОПКИ
# =========================

@dp.message(F.text == "🌐 Сайт")
async def website_handler(message: Message) -> None:
    website_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🌐 Перейти на OKVEJ",
                    url="https://okvej.com.ua/",
                )
            ]
        ]
    )

    await message.answer(
        "Перейдіть до інтернет-магазину OKVEJ:",
        reply_markup=website_keyboard,
    )


@dp.message(F.text == "📢 Канал OKVEJ")
async def channel_handler(message: Message) -> None:
    channel_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Підписатися на канал",
                    url="https://t.me/okvej",
                )
            ]
        ]
    )

    await message.answer(
        "Новинки, акції та вигідні пропозиції:",
        reply_markup=channel_keyboard,
    )


@dp.message(F.text == "🔥 Акції")
async def sales_handler(message: Message) -> None:
    await message.answer(
        "🔥 Розділ акцій готується.\n\n"
        "Незабаром тут з'являться спеціальні пропозиції."
    )


@dp.callback_query(F.data == "catalog_sales")
async def catalog_sales_handler(callback: CallbackQuery) -> None:
    await callback.answer(
        "Акційні товари незабаром з'являться",
        show_alert=True,
    )


@dp.message(F.text == "🔍 Пошук товару")
async def search_handler(message: Message) -> None:
    await message.answer(
        "🔍 Пошук товарів підключимо наступним етапом."
    )


@dp.message(F.text == "💬 Менеджер")
async def manager_handler(message: Message) -> None:
    await message.answer(
        "💬 Для зв'язку з менеджером напишіть:\n"
        "@okvej"
    )


# =========================
# ЗАПУСК БОТА
# =========================

async def main() -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
