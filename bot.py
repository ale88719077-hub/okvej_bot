import asyncio
import logging
import os
from collections import defaultdict
from urllib.parse import urljoin

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from horoshop_api import HoroshopAPI

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

SITE_URL = "https://okvej.com.ua/"
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "sv000svbdd").lstrip("@")
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID")

bot = Bot(token=TOKEN)
dp = Dispatcher()
shop = HoroshopAPI(
    domain=os.getenv("HOROSHOP_DOMAIN", "okvej.com.ua"),
    login=os.getenv("HOROSHOP_LOGIN"),
    password=os.getenv("HOROSHOP_PASSWORD"),
)

# Корзины хранятся в памяти. После перезапуска Railway они очищаются.
user_carts = defaultdict(dict)
product_cache = {}


class SearchState(StatesGroup):
    waiting_query = State()


class CheckoutState(StatesGroup):
    waiting_name = State()
    waiting_phone = State()
    waiting_city = State()
    waiting_branch = State()
    waiting_comment = State()
    waiting_confirm = State()


main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍬 Каталог"), KeyboardButton(text="🚚 Доставка й оплата")],
        [KeyboardButton(text="🔍 Пошук товару"), KeyboardButton(text="🛒 Кошик")],
        [KeyboardButton(text="🌐 Сайт"), KeyboardButton(text="💬 Менеджер")],
        [KeyboardButton(text="📢 Канал OKVEJ")],
    ],
    resize_keyboard=True,
)


def localize(value):
    if isinstance(value, dict):
        return value.get("ua") or value.get("uk") or value.get("ru") or next(iter(value.values()), "")
    return value or ""


def product_link(product):
    link = localize(product.get("link") or product.get("url") or "")
    if not link:
        return SITE_URL
    return link if str(link).startswith("http") else urljoin(SITE_URL, str(link).lstrip("/"))


def product_key(product):
    raw = str(product.get("id") or product.get("article") or product_link(product))
    return str(abs(hash(raw)))


def price_number(product):
    value = product.get("price") or product.get("cost") or 0
    if isinstance(value, dict):
        value = value.get("value") or value.get("price") or next(iter(value.values()), 0)
    try:
        return float(str(value).replace("грн", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


def normalize_stock(value):
    value = localize(value)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0

    text = str(value).strip().lower()
    negatives = {
        "0", "false", "no", "none", "null", "not_available", "out_of_stock",
        "немає", "немає в наявності", "відсутній", "нет", "нет в наличии",
        "отсутствует", "не в наличии",
    }
    positives = {
        "1", "true", "yes", "available", "in_stock", "instock",
        "в наявності", "є в наявності", "наявний", "есть в наличии", "доступно",
    }
    if text in negatives:
        return False
    if text in positives:
        return True
    try:
        return float(text.replace(",", ".")) > 0
    except ValueError:
        return None


def is_in_stock(product):
    signals = [
        normalize_stock(product.get("presence")),
        normalize_stock(product.get("available")),
        normalize_stock(product.get("in_stock")),
        normalize_stock(product.get("stock")),
        normalize_stock(product.get("quantity")),
        normalize_stock(product.get("count")),
        normalize_stock(product.get("balance")),
    ]
    # Любой явный ноль/нет наличия блокирует товар.
    if False in signals:
        return False
    # Нужен хотя бы один положительный сигнал.
    return True in signals


async def get_all_products(max_items=2000, batch_size=500):
    products = []
    offset = 0
    while len(products) < max_items:
        batch = await shop.get_products(limit=batch_size, offset=offset)
        if not batch:
            break
        products.extend(batch)
        for product in batch:
            product_cache[product_key(product)] = product
        if len(batch) < batch_size:
            break
        offset += batch_size
        await asyncio.sleep(0.3)
    return products[:max_items]


def product_text(product):
    title = localize(product.get("title"))
    price = price_number(product)
    return (
        f"🍬 <b>{title}</b>\n\n"
        f"✅ В наявності\n"
        f"💰 Ціна: <b>{price:g} грн</b>\n"
        f"🔗 {product_link(product)}"
    )


def product_keyboard(product):
    key = product_key(product)
    product_cache[key] = product
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Додати в кошик", callback_data=f"add:{key}")],
        [InlineKeyboardButton(text="🛒 Купити на сайті", url=product_link(product))],
    ])


def cart_view(user_id):
    cart = user_carts[user_id]
    if not cart:
        text = "🛒 <b>Кошик порожній</b>\n\nЗнайдіть товар і натисніть «➕ Додати в кошик»."
    else:
        lines = ["🛒 <b>Ваш кошик</b>\n"]
        total = 0.0
        for key, qty in cart.items():
            product = product_cache.get(key)
            if not product:
                continue
            price = price_number(product)
            subtotal = price * qty
            total += subtotal
            lines.append(f"• <b>{localize(product.get('title'))}</b>\n  {qty} × {price:g} = {subtotal:g} грн")
        lines.append(f"\n💰 <b>Разом: {total:g} грн</b>")
        text = "\n".join(lines)

    rows = []
    for key in cart:
        product = product_cache.get(key)
        if product:
            title = localize(product.get("title"))[:25]
            rows.append([InlineKeyboardButton(text=f"➖ {title}", callback_data=f"remove:{key}")])
    rows.extend([
        [InlineKeyboardButton(text="🗑 Очистити кошик", callback_data="clear_cart")],
        [InlineKeyboardButton(text="✅ Оформити замовлення", callback_data="checkout_start")],
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("add:"))
async def add_to_cart(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    product = product_cache.get(key)
    if not product:
        await callback.answer("Товар не знайдено. Повторіть пошук.", show_alert=True)
        return
    if not is_in_stock(product):
        await callback.answer("Товару вже немає в наявності.", show_alert=True)
        return
    user_carts[callback.from_user.id][key] = user_carts[callback.from_user.id].get(key, 0) + 1
    await callback.answer("✅ Додано в кошик")


@dp.callback_query(F.data.startswith("remove:"))
async def remove_from_cart(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    cart = user_carts[callback.from_user.id]
    if key in cart:
        cart[key] -= 1
        if cart[key] <= 0:
            cart.pop(key, None)
    text, keyboard = cart_view(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "clear_cart")
async def clear_cart(callback: CallbackQuery):
    user_carts[callback.from_user.id].clear()
    text, keyboard = cart_view(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Кошик очищено")


def build_order_text(user_id: int, data: dict) -> str:
    cart = user_carts[user_id]
    lines = [
        "🆕 <b>Нове замовлення з Telegram</b>",
        "",
        f"👤 Ім'я: {data.get('name', '-')}",
        f"📞 Телефон: {data.get('phone', '-')}",
        f"🏙 Місто: {data.get('city', '-')}",
        f"📦 Відділення/адреса: {data.get('branch', '-')}",
        f"💬 Коментар: {data.get('comment', '-')}",
        "",
        "🛒 <b>Товари:</b>",
    ]

    total = 0.0
    for key, qty in cart.items():
        product = product_cache.get(key)
        if not product:
            continue
        price = price_number(product)
        subtotal = price * qty
        total += subtotal
        lines.append(
            f"• {localize(product.get('title'))}\n"
            f"  {qty} × {price:g} грн = {subtotal:g} грн\n"
            f"  {product_link(product)}"
        )

    lines.append("")
    lines.append(f"💰 <b>Разом: {total:g} грн</b>")
    lines.append(f"🆔 Telegram ID: <code>{user_id}</code>")
    return "\n".join(lines)


@dp.callback_query(F.data == "checkout_start")
async def checkout_start(callback: CallbackQuery, state: FSMContext):
    if not user_carts[callback.from_user.id]:
        await callback.answer("Кошик порожній.", show_alert=True)
        return

    await state.clear()
    await state.set_state(CheckoutState.waiting_name)
    await callback.message.answer("👤 Введіть ваше ім'я:")
    await callback.answer()


@dp.message(CheckoutState.waiting_name)
async def checkout_name(message: Message, state: FSMContext):
    await state.update_data(name=(message.text or "").strip())
    await state.set_state(CheckoutState.waiting_phone)
    await message.answer("📞 Введіть номер телефону:")


@dp.message(CheckoutState.waiting_phone)
async def checkout_phone(message: Message, state: FSMContext):
    phone = (message.text or "").strip()
    if len(phone) < 7:
        await message.answer("Введіть коректний номер телефону:")
        return
    await state.update_data(phone=phone)
    await state.set_state(CheckoutState.waiting_city)
    await message.answer("🏙 Вкажіть місто:")


@dp.message(CheckoutState.waiting_city)
async def checkout_city(message: Message, state: FSMContext):
    await state.update_data(city=(message.text or "").strip())
    await state.set_state(CheckoutState.waiting_branch)
    await message.answer("📦 Вкажіть відділення Нової пошти або адресу доставки:")


@dp.message(CheckoutState.waiting_branch)
async def checkout_branch(message: Message, state: FSMContext):
    await state.update_data(branch=(message.text or "").strip())
    await state.set_state(CheckoutState.waiting_comment)
    await message.answer(
        "💬 Додайте коментар до замовлення.\n"
        "Якщо коментаря немає — напишіть «немає»."
    )


@dp.message(CheckoutState.waiting_comment)
async def checkout_comment(message: Message, state: FSMContext):
    comment = (message.text or "").strip()
    await state.update_data(comment=comment)

    data = await state.get_data()
    order_text = build_order_text(message.from_user.id, data)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Підтвердити", callback_data="checkout_confirm")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="checkout_cancel")],
    ])

    await state.set_state(CheckoutState.waiting_confirm)
    await message.answer(
        "Перевірте замовлення:\n\n" + order_text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data == "checkout_cancel")
async def checkout_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("❌ Оформлення скасовано.")
    await callback.answer()


@dp.callback_query(F.data == "checkout_confirm")
async def checkout_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_text = build_order_text(callback.from_user.id, data)

    if MANAGER_CHAT_ID:
        try:
            await bot.send_message(
                int(MANAGER_CHAT_ID),
                order_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await callback.message.answer(
                "✅ Замовлення надіслано менеджеру.\n"
                "Ми зв'яжемося з вами для підтвердження."
            )
            user_carts[callback.from_user.id].clear()
            await state.clear()
        except Exception as e:
            logging.exception("Cannot send order to manager")
            await callback.message.answer(
                "❌ Не вдалося автоматично надіслати замовлення менеджеру.\n"
                f"Напишіть менеджеру: https://t.me/{MANAGER_USERNAME}\n\n"
                "Ваше замовлення:\n\n" + order_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    else:
        await callback.message.answer(
            "⚠️ У Railway не задано MANAGER_CHAT_ID.\n"
            f"Напишіть менеджеру: https://t.me/{MANAGER_USERNAME}\n\n"
            "Ваше замовлення:\n\n" + order_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    await callback.answer()


@dp.message(CommandStart())
async def start(message: Message):

    await message.answer("🍬 <b>Вітаємо в OKVEJ!</b>", parse_mode="HTML", reply_markup=main_menu)




@dp.message(Command("myid"))
async def my_id(message: Message):
    await message.answer(
        f"Ваш Telegram chat ID: <code>{message.chat.id}</code>",
        parse_mode="HTML",
    )


@dp.message(F.text == "🚚 Доставка й оплата")
async def delivery(message: Message):
    await message.answer(
        "🚚 <b>Доставка й оплата</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Відкрити умови", url="https://okvej.com.ua/ua/dostavka-i-oplata/")
        ]]),
    )


@dp.message(F.text == "🍬 Каталог")
async def catalog(message: Message):
    await message.answer(
        "🍬 Каталог OKVEJ",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍬 Цукерки вагові", url="https://okvej.com.ua/ua/konfety-vesovye/")],
            [InlineKeyboardButton(text="🍭 Карамель", url="https://okvej.com.ua/ua/karamel-v-miahkoi-upakovke/")],
            [InlineKeyboardButton(text="🎁 Подарунки", url="https://okvej.com.ua/ua/nabory-podarochnykh-konfet/")],
            [InlineKeyboardButton(text="🍪 Печиво", url="https://okvej.com.ua/ua/pechene-y-muchnye-yzdelyia/")],
            [InlineKeyboardButton(text="☁️ Зефір та мармелад", url="https://okvej.com.ua/ua/zefyr-y-marmelad/")],
            [InlineKeyboardButton(text="🍫 Шоколад", url="https://okvej.com.ua/ua/shokolad/")],
            [InlineKeyboardButton(text="🚚 Доставка й оплата", url="https://okvej.com.ua/ua/dostavka-i-oplata/")],
        ]),
    )


@dp.message(F.text == "🔍 Пошук товару")
async def search_start(message: Message, state: FSMContext):
    await state.set_state(SearchState.waiting_query)
    await message.answer("🔍 Введіть назву товару")


@dp.message(SearchState.waiting_query)
async def search_products(message: Message, state: FSMContext):
    query = (message.text or "").strip().lower()
    try:
        products = await get_all_products()
        results = [
            p for p in products
            if is_in_stock(p) and query in localize(p.get("title")).lower()
        ]
        if not results:
            await message.answer("😔 Нічого не знайдено в наявності.")
        else:
            for product in results[:10]:
                await message.answer(
                    product_text(product),
                    parse_mode="HTML",
                    reply_markup=product_keyboard(product),
                )
    except Exception as e:
        logging.exception("Search error")
        await message.answer(f"❌ Помилка: {e}")
    await state.clear()


@dp.message(F.text == "🛒 Кошик")
async def show_cart(message: Message):
    text, keyboard = cart_view(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@dp.message(F.text == "🌐 Сайт")
async def site(message: Message):
    await message.answer(SITE_URL)


@dp.message(F.text == "💬 Менеджер")
async def manager(message: Message):
    await message.answer(f"https://t.me/{MANAGER_USERNAME}")


@dp.message(F.text == "📢 Канал OKVEJ")
async def channel(message: Message):
    await message.answer("https://t.me/okvej")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
