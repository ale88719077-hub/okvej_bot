import asyncio
import logging
import os
import re
import json
import time
import hashlib
import html
from datetime import datetime, timezone, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET
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

BOT_VERSION = "11.4"
BOT_BUILD = "2026-07-14-site-new-badges-v2"

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
user_favorites = defaultdict(set)
user_recent = defaultdict(list)
product_cache = {}
catalog_products_cache = []
catalog_cache_until = 0.0

NEW_PRODUCTS_LIMIT = 50
NEW_PRODUCTS_CACHE_SECONDS = 6 * 60 * 60
new_products_cache = []
new_products_cache_time = 0.0


def product_page_has_new_badge(html_text, product):
    """
    Шукає «Новинка» біля основного товару, а не в рекомендаціях.

    Пріоритет:
    1. область навколо артикулу;
    2. область навколо H1/назви;
    3. структуровані CSS-класи badge/label у верхній частині сторінки.
    """
    article = str(product.get("article") or "").strip()
    title = clean_product_title(localize(product.get("title")))
    lowered = html_text.lower()

    regions = []

    if article:
        article_positions = [
            match.start()
            for match in re.finditer(
                re.escape(article.lower()),
                lowered,
            )
        ]
        for position in article_positions[:3]:
            regions.append(
                html_text[
                    max(0, position - 8000):
                    min(len(html_text), position + 12000)
                ]
            )

    if title:
        title_lower = title.lower()
        title_position = lowered.find(title_lower)
        if title_position >= 0:
            regions.append(
                html_text[
                    max(0, title_position - 8000):
                    min(len(html_text), title_position + 16000)
                ]
            )

    h1_position = lowered.find("<h1")
    if h1_position >= 0:
        regions.append(
            html_text[
                max(0, h1_position - 5000):
                min(len(html_text), h1_position + 22000)
            ]
        )

    # Верх сторінки зазвичай містить основну картку товару.
    regions.append(html_text[:50000])

    patterns = (
        r">\s*новинка\s*<",
        r"новинка",
        r"class=[\"'][^\"']*(?:badge|label|sticker|product-label)"
        r"[^\"']*[\"'][^>]*>[^<]{0,80}новинка",
        r"class=[\"'][^\"']*(?:new|novelty)[^\"']*[\"']",
    )

    for region in regions:
        region_lower = region.lower()

        # Відсікаємо блоки рекомендацій, якщо вони вже почалися.
        recommendation_markers = (
            "с этим товаром покупают",
            "з цим товаром купують",
            "рекомендуем",
            "рекомендуємо",
            "похожие товары",
            "схожі товари",
            "просмотренные товары",
            "переглянуті товари",
        )
        cut_positions = [
            region_lower.find(marker)
            for marker in recommendation_markers
            if region_lower.find(marker) >= 0
        ]
        if cut_positions:
            region = region[:min(cut_positions)]

        for pattern in patterns:
            if re.search(pattern, region, flags=re.IGNORECASE):
                return True

    return False


async def check_product_new_badge(session, product, semaphore):
    key = product_key(product)
    url = product_link(product)

    async with semaphore:
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with session.get(
                url,
                timeout=timeout,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 Chrome/126 Safari/537.36"
                    ),
                    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8",
                },
            ) as response:
                if response.status != 200:
                    return key, False

                html_text = await response.text(errors="ignore")
                return key, product_page_has_new_badge(
                    html_text,
                    product,
                )

        except Exception:
            logging.exception("New badge check failed for %s", url)
            return key, False


async def get_real_new_products(products, force_refresh=False):
    global new_products_cache, new_products_cache_time

    now = time.time()

    if (
        not force_refresh
        and new_products_cache_time
        and now - new_products_cache_time < NEW_PRODUCTS_CACHE_SECONDS
    ):
        return new_products_cache

    semaphore = asyncio.Semaphore(8)

    async with aiohttp.ClientSession() as session:
        tasks = [
            check_product_new_badge(session, product, semaphore)
            for product in products
        ]
        results = await asyncio.gather(*tasks)

    flags = dict(results)

    new_products_cache = [
        product
        for product in products
        if flags.get(product_key(product), False)
    ][:NEW_PRODUCTS_LIMIT]
    new_products_cache_time = now

    return new_products_cache

def new_products_keyboard(products, page=0):
    page_count = max(
        1,
        (len(products) + NEW_PRODUCTS_PAGE_SIZE - 1) // NEW_PRODUCTS_PAGE_SIZE,
    )
    page = max(0, min(page, page_count - 1))
    start = page * NEW_PRODUCTS_PAGE_SIZE
    page_items = products[start:start + NEW_PRODUCTS_PAGE_SIZE]

    rows = []
    for product in page_items:
        key = product_key(product)
        product_cache[key] = product
        title = clean_product_title(localize(product.get("title")))
        price = price_number(product)
        rows.append([
            InlineKeyboardButton(
                text=f"🆕 {title[:36]} — {price:g} грн",
                callback_data=f"new_product:{key}:{page}",
            )
        ])

    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Попередня",
                callback_data=f"new_page:{page - 1}",
            )
        )

    navigation.append(
        InlineKeyboardButton(
            text=f"Сторінка {page + 1}/{page_count}",
            callback_data="catalog_noop",
        )
    )

    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(
                text="Наступна ➡️",
                callback_data=f"new_page:{page + 1}",
            )
        )

    rows.append(navigation)
    rows.append([
        InlineKeyboardButton(
            text="🔄 Оновити новинки",
            callback_data="new_refresh",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="🍬 До каталогу",
            callback_data="catalog_categories",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text == "🆕 Новинки")
async def new_products_menu(message: Message):
    loading = await message.answer(
        "🔎 Перевіряю реальні позначки «Новинка» на сайті OKVEJ...\n"
        "Перший запуск може тривати до хвилини."
    )

    products = await get_in_stock_products()
    items = await get_real_new_products(products)

    if not items:
        await loading.edit_text(
            "🆕 Товарів із позначкою «Новинка» зараз не знайдено."
        )
        return

    await loading.edit_text(
        "🆕 <b>Новинки OKVEJ</b>\n\n"
        f"Знайдено товарів із позначкою «Новинка»: "
        f"<b>{len(items)}</b>",
        parse_mode="HTML",
        reply_markup=new_products_keyboard(items, 0),
    )


@dp.callback_query(F.data.startswith("new_page:"))
async def new_products_page(callback: CallbackQuery):
    page = int(callback.data.split(":", 1)[1])
    products = await get_in_stock_products()
    items = await get_real_new_products(products)

    await callback.message.edit_reply_markup(
        reply_markup=new_products_keyboard(items, page),
    )
    await callback.answer()


@dp.callback_query(F.data == "new_refresh")
async def new_products_refresh(callback: CallbackQuery):
    await callback.answer(
        "Оновлюю позначки новинок. Це може тривати до хвилини.",
        show_alert=True,
    )

    products = await get_in_stock_products(force_refresh=True)
    items = await get_real_new_products(products, force_refresh=True)

    if not items:
        await callback.message.edit_text(
            "🆕 Товарів із позначкою «Новинка» зараз не знайдено."
        )
        return

    await callback.message.edit_text(
        "🆕 <b>Новинки OKVEJ</b>\n\n"
        f"Знайдено товарів із позначкою «Новинка»: "
        f"<b>{len(items)}</b>",
        parse_mode="HTML",
        reply_markup=new_products_keyboard(items, 0),
    )


@dp.callback_query(F.data.startswith("new_product:"))
async def new_product_card(callback: CallbackQuery):
    _, key, page_text = callback.data.split(":", 2)
    product = product_cache.get(key)

    if not product:
        products = await get_in_stock_products()
        product = next(
            (item for item in products if product_key(item) == key),
            None,
        )

    if not product or not is_in_stock(product):
        await callback.answer("Товар уже недоступний.", show_alert=True)
        return

    image_url = get_image_url(product)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🛒 Додати в кошик",
            callback_data=f"add:{key}",
        )],
        [InlineKeyboardButton(
            text="❤️ В обране",
            callback_data=f"favorite_add:{key}",
        )],
        [InlineKeyboardButton(
            text="🌐 Відкрити на сайті",
            url=product_link(product),
        )],
        [InlineKeyboardButton(
            text="⬅️ До новинок",
            callback_data=f"new_page:{page_text}",
        )],
    ])

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
