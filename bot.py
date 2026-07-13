import asyncio
import logging
import os
import re
import json
import time
import hashlib
from collections import defaultdict
from urllib.parse import urljoin, urlparse, unquote

import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from html.parser import HTMLParser

from horoshop_api import HoroshopAPI

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

SITE_URL = "https://okvej.com.ua/"
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@okvej")
BOT_URL = "https://t.me/okvej_shop_bot"
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "sv000svbdd").lstrip("@")
MANAGER_CHAT_ID = (os.getenv("MANAGER_CHAT_ID") or "").strip()
logging.info("MANAGER_CHAT_ID configured: %s", bool(MANAGER_CHAT_ID))

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
catalog_products_cache = []
catalog_cache_until = 0.0

CATALOG_PAGE_SIZE = 8
CATALOG_CACHE_SECONDS = 600


class SearchState(StatesGroup):
    waiting_query = State()


class PostState(StatesGroup):
    waiting_link = State()


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
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]


def price_number(product):
    value = product.get("price") or product.get("cost") or 0
    if isinstance(value, dict):
        value = value.get("value") or value.get("price") or next(iter(value.values()), 0)
    try:
        return float(str(value).replace("грн", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


def normalize_stock(value):
    """
    Нормализует поле наличия Horoshop.

    Важно: Horoshop может вернуть presence не строкой, а словарём,
    например {"id": 2, "title": {"ua": "Немає в наявності"}}.
    Старый код мог взять первым значение id=2 и ошибочно считать товар доступным.
    """
    if value is None or value == "":
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value > 0

    if isinstance(value, dict):
        # Сначала проверяем человекочитаемые поля, а не id.
        preferred_keys = (
            "title",
            "name",
            "text",
            "label",
            "status",
            "value",
            "presence",
            "available",
            "in_stock",
            "stock",
            "quantity",
            "count",
            "balance",
        )

        results = []

        for key in preferred_keys:
            if key in value:
                result = normalize_stock(value.get(key))
                if result is not None:
                    results.append(result)

        # Затем проверяем языковые значения.
        for key in ("ua", "uk", "uk_UA", "ru", "ru_RU", "en"):
            if key in value:
                result = normalize_stock(value.get(key))
                if result is not None:
                    results.append(result)

        # Явное «нет» всегда важнее числового id.
        if False in results:
            return False
        if True in results:
            return True

        # id/code сами по себе не считаем доказательством наличия.
        return None

    if isinstance(value, (list, tuple, set)):
        results = [normalize_stock(item) for item in value]
        if False in results:
            return False
        if True in results:
            return True
        return None

    text = str(value).strip().lower()

    negatives = {
        "0",
        "false",
        "no",
        "none",
        "null",
        "not_available",
        "not available",
        "out_of_stock",
        "out of stock",
        "outofstock",
        "немає",
        "немає в наявності",
        "нема в наявності",
        "відсутній",
        "відсутня",
        "відсутнє",
        "нет",
        "нет в наличии",
        "не в наличии",
        "отсутствует",
        "відсутній на складі",
        "продано",
        "закінчився",
        "закінчилося",
    }

    positives = {
        "1",
        "true",
        "yes",
        "available",
        "in_stock",
        "in stock",
        "instock",
        "в наявності",
        "є в наявності",
        "наявний",
        "наявна",
        "наявне",
        "есть в наличии",
        "доступно",
        "available_for_order",
        "готово до відправки",
    }

    if text in negatives:
        return False

    if text in positives:
        return True

    # Дополнительная защита для длинных статусов.
    negative_fragments = (
        "немає в наявності",
        "нема в наявності",
        "нет в наличии",
        "не в наличии",
        "out of stock",
        "not available",
        "відсут",
        "закінчив",
        "продано",
    )
    if any(fragment in text for fragment in negative_fragments):
        return False

    positive_fragments = (
        "є в наявності",
        "в наявності",
        "есть в наличии",
        "in stock",
        "available",
    )
    if any(fragment in text for fragment in positive_fragments):
        return True

    try:
        return float(text.replace(",", ".")) > 0
    except ValueError:
        return None


def is_in_stock(product):
    """Для OKVEJ presence.id == 1 означает «В наявності»."""
    presence = product.get("presence")

    if isinstance(presence, dict):
        try:
            return int(presence.get("id")) == 1
        except (TypeError, ValueError):
            normalized = normalize_stock(presence.get("value"))
            if normalized is not None:
                return normalized

    normalized = normalize_stock(presence)
    if normalized is not None:
        return normalized

    for field in ("available", "in_stock", "stock", "quantity", "count", "balance"):
        signal = normalize_stock(product.get(field))
        if signal is not None:
            return signal

    return False


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


async def get_in_stock_products(force_refresh: bool = False):
    """Завантажує та кешує лише товари, які є в наявності."""
    global catalog_products_cache, catalog_cache_until

    now = time.time()
    if (
        not force_refresh
        and catalog_products_cache
        and now < catalog_cache_until
    ):
        return catalog_products_cache

    products = await get_all_products()
    available = [product for product in products if is_in_stock(product)]
    available.sort(key=lambda item: localize(item.get("title")).lower())

    catalog_products_cache = available
    catalog_cache_until = now + CATALOG_CACHE_SECONDS
    return available


def category_name(product):
    category = product.get("category") or product.get("categories")

    if isinstance(category, list):
        category = category[-1] if category else None

    if isinstance(category, dict):
        for key in ("title", "name", "value"):
            title = localize(category.get(key)).strip()
            if title:
                return title
        for key in ("ua", "uk", "ru"):
            title = str(category.get(key) or "").strip()
            if title:
                return title

    title = localize(category).strip()
    return title or "Інші товари"


def category_key(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


def grouped_categories(products):
    groups = {}
    for product in products:
        name = category_name(product)
        groups.setdefault(name, []).append(product)
    return dict(sorted(groups.items(), key=lambda item: item[0].lower()))


def categories_keyboard(products) -> InlineKeyboardMarkup:
    rows = []
    for name, items in grouped_categories(products).items():
        rows.append([
            InlineKeyboardButton(
                text=f"🍬 {name[:44]} ({len(items)})",
                callback_data=f"catalog_category:{category_key(name)}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="🌐 Відкрити весь каталог на сайті",
            url=SITE_URL,
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def find_category(products, key: str):
    for name, items in grouped_categories(products).items():
        if category_key(name) == key:
            return name, items
    return None, []


def catalog_page_keyboard(products, category_id: str, page: int) -> InlineKeyboardMarkup:
    total = len(products)
    page_count = max(1, (total + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * CATALOG_PAGE_SIZE
    page_items = products[start:start + CATALOG_PAGE_SIZE]

    rows = []
    for product in page_items:
        key = product_key(product)
        product_cache[key] = product
        title = localize(product.get("title")).strip() or "Товар"
        price = price_number(product)
        rows.append([
            InlineKeyboardButton(
                text=f"{title[:39]} — {price:g} грн",
                callback_data=f"catalog_product:{key}:{category_id}:{page}",
            )
        ])

    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"catalog_page:{category_id}:{page - 1}",
            )
        )
    navigation.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{page_count}",
            callback_data="catalog_noop",
        )
    )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"catalog_page:{category_id}:{page + 1}",
            )
        )

    rows.append(navigation)
    rows.append([
        InlineKeyboardButton(
            text="⬅️ До категорій",
            callback_data="catalog_categories",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def catalog_page_text(category_title: str, products, page: int) -> str:
    total = len(products)
    page_count = max(1, (total + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * CATALOG_PAGE_SIZE + 1
    end = min((page + 1) * CATALOG_PAGE_SIZE, total)

    return (
        f"🍬 <b>{category_title}</b>\n\n"
        f"✅ У наявності: <b>{total}</b>\n"
        f"Показано: <b>{start}–{end}</b>\n\n"
        "Оберіть товар:"
    )


def get_image_url(product):
    images = product.get("images") or product.get("image") or product.get("photo")
    url = None

    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            url = (
                first.get("url")
                or first.get("src")
                or first.get("image")
                or first.get("big")
            )
        else:
            url = str(first)
    elif isinstance(images, dict):
        url = (
            images.get("url")
            or images.get("src")
            or images.get("image")
            or images.get("big")
        )
    elif isinstance(images, str):
        url = images

    if not url:
        return None

    if str(url).startswith("http"):
        return str(url)

    return urljoin(SITE_URL, str(url).lstrip("/"))


def normalize_url(url):
    return str(url or "").strip().rstrip("/").lower()


def canonical_product_path(url):
    """
    Сравнивает ссылки независимо от домена, /ua/, /ru/, завершающего слэша
    и параметров после знака ?.
    """
    parsed = urlparse(str(url or "").strip())
    path = unquote(parsed.path).strip("/").lower()
    parts = [part for part in path.split("/") if part]

    if parts and parts[0] in {"ua", "ru", "uk"}:
        parts = parts[1:]

    return "/".join(parts)


class ProductPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.json_ld_parts = []
        self._inside_json_ld = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)

        if tag.lower() == "meta":
            key = (
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
            )
            content = attributes.get("content")

            if key and content:
                self.meta[str(key).lower()] = content.strip()

        if tag.lower() == "script":
            script_type = str(attributes.get("type", "")).lower()
            if "ld+json" in script_type:
                self._inside_json_ld = True

    def handle_endtag(self, tag):
        if tag.lower() == "script":
            self._inside_json_ld = False

    def handle_data(self, data):
        if self._inside_json_ld:
            self.json_ld_parts.append(data)


def clean_page_title(title):
    title = str(title or "").strip()

    for separator in (" | OKVEJ", " — OKVEJ", " - OKVEJ"):
        if separator.lower() in title.lower():
            index = title.lower().find(separator.lower())
            title = title[:index].strip()

    return title


def find_price_in_json(value):
    if isinstance(value, dict):
        if "price" in value:
            price = value.get("price")
            if isinstance(price, (str, int, float)):
                return str(price)

        for child in value.values():
            result = find_price_in_json(child)
            if result:
                return result

    elif isinstance(value, list):
        for child in value:
            result = find_price_in_json(child)
            if result:
                return result

    return None


async def load_product_from_page(link):
    """
    Запасной способ: читает название, фото и цену прямо со страницы товара.
    Используется, если ссылка товара не совпала со ссылкой из API Хорошоп.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/126 Safari/537.36"
        )
    }

    timeout = aiohttp.ClientTimeout(total=25)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(link, allow_redirects=True) as response:
            if response.status >= 400:
                raise RuntimeError(f"Страница товара вернула HTTP {response.status}")

            html = await response.text()
            final_link = str(response.url)

    parser = ProductPageParser()
    parser.feed(html)

    title = clean_page_title(
        parser.meta.get("og:title")
        or parser.meta.get("twitter:title")
        or parser.meta.get("title")
    )

    image_url = (
        parser.meta.get("og:image")
        or parser.meta.get("twitter:image")
        or parser.meta.get("image")
    )

    price = (
        parser.meta.get("product:price:amount")
        or parser.meta.get("product:price")
        or parser.meta.get("price")
    )

    if not price:
        for raw_json in parser.json_ld_parts:
            try:
                json_data = json.loads(raw_json.strip())
            except (json.JSONDecodeError, TypeError):
                continue

            price = find_price_in_json(json_data)
            if price:
                break

    if not price:
        price_match = re.search(
            r'(?:"price"|itemprop=["\\\']price["\\\'])[^0-9]{0,50}([0-9]+(?:[.,][0-9]+)?)',
            html,
            flags=re.IGNORECASE,
        )
        if price_match:
            price = price_match.group(1)

    if image_url and not image_url.startswith("http"):
        image_url = urljoin(final_link, image_url)

    if not title:
        raise RuntimeError("Не удалось прочитать название товара со страницы")

    return {
        "title": title,
        "price": str(price or "").replace(",", ".").strip(),
        "image_url": image_url,
        "link": final_link,
    }


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
    manager_chat_id = (MANAGER_CHAT_ID or "").strip()

    if not manager_chat_id:
        await callback.message.answer(
            "⚠️ У Railway не задано MANAGER_CHAT_ID.\n"
            f"Напишіть менеджеру: https://t.me/{MANAGER_USERNAME}\n\n"
            "Ваше замовлення:\n\n" + order_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await callback.answer()
        return

    try:
        chat_id = int(manager_chat_id)

        await bot.send_message(
            chat_id=chat_id,
            text=order_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        await callback.message.answer(
            "✅ Замовлення надіслано менеджеру.\n"
            "Ми зв'яжемося з вами для підтвердження."
        )

        user_carts[callback.from_user.id].clear()
        await state.clear()

    except ValueError:
        logging.exception("MANAGER_CHAT_ID is not an integer")
        await callback.message.answer(
            "❌ MANAGER_CHAT_ID має містити тільки цифри.\n"
            "Перевірте значення змінної в Railway."
        )

    except Exception as e:
        logging.exception("Cannot send order to manager")
        await callback.message.answer(
            "❌ Не вдалося автоматично надіслати замовлення менеджеру.\n"
            f"Помилка: {e}\n\n"
            f"Напишіть менеджеру: https://t.me/{MANAGER_USERNAME}\n\n"
            "Ваше замовлення:\n\n" + order_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    await callback.answer()



@dp.message(F.text == "/пост")
async def manual_post_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(PostState.waiting_link)
    await message.answer(
        "Пришлите ссылку на нужный товар с сайта OKVEJ.\n\n"
        "Например:\n"
        "https://okvej.com.ua/ua/nazvanie-tovara/"
    )


@dp.message(PostState.waiting_link)
async def manual_post_publish(message: Message, state: FSMContext):
    link = (message.text or "").strip()

    if not link.startswith("http"):
        await message.answer("Пришлите полную ссылку на товар, начинающуюся с http.")
        return

    await message.answer("🔎 Ищу товар в каталоге...")

    try:
        products = await get_all_products()
        target_path = canonical_product_path(link)
        product = None

        for item in products:
            if canonical_product_path(product_link(item)) == target_path:
                product = item
                break

        if product:
            title = localize(product.get("title"))
            price = price_number(product)
            image_url = get_image_url(product)
            final_link = product_link(product)
            price_line = f"💰 Цена: <b>{price:g} грн</b>\n\n"
        else:
            # Если API возвращает другую языковую ссылку или другой slug,
            # читаем данные непосредственно со страницы товара.
            page_product = await load_product_from_page(link)
            title = page_product["title"]
            image_url = page_product["image_url"]
            final_link = page_product["link"]
            raw_price = page_product["price"]

            if raw_price:
                try:
                    formatted_price = f"{float(raw_price):g}"
                except ValueError:
                    formatted_price = raw_price
                price_line = f"💰 Цена: <b>{formatted_price} грн</b>\n\n"
            else:
                price_line = "💰 Актуальная цена указана на сайте\n\n"

        post_text = (
            f"🍬 <b>{title}</b>\n\n"
            f"{price_line}"
            f"🔗 Заказать:\n{final_link}"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="🛒 Купить",
                    url=final_link,
                )],
                [InlineKeyboardButton(
                    text="💬 Менеджер",
                    url=f"https://t.me/{MANAGER_USERNAME}",
                )],
                [InlineKeyboardButton(
                    text="🤖 Открыть бота",
                    url=BOT_URL,
                )],
            ]
        )

        if image_url:
            await bot.send_photo(
                CHANNEL_USERNAME,
                photo=image_url,
                caption=post_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await bot.send_message(
                CHANNEL_USERNAME,
                post_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        await message.answer("✅ Товар опубликован в канале.")
        await state.clear()

    except Exception as e:
        logging.exception("Manual product post error")
        await message.answer(f"❌ Ошибка публикации: {e}")


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
    loading = await message.answer("⏳ Завантажую каталог...")

    try:
        products = await get_in_stock_products()
        groups = grouped_categories(products)

        await loading.edit_text(
            "🍬 <b>Каталог OKVEJ</b>\n\n"
            f"✅ У наявності: <b>{len(products)}</b> товарів\n"
            f"📂 Категорій: <b>{len(groups)}</b>\n\n"
            "Оберіть категорію:",
            parse_mode="HTML",
            reply_markup=categories_keyboard(products),
        )
    except Exception:
        logging.exception("Catalog loading error")
        await loading.edit_text(
            "❌ Не вдалося завантажити каталог. "
            "Спробуйте ще раз трохи пізніше."
        )


@dp.callback_query(F.data == "catalog_categories")
async def catalog_categories(callback: CallbackQuery):
    products = await get_in_stock_products()
    groups = grouped_categories(products)

    await callback.message.edit_text(
        "🍬 <b>Каталог OKVEJ</b>\n\n"
        f"✅ У наявності: <b>{len(products)}</b> товарів\n"
        f"📂 Категорій: <b>{len(groups)}</b>\n\n"
        "Оберіть категорію:",
        parse_mode="HTML",
        reply_markup=categories_keyboard(products),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("catalog_category:"))
async def catalog_category(callback: CallbackQuery):
    category_id = callback.data.split(":", 1)[1]
    products = await get_in_stock_products()
    title, category_products = find_category(products, category_id)

    if not category_products:
        await callback.answer("Категорію не знайдено.", show_alert=True)
        return

    await callback.message.edit_text(
        catalog_page_text(title, category_products, 0),
        parse_mode="HTML",
        reply_markup=catalog_page_keyboard(category_products, category_id, 0),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("catalog_page:"))
async def catalog_page(callback: CallbackQuery):
    try:
        _, category_id, page_text = callback.data.split(":", 2)
        page = int(page_text)
        products = await get_in_stock_products()
        title, category_products = find_category(products, category_id)

        if not category_products:
            await callback.answer("Категорію не знайдено.", show_alert=True)
            return

        await callback.message.edit_text(
            catalog_page_text(title, category_products, page),
            parse_mode="HTML",
            reply_markup=catalog_page_keyboard(category_products, category_id, page),
        )
        await callback.answer()
    except Exception:
        logging.exception("Catalog page error")
        await callback.answer(
            "Не вдалося відкрити сторінку каталогу.",
            show_alert=True,
        )


@dp.callback_query(F.data == "catalog_noop")
async def catalog_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("catalog_product:"))
async def catalog_product(callback: CallbackQuery):
    try:
        _, key, category_id, page_text = callback.data.split(":", 3)
        product = product_cache.get(key)

        if not product:
            products = await get_in_stock_products()
            product = next(
                (item for item in products if product_key(item) == key),
                None,
            )

        if not product or not is_in_stock(product):
            await callback.answer(
                "Цього товару вже немає в наявності.",
                show_alert=True,
            )
            return

        page = int(page_text)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="➕ Додати в кошик",
                callback_data=f"add:{key}",
            )],
            [InlineKeyboardButton(
                text="🛒 Купити на сайті",
                url=product_link(product),
            )],
            [InlineKeyboardButton(
                text="⬅️ Назад до категорії",
                callback_data=f"catalog_page:{category_id}:{page}",
            )],
        ])

        image_url = get_image_url(product)
        if image_url:
            await callback.message.answer_photo(
                photo=image_url,
                caption=product_text(product),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await callback.message.answer(
                product_text(product),
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        await callback.answer()
    except Exception:
        logging.exception("Catalog product error")
        await callback.answer(
            "Не вдалося відкрити товар.",
            show_alert=True,
        )


@dp.message(F.text == "🔍 Пошук товару")
async def search_start(message: Message, state: FSMContext):
    await state.set_state(SearchState.waiting_query)
    await message.answer("🔍 Введіть назву товару")


@dp.message(SearchState.waiting_query)
async def search_products(message: Message, state: FSMContext):
    query = (message.text or "").strip().lower()
    try:
        products = await get_in_stock_products()
        results = [
            p for p in products
            if query in localize(p.get("title")).lower()
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
    logging.info("Starting OKVEJ bot")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
