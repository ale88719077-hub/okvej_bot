import asyncio
import logging
import os
import re
import json
import time
import hashlib
import html
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, unquote

import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, WebAppInfo,
)

from html.parser import HTMLParser
from aiohttp import web

from horoshop_api import HoroshopAPI

BOT_VERSION = "18.7"
BOT_BUILD = "2026-07-21-orders-owner-manager-requirements-fix"

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

SITE_URL = "https://okvej.com.ua/"
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@okvej")
BOT_URL = "https://t.me/okvej_shop_bot"
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "sv000svbdd").lstrip("@")
MANAGER_CHAT_ID = (os.getenv("MANAGER_CHAT_ID") or "").strip()
ADMIN_USER_ID = (os.getenv("ADMIN_USER_ID") or "").strip()
MINI_APP_URL = (os.getenv("MINI_APP_URL") or "").strip()
PORT = int(os.getenv("PORT", "8080"))

ADMIN_DATA_PATH = Path(
    os.getenv("ADMIN_DATA_PATH", "/data/admin_data.json")
)
if not ADMIN_DATA_PATH.parent.exists():
    ADMIN_DATA_PATH = Path("admin_data.json")

ANALYTICS_DATA_PATH = Path(
    os.getenv("ANALYTICS_DATA_PATH", "/data/analytics_data.json")
)
if not ANALYTICS_DATA_PATH.parent.exists():
    ANALYTICS_DATA_PATH = Path("analytics_data.json")


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
user_catalog_album_messages = defaultdict(list)
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


def channel_public_url():
    username = CHANNEL_USERNAME.strip()
    if username.startswith("https://t.me/"):
        return username
    return f"https://t.me/{username.lstrip('@')}"


def subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Підписатися", url=channel_public_url())],
        [InlineKeyboardButton(text="✅ Я підписався", callback_data="subscription_check")],
        [InlineKeyboardButton(text="➡️ Продовжити без підписки", callback_data="subscription_skip")],
    ])


def subscription_welcome_text():
    return (
        "🍬 <b>Ласкаво просимо до OKVEJ!</b>\n\n"
        "📢 Підпишіться на наш канал, щоб першими дізнаватися про:\n\n"
        "🔥 акції\n"
        "🆕 новинки\n"
        "📦 нові надходження\n\n"
        "Після підписки натисніть кнопку <b>«✅ Я підписався»</b>."
    )


async def user_is_channel_member(user_id: int):
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in {"member", "administrator", "creator", "restricted"}
    except Exception:
        logging.exception("Could not check channel subscription for user %s", user_id)
        return None


main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🌐 Сайт")],
        [
            KeyboardButton(text="🍬 Каталог"),
            KeyboardButton(text="🔍 Пошук товару"),
        ],
        [
            KeyboardButton(text="🆕 Новинки"),
            KeyboardButton(text="🔥 Хіти"),
        ],
        [
            KeyboardButton(text="❤️ Обране"),
            KeyboardButton(text="🕒 Переглянуті"),
        ],
        [
            KeyboardButton(text="🛒 Кошик"),
            KeyboardButton(text="🚚 Доставка й оплата"),
        ],
        [
            KeyboardButton(text="💬 Менеджер"),
            KeyboardButton(text="📢 Канал OKVEJ"),
        ],
        [
            KeyboardButton(text="🛍 Відкрити магазин", web_app=WebAppInfo(url=MINI_APP_URL)) if MINI_APP_URL else KeyboardButton(text="🛍 Відкрити магазин"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
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


def is_admin(user_id):
    return bool(ADMIN_USER_ID) and str(user_id).strip() == ADMIN_USER_ID


def default_admin_data():
    return {
        "new_products": [],
        "hits": [],
        "recommended": [],
    }


def load_admin_data():
    data = default_admin_data()

    try:
        if ADMIN_DATA_PATH.exists():
            saved = json.loads(
                ADMIN_DATA_PATH.read_text(encoding="utf-8")
            )
            if isinstance(saved, dict):
                for section in data:
                    values = saved.get(section, [])
                    if isinstance(values, list):
                        data[section] = [
                            str(value).strip()
                            for value in values
                            if str(value).strip()
                        ]
    except Exception:
        logging.exception("Failed to load admin_data.json")

    return data


def save_admin_data():
    try:
        ADMIN_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        ADMIN_DATA_PATH.write_text(
            json.dumps(admin_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:
        logging.exception("Failed to save admin_data.json")
        return False


admin_data = load_admin_data()

def default_analytics_data():
    return {"started_at": datetime.now(timezone.utc).isoformat(), "users": [], "events": {}, "products": {}, "searches": {}, "daily": {}}

def load_analytics_data():
    data = default_analytics_data()
    try:
        if ANALYTICS_DATA_PATH.exists():
            saved = json.loads(ANALYTICS_DATA_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict): data.update(saved)
    except Exception:
        logging.exception("Failed to load analytics_data.json")
    for key, default in (("users", []), ("events", {}), ("products", {}), ("searches", {}), ("daily", {})):
        if not isinstance(data.get(key), type(default)): data[key] = default
    return data

def save_analytics_data():
    try:
        ANALYTICS_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        ANALYTICS_DATA_PATH.write_text(json.dumps(analytics_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        logging.exception("Failed to save analytics_data.json")
        return False

analytics_data = load_analytics_data()

def analytics_today_key():
    return datetime.now(timezone.utc).date().isoformat()

def track_event(event_name, user_id=None, product=None, query=None):
    try:
        if user_id is not None:
            uid=str(user_id)
            if uid not in analytics_data["users"]: analytics_data["users"].append(uid)
        analytics_data["events"][event_name]=int(analytics_data["events"].get(event_name,0))+1
        day=analytics_today_key(); daily=analytics_data["daily"].setdefault(day,{"users":[],"events":{},"products":{},"searches":{}})
        if user_id is not None:
            uid=str(user_id)
            if uid not in daily["users"]: daily["users"].append(uid)
        daily["events"][event_name]=int(daily["events"].get(event_name,0))+1
        if product:
            article=product_article(product) or product_key(product); title=clean_product_title(localize(product.get("title")))
            rec=analytics_data["products"].setdefault(article,{"title":title,"views":0,"cart_adds":0,"favorites":0}); rec["title"]=title
            drec=daily["products"].setdefault(article,{"title":title,"views":0,"cart_adds":0,"favorites":0}); drec["title"]=title
            metric={"product_view":"views","cart_add":"cart_adds","favorite_add":"favorites"}.get(event_name)
            if metric:
                rec[metric]=int(rec.get(metric,0))+1; drec[metric]=int(drec.get(metric,0))+1
        if query:
            q=str(query).strip().lower()
            if q:
                analytics_data["searches"][q]=int(analytics_data["searches"].get(q,0))+1
                daily["searches"][q]=int(daily["searches"].get(q,0))+1
        save_analytics_data()
    except Exception:
        logging.exception("Analytics tracking error")

def top_items(mapping, metric=None, limit=5):
    items=[]
    for key,value in mapping.items():
        if isinstance(value,dict): score=int(value.get(metric or "views",0)); label=value.get("title") or key
        else: score=int(value); label=key
        items.append((label,score))
    return sorted(items,key=lambda x:(-x[1],str(x[0]).lower()))[:limit]

def analytics_report(data,title):
    e=data.get("events",{}); users=data.get("users",[]); products=data.get("products",{}); searches=data.get("searches",{})
    lines=[f"📊 <b>{title}</b>","",f"👥 Користувачів: <b>{len(users)}</b>",f"▶️ Запусків: <b>{e.get('start',0)}</b>",f"🍬 Відкриттів каталогу: <b>{e.get('catalog_open',0)}</b>",f"🔍 Пошуків: <b>{e.get('search',0)}</b>",f"👆 Переглядів товарів: <b>{e.get('product_view',0)}</b>",f"🛒 Додавань у кошик: <b>{e.get('cart_add',0)}</b>",f"❤️ Додавань в обране: <b>{e.get('favorite_add',0)}</b>",f"✅ Початих оформлень: <b>{e.get('checkout_start',0)}</b>",f"📢 Публікацій у канал: <b>{e.get('channel_post',0)}</b>","","📈 <b>Підписка через бота:</b>",f"👀 Показів пропозиції: <b>{e.get('subscription_offer',0)}</b>",f"🔎 Перевірок підписки: <b>{e.get('subscription_check',0)}</b>",f"✅ Підтверджених підписок: <b>{e.get('subscription_confirmed',0)}</b>",f"⏭️ Продовжили без підписки: <b>{e.get('subscription_skip',0)}</b>"]
    tp=top_items(products,"views",5)
    if tp:
        lines += ["","🔥 <b>Топ товарів:</b>"]+[f"{i}. {html.escape(str(label)[:70])} — {score}" for i,(label,score) in enumerate(tp,1)]
    ts=top_items(searches,limit=5)
    if ts:
        lines += ["","🔎 <b>Топ пошуку:</b>"]+[f"{i}. {html.escape(str(label)[:50])} — {score}" for i,(label,score) in enumerate(ts,1)]
    return "\n".join(lines)


def product_article(product):
    return str(product.get("article") or "").strip()


def section_articles(section):
    return set(admin_data.get(section, []))


def product_in_section(product, section):
    article = product_article(product)
    return bool(article) and article in section_articles(section)


def set_product_section(product, section, enabled):
    article = product_article(product)
    if not article:
        return False

    values = section_articles(section)
    if enabled:
        values.add(article)
    else:
        values.discard(article)

    admin_data[section] = sorted(values)
    return save_admin_data()


def products_from_section(products, section):
    by_article = {
        product_article(product): product
        for product in products
        if product_article(product)
    }

    return [
        by_article[article]
        for article in admin_data.get(section, [])
        if article in by_article and is_in_stock(by_article[article])
    ]


def admin_section_buttons(product, user_id):
    if not is_admin(user_id):
        return []

    key = product_key(product)
    rows = []

    rows.append([
        InlineKeyboardButton(
            text=(
                "❌ Прибрати з новинок"
                if product_in_section(product, "new_products")
                else "🆕 Додати в новинки"
            ),
            callback_data=(
                f"admin_section:new_products:"
                f"{'remove' if product_in_section(product, 'new_products') else 'add'}:{key}"
            ),
        )
    ])

    rows.append([
        InlineKeyboardButton(
            text=(
                "❌ Прибрати з хітів"
                if product_in_section(product, "hits")
                else "🔥 Додати в хіти"
            ),
            callback_data=(
                f"admin_section:hits:"
                f"{'remove' if product_in_section(product, 'hits') else 'add'}:{key}"
            ),
        )
    ])

    return rows



def price_number(product):
    value = product.get("price") or product.get("cost") or 0
    if isinstance(value, dict):
        value = value.get("value") or value.get("price") or next(iter(value.values()), 0)
    try:
        return float(str(value).replace("грн", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


ADMIN_SECTION_PAGE_SIZE = 8


def admin_section_keyboard(products, section, page=0):
    page_count = max(
        1,
        (len(products) + ADMIN_SECTION_PAGE_SIZE - 1)
        // ADMIN_SECTION_PAGE_SIZE,
    )
    page = max(0, min(page, page_count - 1))
    start = page * ADMIN_SECTION_PAGE_SIZE
    page_items = products[start:start + ADMIN_SECTION_PAGE_SIZE]

    rows = []
    icon = "🆕" if section == "new_products" else "🔥"

    for product in page_items:
        key = product_key(product)
        product_cache[key] = product
        title = clean_product_title(localize(product.get("title")))
        price = price_number(product)

        rows.append([
            InlineKeyboardButton(
                text=f"{icon} {title[:36]} — {price:g} грн",
                callback_data=f"admin_product:{section}:{key}:{page}",
            )
        ])

    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Попередня",
                callback_data=f"admin_section_page:{section}:{page - 1}",
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
                callback_data=f"admin_section_page:{section}:{page + 1}",
            )
        )

    rows.append(navigation)
    rows.append([
        InlineKeyboardButton(
            text="🍬 До каталогу",
            callback_data="catalog_categories",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_keyboard(product, user_id, back_button=None):
    key = product_key(product)
    product_cache[key] = product

    rows = [
        [InlineKeyboardButton(
            text="🛒 Купити зараз",
            callback_data=f"add:{key}",
        )],
        [InlineKeyboardButton(
            text="❤️ В обране",
            callback_data=f"favorite_add:{key}",
        )],
    ]

    rows.extend(admin_section_buttons(product, user_id))

    rows.extend([
        [InlineKeyboardButton(
            text="🌐 Відкрити на сайті",
            url=product_link(product),
        )],
        [InlineKeyboardButton(
            text="🛍 Перейти до кошика",
            callback_data="open_cart",
        )],
    ])

    if back_button:
        rows.append([back_button])

    return InlineKeyboardMarkup(inline_keyboard=rows)



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


CATEGORY_SLUG_NAMES = {
    "konfety-vesovye": "Цукерки",
    "konfety": "Цукерки",
    "karamel-v-miahkoi-upakovke": "Карамель",
    "karamel": "Карамель",
    "nabory-podarochnykh-konfet": "Подарункові набори",
    "podarochnye-nabory": "Подарункові набори",
    "pechene-y-muchnye-yzdelyia": "Печиво",
    "pechene": "Печиво",
    "zefyr-y-marmelad": "Зефір та мармелад",
    "zefir": "Зефір та мармелад",
    "marmelad": "Зефір та мармелад",
    "shokolad": "Шоколад",
    "vafli": "Вафлі",
    "keksy": "Кекси та випічка",
    "torty": "Торти",
    "napitki": "Напої",
    "orehi-i-suhofrukty": "Горіхи та сухофрукти",
    "zhvachka": "Жувальна гумка",
    "batonchiki": "Батончики",
}


TITLE_CATEGORY_RULES = [
    ("Печиво", (
        "печиво", "печенье", "крекер", "галет", "cookie", "cookies",
        "biscuit", "biskvitne pechyvo",
    )),
    ("Вафлі", (
        "вафл", "wafer", "wafers", "vafli",
    )),
    ("Кекси та випічка", (
        "кекс", "мафін", "маффин", "рулет", "слойк", "булоч",
        "випіч", "выпеч", "croissant", "круасан", "cake",
    )),
    ("Зефір та мармелад", (
        "зефір", "зефир", "мармелад", "пастил", "jelly", "gummy",
    )),
    ("Шоколад", (
        "шоколад", "chocolate", "shokolad",
    )),
    ("Карамель", (
        "карамел", "льодяник", "леденц", "lollipop", "candy drops",
    )),
    ("Драже", (
        "драже", "drazhe", "dragee",
    )),
    ("Батончики", (
        "батончик", "batonchyk", "bar ", "bars ",
    )),
    ("Жувальна гумка", (
        "жувальн", "жевательн", "жвач", "gum",
    )),
    ("Горіхи та сухофрукти", (
        "горіх", "орех", "арахіс", "арахис", "фісташ", "фисташ",
        "мигдал", "миндал", "курага", "родзин", "изюм", "сухофрукт",
    )),
    ("Напої", (
        "напій", "напиток", "чай", "кава", "кофе", "cappuccino",
        "какао", "drink",
    )),
    ("Подарункові набори", (
        "подарунк", "подарочн", "набір", "набор", "gift",
    )),
    ("Цукерки", (
        "цукерк", "конфет", "truffle", "трюфел", "праліне", "пралине",
        "ірис", "ирис", "toffee", "fudge",
    )),
]


def normalize_title_for_category(value: str) -> str:
    value = unquote(str(value or "")).lower()
    value = value.replace("-", " ").replace("_", " ")
    return " ".join(value.split())


FIRST_WORD_CATEGORY_RULES = {
    "цукерки": "Цукерки",
    "конфеты": "Цукерки",
    "конфета": "Цукерки",
    "шоколад": "Шоколад",
    "печиво": "Печиво",
    "печенье": "Печиво",
    "карамель": "Карамель",
    "ірис": "Карамель",
    "ирис": "Карамель",
    "зефір": "Зефір та мармелад",
    "зефир": "Зефір та мармелад",
    "мармелад": "Зефір та мармелад",
    "вафлі": "Вафлі",
    "вафли": "Вафлі",
    "драже": "Драже",
    "жувальна": "Жувальна гумка",
    "жевательная": "Жувальна гумка",
    "горіх": "Горіхи та сухофрукти",
    "горіхи": "Горіхи та сухофрукти",
    "орех": "Горіхи та сухофрукти",
    "орехи": "Горіхи та сухофрукти",
    "сухофрукти": "Горіхи та сухофрукти",
    "сухофрукты": "Горіхи та сухофрукти",
    "напій": "Напої",
    "напої": "Напої",
    "напиток": "Напої",
    "напитки": "Напої",
    "батончик": "Батончики",
    "батончики": "Батончики",
    "кекс": "Кекси та випічка",
    "кекси": "Кекси та випічка",
    "торт": "Кекси та випічка",
    "рулет": "Кекси та випічка",
    "набір": "Подарункові набори",
    "набор": "Подарункові набори",
    "подарунковий": "Подарункові набори",
    "подарочный": "Подарункові набори",
}


def category_from_title(product):
    title = clean_product_title(localize(product.get("title")))
    normalized = normalize_title_for_category(title).strip()
    if not normalized:
        return None

    first_word = normalized.split()[0].strip(" \"'«»()[]{}.,:;_-")
    return FIRST_WORD_CATEGORY_RULES.get(first_word)



def category_from_link(product):
    """
    Использует URL только если в нём действительно есть отдельный сегмент
    категории. Один последний сегмент обычно является slug самого товара.
    """
    link = product_link(product)
    path = unquote(urlparse(link).path).strip("/")
    parts = [part for part in path.split("/") if part]

    if parts and parts[0] in ("ua", "uk", "ru"):
        parts = parts[1:]

    # Нужны минимум два сегмента: категория/товар.
    if len(parts) < 2:
        return None

    category_slug = parts[-2].lower()

    if category_slug in CATEGORY_SLUG_NAMES:
        return CATEGORY_SLUG_NAMES[category_slug]

    # Не создаём отдельную категорию из неизвестного slug.
    return None


def category_name(product):
    title_category = category_from_title(product)
    if title_category:
        return title_category

    category = (
        product.get("category")
        or product.get("categories")
        or product.get("main_category")
        or product.get("category_name")
    )

    if isinstance(category, list):
        category = category[-1] if category else None

    if isinstance(category, dict):
        for key in ("title", "name", "value"):
            value = localize(category.get(key)).strip()
            if value:
                return value

    return category_from_link(product) or "Інші товари"



def category_key(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


CATEGORY_PRIORITY = [
    "Цукерки",
    "Подарункові набори",
    "Печиво",
    "Карамель",
    "Шоколад",
    "Зефір та мармелад",
    "Кекси та випічка",
    "Вафлі",
    "Драже",
    "Жувальна гумка",
    "Горіхи та сухофрукти",
    "Напої",
    "Батончики",
    "Інші товари",
]


def category_sort_key(name: str):
    try:
        return (0, CATEGORY_PRIORITY.index(name))
    except ValueError:
        return (1, name.lower())


def product_storefront_sort_key(product):
    article = product_article(product)
    is_hit = article in section_articles("hits")
    is_new = article in section_articles("new_products")
    is_recommended = article in section_articles("recommended")
    title = clean_product_title(localize(product.get("title"))).lower()

    return (
        0 if is_hit else 1,
        0 if is_new else 1,
        0 if is_recommended else 1,
        title,
    )


def grouped_categories(products):
    groups = {}

    for product in products:
        name = category_name(product)
        groups.setdefault(name, []).append(product)

    for name in groups:
        groups[name] = sorted(
            groups[name],
            key=product_storefront_sort_key,
        )

    return dict(
        sorted(
            groups.items(),
            key=lambda item: category_sort_key(item[0]),
        )
    )


CATEGORY_PAGE_SIZE = 12


def categories_keyboard(products, page: int = 0) -> InlineKeyboardMarkup:
    groups = list(grouped_categories(products).items())
    total = len(groups)
    page_count = max(1, (total + CATEGORY_PAGE_SIZE - 1) // CATEGORY_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))

    start = page * CATEGORY_PAGE_SIZE
    page_items = groups[start:start + CATEGORY_PAGE_SIZE]

    rows = []
    for index, (name, items) in enumerate(page_items, start=start + 1):
        rows.append([
            InlineKeyboardButton(
                text=f"{index}) {name[:40]} ({len(items)})",
                callback_data=f"catalog_category:{category_key(name)}",
            )
        ])

    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Попередня",
                callback_data=f"categories_page:{page - 1}",
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
                callback_data=f"categories_page:{page + 1}",
            )
        )

    rows.append(navigation)
    rows.append([
        InlineKeyboardButton(
            text="🔄 Оновити каталог",
            callback_data="catalog_refresh",
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



CATALOG_GRID_PAGE_SIZE = 3


def product_badge_prefix(product):
    article = product_article(product)
    labels = []
    if article and article in section_articles("hits"):
        labels.append("🔥")
    if article and article in section_articles("new_products"):
        labels.append("🆕")
    if article and article in section_articles("recommended"):
        labels.append("⭐")
    return "".join(labels)


def catalog_grid_keyboard(products, category_id: str, page: int):
    total = len(products)
    page_count = max(1, (total + CATALOG_GRID_PAGE_SIZE - 1) // CATALOG_GRID_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * CATALOG_GRID_PAGE_SIZE
    page_items = products[start:start + CATALOG_GRID_PAGE_SIZE]

    rows = []
    for offset, product in enumerate(page_items, start=1):
        key = product_key(product)
        product_cache[key] = product
        title = clean_product_title(localize(product.get("title")))
        price = price_number(product)
        badge = product_badge_prefix(product)
        rows.append([
            InlineKeyboardButton(
                text=f"{badge} {offset}. {title[:28]} · {price:g} грн",
                callback_data=f"catalog_product:{key}:{category_id}:{page}",
            )
        ])

    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="⬅️ Попередня",
            callback_data=f"catalog_grid_page:{category_id}:{page - 1}",
        ))
    navigation.append(InlineKeyboardButton(
        text=f"Сторінка {page + 1}/{page_count}",
        callback_data="catalog_noop",
    ))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton(
            text="Наступна ➡️",
            callback_data=f"catalog_grid_page:{category_id}:{page + 1}",
        ))

    rows.append(navigation)

    hit_count = sum(
        1 for product in products
        if product_article(product) in section_articles("hits")
    )
    if hit_count:
        rows.append([
            InlineKeyboardButton(
                text=f"🔥 Показати тільки хіти ({hit_count})",
                callback_data=f"catalog_hits:{category_id}:0",
            )
        ])

    rows.append([InlineKeyboardButton(
        text="⬅️ До категорій",
        callback_data="catalog_categories",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def catalog_grid_text(category_title: str, products, page: int):
    total = len(products)
    page_count = max(
        1,
        (total + CATALOG_GRID_PAGE_SIZE - 1) // CATALOG_GRID_PAGE_SIZE,
    )
    page = max(0, min(page, page_count - 1))

    return (
        f"🍬 <b>{category_title}</b> · "
        f"<b>{total}</b> товарів · "
        f"сторінка <b>{page + 1}/{page_count}</b>"
    )


async def clear_catalog_album(chat_id: int, user_id: int):
    for message_id in user_catalog_album_messages.pop(user_id, []):
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass


async def send_catalog_grid(
    message,
    category_title: str,
    products,
    category_id: str,
    page: int,
    user_id: int,
):
    total = len(products)
    page_count = max(
        1,
        (total + CATALOG_GRID_PAGE_SIZE - 1) // CATALOG_GRID_PAGE_SIZE,
    )
    page = max(0, min(page, page_count - 1))
    start = page * CATALOG_GRID_PAGE_SIZE
    page_items = products[start:start + CATALOG_GRID_PAGE_SIZE]

    await clear_catalog_album(message.chat.id, user_id)
    sent_ids = []

    for product in page_items:
        key = product_key(product)
        product_cache[key] = product

        title = clean_product_title(localize(product.get("title")))
        price = price_number(product)
        article = product_article(product)
        image_url = get_image_url(product)

        caption = []

        if article and article in section_articles("hits"):
            caption.append("🔥 <b>ХІТ ПРОДАЖУ</b>")

        if article and article in section_articles("new_products"):
            caption.append("🆕 <b>НОВИНКА</b>")

        if article and article in section_articles("recommended"):
            caption.append("⭐ <b>РЕКОМЕНДОВАНО</b>")

        caption.extend([
            f"🍬 <b>{html.escape(title)}</b>",
            f"💰 <b>{price:g} грн</b>",
        ])

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="👆 Детальніше",
                    callback_data=f"catalog_product:{key}:{category_id}:{page}",
                )
            ]]
        )

        if image_url:
            sent = await message.answer_photo(
                photo=image_url,
                caption="\n".join(caption),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            sent = await message.answer(
                "\n".join(caption),
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        sent_ids.append(sent.message_id)

    user_catalog_album_messages[user_id] = sent_ids

    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"catalog_grid_page:{category_id}:{page - 1}",
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
                callback_data=f"catalog_grid_page:{category_id}:{page + 1}",
            )
        )

    await message.answer(
        f"🍬 <b>{category_title}</b> · <b>{total}</b> товарів",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                navigation,
                [InlineKeyboardButton(
                    text="⬅️ До категорій",
                    callback_data="catalog_categories",
                )],
            ]
        ),
    )


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
                text="⬅️ Попередня",
                callback_data=f"catalog_page:{category_id}:{page - 1}",
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
        f"📄 Сторінка: <b>{page + 1} із {page_count}</b>\n"
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


def clean_product_title(value):
    title = str(value or "").strip()

    prefixes = (
        "copy_", "copy-", "copy ",
        "копія_", "копия_", "копія ", "копия ",
        "cory_", "сору_",
    )

    lowered = title.lower()
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if lowered.startswith(prefix):
                title = title[len(prefix):].lstrip(" _-")
                lowered = title.lower()
                changed = True
                break

    title = re.sub(r"\s+", " ", title).strip()
    return title or "Товар"


def clean_product_description(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(value.split())


def product_weight(product):
    for key in ("weight", "packing", "packaging", "unit", "measure"):
        value = localize(product.get(key)).strip()
        if value:
            return value
    return ""


def product_text(product):
    title = clean_product_title(localize(product.get("title")))
    price = price_number(product)
    article = product_article(product)
    weight = product_weight(product)

    description = localize(
        product.get("short_description")
        or product.get("description_short")
        or product.get("description")
    )
    description = clean_product_description(description)

    badges = []

    if article and article in section_articles("hits"):
        badges.append("🔥 <b>ХІТ ПРОДАЖУ</b> 🔥")

    if article and article in section_articles("new_products"):
        badges.append("🆕 <b>НОВИНКА</b>")

    if article and article in section_articles("recommended"):
        badges.append("⭐ <b>РЕКОМЕНДОВАНО</b>")

    lines = []

    if badges:
        lines.extend(badges)
        lines.append("")

    lines.extend([
        f"🍬 <b>{html.escape(title)}</b>",
        "",
        f"💰 Ціна: <b>{price:g} грн</b>",
    ])

    if weight:
        lines.append(f"⚖️ Фасування: <b>{html.escape(weight)}</b>")

    lines.append("✅ В наявності")

    if description:
        short_description = description[:320].rstrip()
        if len(description) > 320:
            short_description += "…"
        lines.extend(["", f"📝 {html.escape(short_description)}"])

    return "\n".join(lines)


def product_keyboard(product, user_id=None):
    return card_keyboard(product, user_id)


def cart_view(user_id):
    cart = user_carts[user_id]

    if not cart:
        text = (
            "🛒 <b>Кошик порожній</b>\n\n"
            "Додайте товари з каталогу."
        )
    else:
        lines = ["🛒 <b>Ваш кошик</b>", ""]
        total = 0.0
        number = 1

        for key, qty in cart.items():
            product = product_cache.get(key)
            if not product:
                continue

            price = price_number(product)
            subtotal = price * qty
            total += subtotal
            title = clean_product_title(localize(product.get("title")))

            lines.append(
                f"{number}. <b>{title}</b>\n"
                f"   {qty} × {price:g} грн = <b>{subtotal:g} грн</b>"
            )
            number += 1

        lines.extend(["", f"💰 Разом: <b>{total:g} грн</b>"])
        text = "\n".join(lines)

    rows = []

    for key, qty in cart.items():
        product = product_cache.get(key)
        if not product:
            continue

        title = clean_product_title(localize(product.get("title")))
        rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"cart_minus:{key}"),
            InlineKeyboardButton(
                text=f"{title[:22]} × {qty}",
                callback_data="catalog_noop",
            ),
            InlineKeyboardButton(text="➕", callback_data=f"cart_plus:{key}"),
        ])
        rows.append([
            InlineKeyboardButton(
                text="🗑 Видалити позицію",
                callback_data=f"cart_remove:{key}",
            )
        ])

    if cart:
        rows.append([
            InlineKeyboardButton(
                text="🧹 Очистити кошик",
                callback_data="clear_cart",
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text="✅ Оформити замовлення",
                callback_data="checkout_start",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="🍬 Продовжити покупки",
            callback_data="catalog_categories",
        )
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
    track_event("cart_add", callback.from_user.id, product=product)

    quick_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🛍 Перейти до кошика",
            callback_data="open_cart",
        )],
        [InlineKeyboardButton(
            text="🍬 Продовжити покупки",
            callback_data="catalog_categories",
        )],
    ])

    await callback.message.answer(
        "✅ <b>Товар додано до кошика</b>",
        parse_mode="HTML",
        reply_markup=quick_keyboard,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("favorite_add:"))
async def favorite_add(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    product = product_cache.get(key)

    if not product:
        products = await get_in_stock_products()
        product = next(
            (item for item in products if product_key(item) == key),
            None,
        )

    if not product:
        await callback.answer("Товар не знайдено.", show_alert=True)
        return

    user_favorites[callback.from_user.id].add(key)
    track_event("favorite_add", callback.from_user.id, product=product)
    await callback.answer("Додано в обране ❤️", show_alert=True)


@dp.message(Command("favorites"))
async def favorites_command(message: Message):
    keys = user_favorites.get(message.from_user.id, set())

    if not keys:
        await message.answer(
            "❤️ <b>Обране порожнє</b>\n\n"
            "Додавайте товари кнопкою «В обране».",
            parse_mode="HTML",
        )
        return

    rows = []
    lines = ["❤️ <b>Обрані товари</b>", ""]

    for index, key in enumerate(list(keys)[:20], start=1):
        product = product_cache.get(key)
        if not product:
            continue

        title = clean_product_title(localize(product.get("title")))
        price = price_number(product)
        lines.append(f"{index}. {title} — <b>{price:g} грн</b>")
        rows.append([
            InlineKeyboardButton(
                text=f"🛒 {title[:32]}",
                callback_data=f"add:{key}",
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text="🗑 Видалити з обраного",
                callback_data=f"favorite_remove:{key}",
            )
        ])

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("favorite_remove:"))
async def favorite_remove(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    user_favorites[callback.from_user.id].discard(key)
    await callback.answer("Видалено з обраного")


@dp.callback_query(F.data == "open_cart")
async def open_cart(callback: CallbackQuery):
    text_value, keyboard = cart_view(callback.from_user.id)
    await callback.message.answer(
        text_value,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("cart_plus:"))
async def cart_plus(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    cart = user_carts[callback.from_user.id]
    cart[key] = cart.get(key, 0) + 1

    text_value, keyboard = cart_view(callback.from_user.id)
    await callback.message.edit_text(
        text_value,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("cart_minus:"))
async def cart_minus(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    cart = user_carts[callback.from_user.id]

    if key in cart:
        cart[key] -= 1
        if cart[key] <= 0:
            cart.pop(key, None)

    text_value, keyboard = cart_view(callback.from_user.id)
    await callback.message.edit_text(
        text_value,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("cart_remove:"))
async def cart_remove(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    user_carts[callback.from_user.id].pop(key, None)

    text_value, keyboard = cart_view(callback.from_user.id)
    await callback.message.edit_text(
        text_value,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await callback.answer("Позицію видалено")


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
    track_event("checkout_start", callback.from_user.id)
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

        badges = []

        if product:
            title = localize(product.get("title"))
            price = price_number(product)
            image_url = get_image_url(product)
            final_link = product_link(product)
            price_line = f"💰 Ціна: <b>{price:g} грн</b>\n\n"

            article = product_article(product)

            if article and article in section_articles("hits"):
                badges.append("🔥 <b>ХІТ ПРОДАЖУ</b> 🔥")

            if article and article in section_articles("new_products"):
                badges.append("🆕 <b>НОВИНКА</b>")

            if article and article in section_articles("recommended"):
                badges.append("⭐ <b>РЕКОМЕНДОВАНО</b>")
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
                price_line = f"💰 Ціна: <b>{formatted_price} грн</b>\n\n"
            else:
                price_line = "💰 Актуальна ціна вказана на сайті\n\n"

        badge_text = ""
        if badges:
            badge_text = "\n".join(badges) + "\n\n"

        post_text = (
            f"{badge_text}"
            f"🍬 <b>{title}</b>\n\n"
            f"{price_line}"
            f"🔗 Замовити:\n{final_link}"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="🛒 Купити",
                    url=final_link,
                )],
                [InlineKeyboardButton(
                    text="💬 Менеджер",
                    url=f"https://t.me/{MANAGER_USERNAME}",
                )],
                [InlineKeyboardButton(
                    text="🤖 Відкрити бота",
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

        track_event("channel_post", message.from_user.id, product=product)
        await message.answer("✅ Товар опубликован в канале.")
        await state.clear()

    except Exception as e:
        logging.exception("Manual product post error")
        await message.answer(f"❌ Ошибка публикации: {e}")


@dp.message(Command("analytics"))
async def analytics_handler(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Немає доступу.")
        return
    await message.answer(analytics_report(analytics_data,"Аналітика OKVEJ · за весь час"),parse_mode="HTML")

@dp.message(Command("analytics_today"))
async def analytics_today_handler(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Немає доступу.")
        return
    today=analytics_today_key(); daily=analytics_data.get("daily",{}).get(today,{"users":[],"events":{},"products":{},"searches":{}})
    await message.answer(analytics_report(daily,f"Аналітика OKVEJ · сьогодні ({today})"),parse_mode="HTML")

@dp.message(Command("analytics_reset"))
async def analytics_reset_handler(message: Message):
    global analytics_data
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Немає доступу.")
        return
    analytics_data=default_analytics_data(); save_analytics_data(); await message.answer("🧹 Аналітику очищено.")

@dp.message(Command("commands"))
async def commands_handler(message: Message):
    await message.answer(
        "⚡ <b>Швидкі команди OKVEJ</b>\n\n"
        "/start — відкрити головне меню\n"
        "/menu — оновити клавіатуру\n"
        "/version — перевірити версію бота\n"
        "/admin — відкрити адмін-панель\n"
        "/пост — опублікувати товар у каналі\n"
        "/myid — показати Telegram ID\n"
        "/analytics — аналітика за весь час\n"
        "/analytics_today — аналітика за сьогодні\n"
        "/analytics_reset — очистити аналітику\n"
        "/commands — список швидких команд",
        parse_mode="HTML",
        reply_markup=main_menu,
    )


@dp.message(Command("menu"))
async def menu_command(message: Message):
    await message.answer(
        "✅ Головне меню оновлено.",
        reply_markup=main_menu,
    )


@dp.message(Command("version"))
async def version_handler(message: Message):
    await message.answer(
        "🤖 <b>OKVEJ Bot</b>\n\n"
        f"Версія: <b>{BOT_VERSION}</b>\n"
        f"Збірка: <b>{BOT_BUILD}</b>\n\n"
        "Каталог працює через Horoshop API та показує "
        "лише товари зі статусом «В наявності».",
        parse_mode="HTML",
    )


@dp.message(CommandStart())
async def start(message: Message):
    track_event("start", message.from_user.id)
    track_event("subscription_offer", message.from_user.id)
    await message.answer(
        subscription_welcome_text(),
        parse_mode="HTML",
        reply_markup=subscription_keyboard(),
    )


@dp.callback_query(F.data == "subscription_check")
async def subscription_check(callback: CallbackQuery):
    track_event("subscription_check", callback.from_user.id)
    subscribed = await user_is_channel_member(callback.from_user.id)

    if subscribed is True:
        track_event("subscription_confirmed", callback.from_user.id)
        await callback.answer("✅ Підписку підтверджено!")
        await callback.message.answer(
            "✅ <b>Дякуємо за підписку!</b>\n\nКаталог OKVEJ відкрито 👇",
            parse_mode="HTML",
            reply_markup=main_menu,
        )
        return

    if subscribed is None:
        await callback.answer(
            "Не вдалося перевірити підписку. Можна продовжити без неї.",
            show_alert=True,
        )
        return

    await callback.answer(
        "Підписку поки не знайдено. Підпишіться на канал і повторіть перевірку.",
        show_alert=True,
    )


@dp.callback_query(F.data == "subscription_skip")
async def subscription_skip(callback: CallbackQuery):
    track_event("subscription_skip", callback.from_user.id)
    await callback.answer()
    await callback.message.answer(
        "🍬 <b>Головне меню OKVEJ</b>\n\n"
        "Ви можете користуватися каталогом без підписки.",
        parse_mode="HTML",
        reply_markup=main_menu,
    )


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
    track_event("catalog_open", message.from_user.id)
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
            reply_markup=categories_keyboard(products, 0),
        )
    except Exception as error:
        logging.exception("Catalog loading error")
        await loading.edit_text(
            "❌ Не вдалося відкрити каталог. "
            "Спробуйте ще раз трохи пізніше."
        )


@dp.callback_query(F.data.startswith("categories_page:"))
async def categories_page(callback: CallbackQuery):
    try:
        page = int(callback.data.split(":", 1)[1])
        products = await get_in_stock_products()
        groups = grouped_categories(products)

        await callback.message.edit_text(
            "🍬 <b>Каталог OKVEJ</b>\n\n"
            f"✅ У наявності: <b>{len(products)}</b> товарів\n"
            f"📂 Категорій: <b>{len(groups)}</b>\n\n"
            "Оберіть категорію:",
            parse_mode="HTML",
            reply_markup=categories_keyboard(products, page),
        )
        await callback.answer()
    except Exception:
        logging.exception("Categories page error")
        await callback.answer(
            "Не вдалося відкрити сторінку категорій.",
            show_alert=True,
        )


@dp.callback_query(F.data == "catalog_refresh")
async def catalog_refresh(callback: CallbackQuery):
    global catalog_products_cache, catalog_cache_until

    catalog_products_cache = []
    catalog_cache_until = 0.0

    try:
        products = await get_in_stock_products(force_refresh=True)
        groups = grouped_categories(products)

        await callback.message.edit_text(
            "🍬 <b>Каталог OKVEJ</b>\n\n"
            f"✅ У наявності: <b>{len(products)}</b> товарів\n"
            f"📂 Категорій: <b>{len(groups)}</b>\n\n"
            "Каталог оновлено. Оберіть категорію:",
            parse_mode="HTML",
            reply_markup=categories_keyboard(products, 0),
        )
        await callback.answer("Каталог оновлено")
    except Exception:
        logging.exception("Catalog refresh error")
        await callback.answer(
            "Не вдалося оновити каталог.",
            show_alert=True,
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
        reply_markup=categories_keyboard(products, 0),
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

    await send_catalog_grid(
        callback.message,
        title,
        category_products,
        category_id,
        0,
        callback.from_user.id,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("collapse_product:"))
async def collapse_product_card(callback: CallbackQuery):
    try:
        _, category_id, page_text = callback.data.split(":", 2)
        products = await get_in_stock_products()
        title, category_products = find_category(products, category_id)

        if not category_products:
            await callback.answer("Категорію не знайдено.", show_alert=True)
            return

        try:
            await callback.message.delete()
        except Exception:
            logging.exception("Could not delete product card")

        await send_catalog_grid(
            callback.message,
            title,
            category_products,
            category_id,
            int(page_text),
            callback.from_user.id,
        )
        await callback.answer()

    except Exception:
        logging.exception("Collapse product card error")
        await callback.answer(
            "Не вдалося згорнути картку.",
            show_alert=True,
        )


@dp.callback_query(F.data.startswith("catalog_grid_page:"))
async def catalog_grid_page(callback: CallbackQuery):
    try:
        _, category_id, page_text = callback.data.split(":", 2)
        products = await get_in_stock_products()
        title, category_products = find_category(products, category_id)

        if not category_products:
            await callback.answer("Категорію не знайдено.", show_alert=True)
            return

        await send_catalog_grid(
            callback.message,
            title,
            category_products,
            category_id,
            int(page_text),
            callback.from_user.id,
        )
        await callback.answer()

    except Exception:
        logging.exception("Catalog grid page error")
        await callback.answer("Не вдалося відкрити сторінку каталогу.", show_alert=True)




@dp.callback_query(F.data.startswith("catalog_hits:"))
async def catalog_hits(callback: CallbackQuery):
    try:
        _, category_id, page_text = callback.data.split(":", 2)
        products = await get_in_stock_products()
        title, category_products = find_category(products, category_id)

        hit_products = [
            product for product in category_products
            if product_article(product) in section_articles("hits")
        ]

        if not hit_products:
            await callback.answer("У цій категорії поки немає хітів.", show_alert=True)
            return

        await send_catalog_grid(
            callback.message,
            f"🔥 Хіти · {title}",
            hit_products,
            category_id,
            int(page_text),
            callback.from_user.id,
        )
        await callback.answer()

    except Exception:
        logging.exception("Catalog hits error")
        await callback.answer("Не вдалося відкрити хіти.", show_alert=True)


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

        track_event("product_view", callback.from_user.id, product=product)

        page = int(page_text)

        recent = user_recent[callback.from_user.id]
        if key in recent:
            recent.remove(key)
        recent.insert(0, key)
        del recent[10:]

        keyboard = card_keyboard(
            product,
            callback.from_user.id,
            InlineKeyboardButton(
                text="➖ Згорнути",
                callback_data=f"collapse_product:{category_id}:{page}",
            ),
        )

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


SEARCH_PAGE_SIZE = 8


CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g",
    "д": "d", "е": "e", "ё": "e", "є": "e", "ж": "zh",
    "з": "z", "и": "i", "і": "i", "ї": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


LAT_TO_CYR_REPLACEMENTS = (
    ("shch", "щ"),
    ("sch", "щ"),
    ("zh", "ж"),
    ("kh", "х"),
    ("ch", "ч"),
    ("sh", "ш"),
    ("yu", "ю"),
    ("ya", "я"),
    ("yo", "е"),
    ("ye", "е"),
)


LAT_TO_CYR_SINGLE = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е",
    "f": "ф", "g": "г", "h": "х", "i": "и", "j": "й",
    "k": "к", "l": "л", "m": "м", "n": "н", "o": "о",
    "p": "п", "q": "к", "r": "р", "s": "с", "t": "т",
    "u": "у", "v": "в", "w": "в", "x": "кс", "y": "и",
    "z": "з",
}


def normalize_search_text(value):
    value = str(value or "").lower()
    value = value.replace("ё", "е")
    value = re.sub(r"[^a-zа-яіїєґ0-9]+", " ", value)
    return " ".join(value.split())


def translit_cyr_to_lat(value):
    value = normalize_search_text(value)
    return "".join(CYR_TO_LAT.get(char, char) for char in value)


def translit_lat_to_cyr(value):
    value = normalize_search_text(value)
    result = value

    for source, target in LAT_TO_CYR_REPLACEMENTS:
        result = result.replace(source, target)

    converted = []
    for char in result:
        converted.append(LAT_TO_CYR_SINGLE.get(char, char))

    return "".join(converted)


def search_variants(value):
    normalized = normalize_search_text(value)
    variants = {
        normalized,
        translit_cyr_to_lat(normalized),
        translit_lat_to_cyr(normalized),
    }
    return {variant for variant in variants if variant}


def compact_search_text(value):
    return normalize_search_text(value).replace(" ", "")


def product_search_score(product, query):
    query_variants = search_variants(query)

    if not query_variants:
        return 0

    title = clean_product_title(localize(product.get("title")))
    article = normalize_search_text(product.get("article"))

    title_variants = search_variants(title)
    article_variants = search_variants(article)

    best_score = 0

    for query_variant in query_variants:
        query_compact = compact_search_text(query_variant)
        query_words = query_variant.split()

        if article and (
            query_variant == article
            or query_compact == compact_search_text(article)
        ):
            best_score = max(best_score, 2000)

        for title_variant in title_variants:
            title_compact = compact_search_text(title_variant)
            title_words = title_variant.split()

            if query_compact and query_compact == title_compact:
                best_score = max(best_score, 1800)
                continue

            if query_compact and query_compact in title_compact:
                position = title_compact.find(query_compact)
                length_penalty = max(0, len(title_compact) - len(query_compact))
                score = 1450 - position * 8 - min(length_penalty, 300)
                best_score = max(best_score, score)

            if query_variant == title_variant:
                best_score = max(best_score, 1700)
                continue

            if title_variant.startswith(query_variant):
                best_score = max(best_score, 1200)

            if len(query_words) >= 2:
                all_words_match = all(
                    any(
                        title_word == query_word
                        or title_word.startswith(query_word)
                        or query_word.startswith(title_word)
                        for title_word in title_words
                    )
                    for query_word in query_words
                )

                if all_words_match:
                    matched_length = sum(len(word) for word in query_words)
                    best_score = max(
                        best_score,
                        1000 + matched_length * 10,
                    )

            elif len(query_words) == 1:
                word = query_words[0]

                if len(word) < 3:
                    if any(title_word.startswith(word) for title_word in title_words):
                        best_score = max(best_score, 500)
                else:
                    if any(title_word == word for title_word in title_words):
                        best_score = max(best_score, 900)
                    elif any(title_word.startswith(word) for title_word in title_words):
                        best_score = max(best_score, 700)
                    elif word in title_variant:
                        best_score = max(best_score, 450)

    return best_score


def search_products_ranked(products, query):
    scored = []

    for product in products:
        score = product_search_score(product, query)

        if score >= 450:
            scored.append((score, product))

    scored.sort(
        key=lambda item: (
            -item[0],
            len(
                clean_product_title(
                    localize(item[1].get("title"))
                )
            ),
            clean_product_title(
                localize(item[1].get("title"))
            ).lower(),
        )
    )

    return [product for _, product in scored]


def search_results_keyboard(results, query, page):
    page_count = max(
        1,
        (len(results) + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE,
    )
    page = max(0, min(page, page_count - 1))

    start = page * SEARCH_PAGE_SIZE
    page_items = results[start:start + SEARCH_PAGE_SIZE]

    rows = []

    for product in page_items:
        key = product_key(product)
        product_cache[key] = product
        title = clean_product_title(localize(product.get("title")))
        price = price_number(product)

        rows.append([
            InlineKeyboardButton(
                text=f"{title[:39]} — {price:g} грн",
                callback_data=f"search_product:{key}:{page}",
            )
        ])

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Попередня",
                callback_data=f"search_page:{page - 1}",
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
                callback_data=f"search_page:{page + 1}",
            )
        )

    rows.append(navigation)
    rows.append([
        InlineKeyboardButton(
            text="🔍 Новий пошук",
            callback_data="search_again",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text == "🔍 Пошук товару")
async def search_start(message: Message, state: FSMContext):
    await state.set_state(SearchState.waiting_query)
    await message.answer(
        "🔍 <b>Пошук товару</b>\n\n"
        "Введіть назву, частину назви або артикул. Можна писати кирилицею або латиницею.",
        parse_mode="HTML",
    )


@dp.message(SearchState.waiting_query)
async def search_products(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    track_event("search", message.from_user.id, query=query)

    if len(query) < 2:
        await message.answer("Введіть щонайменше 2 символи.")
        return

    try:
        products = await get_in_stock_products()
        results = search_products_ranked(products, query)

        await state.update_data(
            search_query=query,
            search_result_keys=[
                product_key(product)
                for product in results
            ],
        )

        for product in results:
            product_cache[product_key(product)] = product

        if not results:
            await message.answer(
                "😔 Нічого не знайдено в наявності.\n\n"
                "Спробуйте коротшу назву або артикул."
            )
        else:
            shown_end = min(SEARCH_PAGE_SIZE, len(results))
            await message.answer(
                "🔍 <b>Результати пошуку</b>\n\n"
                f"Запит: <b>{html.escape(query)}</b>\n"
                f"Знайдено: <b>{len(results)}</b>\n"
                f"Показано: <b>1–{shown_end}</b>",
                parse_mode="HTML",
                reply_markup=search_results_keyboard(
                    results,
                    query,
                    0,
                ),
            )

    except Exception:
        logging.exception("Search error")
        await message.answer(
            "❌ Не вдалося виконати пошук. Спробуйте пізніше."
        )

    await state.clear()


@dp.callback_query(F.data.startswith("search_page:"))
async def search_page(callback: CallbackQuery):
    page = int(callback.data.split(":", 1)[1])

    # Результати відновлюються з кешу товарів.
    # Порядок відповідає останньому пошуку користувача.
    keys = [
        key for key, product in product_cache.items()
        if is_in_stock(product)
    ]
    results = [product_cache[key] for key in keys]

    if not results:
        await callback.answer(
            "Результати пошуку вже застаріли. Виконайте новий пошук.",
            show_alert=True,
        )
        return

    await callback.message.edit_reply_markup(
        reply_markup=search_results_keyboard(
            results,
            "",
            page,
        )
    )
    await callback.answer()


@dp.callback_query(F.data == "search_again")
async def search_again(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.waiting_query)
    await callback.message.answer(
        "🔍 Введіть новий пошуковий запит:"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("search_product:"))
async def search_product(callback: CallbackQuery):
    _, key, page_text = callback.data.split(":", 2)
    product = product_cache.get(key)

    if not product or not is_in_stock(product):
        await callback.answer(
            "Товар уже недоступний.",
            show_alert=True,
        )
        return

    recent = user_recent[callback.from_user.id]
    if key in recent:
        recent.remove(key)
    recent.insert(0, key)
    del recent[10:]

    await callback.message.answer_photo(
        photo=get_image_url(product),
        caption=product_text(product),
        parse_mode="HTML",
        reply_markup=product_keyboard(product, callback.from_user.id),
    )
    await callback.answer()


@dp.message(F.text == "❤️ Обране")
async def favorites_menu(message: Message):
    await favorites_command(message)


@dp.message(F.text == "🕒 Переглянуті")
async def recent_products(message: Message):
    keys = user_recent.get(message.from_user.id, [])

    if not keys:
        await message.answer(
            "🕒 <b>Переглянутих товарів ще немає</b>",
            parse_mode="HTML",
        )
        return

    rows = []
    lines = ["🕒 <b>Останні переглянуті</b>", ""]

    for index, key in enumerate(keys[:10], start=1):
        product = product_cache.get(key)
        if not product:
            continue

        title = clean_product_title(localize(product.get("title")))
        price = price_number(product)

        lines.append(f"{index}. {title} — <b>{price:g} грн</b>")
        rows.append([
            InlineKeyboardButton(
                text=f"🍬 {title[:35]}",
                callback_data=f"recent_product:{key}",
            )
        ])

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("recent_product:"))
async def recent_product(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    product = product_cache.get(key)

    if not product or not is_in_stock(product):
        await callback.answer(
            "Товар уже недоступний.",
            show_alert=True,
        )
        return

    image_url = get_image_url(product)

    if image_url:
        await callback.message.answer_photo(
            photo=image_url,
            caption=product_text(product),
            parse_mode="HTML",
            reply_markup=product_keyboard(product, callback.from_user.id),
        )
    else:
        await callback.message.answer(
            product_text(product),
            parse_mode="HTML",
            reply_markup=product_keyboard(product, callback.from_user.id),
        )

    await callback.answer()


async def show_manual_section(message: Message, section: str):
    products = await get_in_stock_products()
    items = products_from_section(products, section)
    title = "🆕 Новинки OKVEJ" if section == "new_products" else "🔥 Хіти OKVEJ"

    if not items:
        await message.answer(
            f"<b>{title}</b>\n\nТовари поки не вибрані.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"<b>{title}</b>\n\n"
        f"Вибрано товарів: <b>{len(items)}</b>",
        parse_mode="HTML",
        reply_markup=admin_section_keyboard(items, section, 0),
    )


@dp.message(F.text == "🆕 Новинки")
async def new_products_menu(message: Message):
    await show_manual_section(message, "new_products")


@dp.message(F.text == "🔥 Хіти")
async def hits_menu(message: Message):
    await show_manual_section(message, "hits")


@dp.callback_query(F.data.startswith("admin_section_page:"))
async def admin_section_page(callback: CallbackQuery):
    _, section, page_text = callback.data.split(":", 2)
    products = await get_in_stock_products()
    items = products_from_section(products, section)

    await callback.message.edit_reply_markup(
        reply_markup=admin_section_keyboard(items, section, int(page_text)),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_product:"))
async def admin_product_card(callback: CallbackQuery):
    _, section, key, page_text = callback.data.split(":", 3)
    product = product_cache.get(key)

    if not product:
        products = await get_in_stock_products()
        product = next(
            (item for item in products if product_key(item) == key),
            None,
        )

    if not product:
        await callback.answer("Товар не знайдено.", show_alert=True)
        return

    back_title = "новинок" if section == "new_products" else "хітів"
    keyboard = card_keyboard(
        product,
        callback.from_user.id,
        InlineKeyboardButton(
            text=f"⬅️ До {back_title}",
            callback_data=f"admin_section_page:{section}:{page_text}",
        ),
    )

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


@dp.callback_query(F.data.startswith("admin_section:"))
async def admin_section_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return

    _, section, action, key = callback.data.split(":", 3)
    if section not in {"new_products", "hits", "recommended"}:
        await callback.answer("Невідомий розділ.", show_alert=True)
        return

    product = product_cache.get(key)
    if not product:
        products = await get_in_stock_products()
        product = next(
            (item for item in products if product_key(item) == key),
            None,
        )

    if not product:
        await callback.answer("Товар не знайдено.", show_alert=True)
        return

    enabled = action == "add"
    if not set_product_section(product, section, enabled):
        await callback.answer("Не вдалося зберегти.", show_alert=True)
        return

    names = {
        "new_products": "новинки",
        "hits": "хіти",
        "recommended": "рекомендовані",
    }
    await callback.answer(
        f"Товар {'додано в' if enabled else 'прибрано з'} {names[section]}.",
        show_alert=True,
    )

    await callback.message.edit_reply_markup(
        reply_markup=card_keyboard(product, callback.from_user.id),
    )


@dp.message(Command("admin"))
async def admin_status(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас немає доступу до адмін-панелі.")
        return

    await message.answer(
        "⚙️ <b>Адмін-панель OKVEJ</b>\n\n"
        f"Новинок: <b>{len(admin_data['new_products'])}</b>\n"
        f"Хітів: <b>{len(admin_data['hits'])}</b>\n"
        f"Рекомендованих: <b>{len(admin_data['recommended'])}</b>\n\n"
        "Відкрийте картку товару, щоб додати або прибрати його.",
        parse_mode="HTML",
    )




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
    await message.answer(channel_public_url())



MINI_APP_HTML = r"""
<div id="app"></div>
<style>
:root{color-scheme:light;--blue:#0c98e7;--blue2:#0878cc;--blue3:#51c6f4;--ink:#102033;--muted:#728197;--line:#dbe8f2;--bg:#eff8ff;--card:#fff;--ok:#11a866;--shadow:0 12px 30px rgba(15,106,163,.12)}
*{box-sizing:border-box}html,body{margin:0;min-height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink);-webkit-tap-highlight-color:transparent}body{overflow-x:hidden}button,input,textarea{font:inherit}.app{min-height:100vh;padding-bottom:calc(88px + env(safe-area-inset-bottom))}.top{padding:10px 10px 0;position:sticky;top:0;z-index:15;background:rgba(239,248,255,.92);backdrop-filter:blur(16px)}
.hero{position:relative;overflow:hidden;min-height:188px;border-radius:0 0 30px 30px;background-image:linear-gradient(90deg,rgba(9,151,232,.75),rgba(0,116,205,.62)),url("data:image/jpeg;base64,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");background-size:cover;background-position:center 48%;color:white;padding:22px 18px 16px;box-shadow:0 14px 30px rgba(0,116,205,.22)}
.hero:after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(255,255,255,.03),rgba(0,72,147,.08));pointer-events:none}.brand{position:relative;z-index:1;display:flex;align-items:center;justify-content:space-between;gap:14px}.brand-main{display:flex;align-items:center;gap:13px}.logo{width:68px;height:68px;border-radius:22px;background:rgba(255,255,255,.23);display:grid;place-items:center;font-size:38px;border:1px solid rgba(255,255,255,.25);box-shadow:inset 0 1px 0 rgba(255,255,255,.25)}.brand h1{font-size:31px;line-height:1;margin:0 0 6px;font-weight:900;letter-spacing:.2px}.brand p{margin:0;font-size:15px;font-weight:650;text-shadow:0 1px 2px rgba(0,0,0,.1)}.count{padding:10px 12px;border-radius:999px;background:rgba(255,255,255,.23);font-weight:850;white-space:nowrap}
.search-row{position:relative;z-index:2;margin-top:18px;display:grid;grid-template-columns:1fr auto;gap:9px}.search{position:relative}.search input{width:100%;height:54px;border:0;border-radius:18px;padding:0 48px 0 17px;background:#fff;color:var(--ink);font-size:16px;outline:none;box-shadow:0 7px 18px rgba(0,80,140,.13)}.search input:focus{box-shadow:0 0 0 4px rgba(255,255,255,.35),0 7px 18px rgba(0,80,140,.13)}.search span{position:absolute;right:16px;top:13px;font-size:23px}.cat-button{width:56px;height:54px;border:0;border-radius:18px;background:#fff;color:var(--blue2);font-size:25px;box-shadow:0 7px 18px rgba(0,80,140,.13)}
.content{padding:14px 10px 0}.section{margin-top:15px}.section-head{display:flex;align-items:center;justify-content:space-between;margin:0 3px 10px;gap:10px}.section-head h2{margin:0;font-size:22px;line-height:1.15}.section-head span,.view-all{font-size:13px;color:var(--blue2);font-weight:750;border:0;background:transparent}.quick-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.quick{border:1px solid var(--line);background:rgba(255,255,255,.96);border-radius:21px;padding:15px;text-align:left;color:inherit;min-height:104px;box-shadow:0 6px 18px rgba(33,94,128,.06)}.quick .ico{font-size:28px}.quick b{display:block;font-size:17px;margin-top:8px}.quick small{display:block;color:var(--muted);margin-top:3px;font-size:13px}.quick.primary-tile{grid-column:1/-1;background:linear-gradient(135deg,var(--blue3),var(--blue));color:#fff;border:0}.quick.primary-tile small{color:rgba(255,255,255,.85)}
.site-card{display:flex;align-items:center;justify-content:space-between;gap:12px;width:100%;border:0;border-radius:22px;padding:17px 18px;background:linear-gradient(135deg,var(--blue),var(--blue2));color:white;text-align:left;box-shadow:0 12px 24px rgba(8,120,204,.22)}.site-card b{display:block;font-size:19px}.site-card small{display:block;margin-top:4px;opacity:.85}.site-card .arrow{font-size:32px}.delivery{margin-top:10px;border-radius:18px;padding:12px 15px;background:rgba(255,255,255,.82);border:1px solid #cfe9fb;color:var(--blue2);font-weight:750;text-align:center}
.category-strip{display:flex;gap:8px;overflow-x:auto;padding:2px 0 5px;scrollbar-width:none}.category-strip::-webkit-scrollbar{display:none}.chip{border:1px solid var(--line);background:#fff;color:var(--muted);padding:10px 14px;border-radius:999px;font-weight:800;white-space:nowrap}.chip.active{background:var(--blue);color:#fff;border-color:var(--blue)}.product-grid{display:grid;grid-template-columns:1fr;gap:15px}.product-card{background:#fff;border:1px solid var(--line);border-radius:25px;overflow:hidden;box-shadow:var(--shadow);position:relative}.photo{height:min(88vw,590px);min-height:390px;background:white;display:flex;align-items:center;justify-content:center;padding:8px;position:relative}.photo img{width:100%;height:100%;object-fit:contain}.heart{position:absolute;right:12px;top:12px;width:45px;height:45px;border:0;border-radius:50%;background:rgba(255,255,255,.94);font-size:23px;box-shadow:0 5px 16px rgba(0,0,0,.09)}.stock{position:absolute;left:14px;bottom:13px;padding:7px 10px;border-radius:99px;background:rgba(255,255,255,.94);color:var(--ok);font-size:12px;font-weight:850}.product-info{padding:15px}.tags{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:9px}.tag{padding:5px 9px;border-radius:99px;background:#eaf7ff;color:var(--blue2);font-size:12px;font-weight:850}.tag.hot{background:#fff1e8;color:#cf5e1a}.product-title{font-size:20px;line-height:1.28;font-weight:800;margin:0 0 7px}.meta{font-size:13px;color:var(--muted);margin-bottom:8px}.price{font-size:31px;font-weight:900}.actions{display:grid;grid-template-columns:52px 1fr;gap:9px;margin-top:12px}.details,.add{border:0;border-radius:15px;min-height:52px;font-weight:850}.details{background:#e8f7ff;color:var(--blue2);font-size:21px}.add{background:linear-gradient(135deg,var(--blue),var(--blue2));color:#fff;font-size:16px}.empty,.loader{background:#fff;border:1px solid var(--line);border-radius:21px;padding:42px 14px;text-align:center;color:var(--muted)}
.bottom{position:fixed;left:0;right:0;bottom:0;z-index:20;padding:8px 8px calc(8px + env(safe-area-inset-bottom));background:rgba(239,248,255,.94);backdrop-filter:blur(16px);border-top:1px solid var(--line)}.bottom-inner{max-width:850px;margin:auto;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:5px}.nav{border:0;border-radius:16px;background:#fff;color:var(--ink);font-size:11px;font-weight:800;padding:8px 2px}.nav.active{background:var(--blue);color:white}.nav i{display:block;font-style:normal;font-size:21px;margin-bottom:3px}.badge{display:inline-grid;place-items:center;min-width:18px;height:18px;border-radius:99px;background:white;color:var(--blue);font-size:10px;margin-left:2px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.47);display:flex;align-items:flex-end;z-index:40}.sheet{width:100%;max-height:96vh;overflow:auto;background:var(--bg);border-radius:28px 28px 0 0;padding:10px 14px calc(22px + env(safe-area-inset-bottom))}.grab{width:48px;height:5px;border-radius:99px;background:#c7d4dd;margin:2px auto 10px}.close{float:right;border:0;background:#fff;width:40px;height:40px;border-radius:50%;font-size:24px}.sheet-image{width:100%;height:min(82vw,620px);object-fit:contain;background:white;border-radius:22px}.sheet h2{font-size:24px;margin:14px 0 7px}.sheet-price{font-size:32px;font-weight:900}.description{color:var(--muted);line-height:1.55;white-space:pre-line}.sheet-actions{display:grid;grid-template-columns:56px 1fr;gap:10px;margin-top:15px}.primary{border:0;border-radius:15px;background:linear-gradient(135deg,var(--blue),var(--blue2));color:#fff;font-weight:850;padding:14px}.secondary{border:0;border-radius:15px;background:#fff;font-size:23px}.cat-list{display:grid;gap:8px;margin-top:12px}.cat-item{border:1px solid var(--line);background:#fff;color:inherit;border-radius:15px;padding:13px 14px;display:flex;justify-content:space-between;font-weight:750}.cat-item.active{border-color:var(--blue);background:#eaf7ff;color:var(--blue2)}.cart-item{display:grid;grid-template-columns:72px 1fr auto;gap:10px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:16px;padding:9px;margin:9px 0}.cart-item img{width:72px;height:72px;object-fit:contain;border-radius:12px}.qty{display:flex;align-items:center;gap:7px}.qty button{border:0;background:#e8f7ff;color:var(--blue2);width:31px;height:31px;border-radius:10px;font-weight:850}.summary{display:flex;justify-content:space-between;font-size:22px;font-weight:900;margin:17px 2px}.checkout{display:grid;gap:8px}.checkout input,.checkout textarea{width:100%;border:1px solid var(--line);background:#fff;border-radius:13px;padding:12px}.toast{position:fixed;left:50%;bottom:102px;transform:translateX(-50%);background:#102033;color:#fff;padding:10px 14px;border-radius:999px;z-index:60;font-weight:700;font-size:13px}
@media(min-width:760px){.top,.content{padding-left:18px;padding-right:18px}.quick-grid{grid-template-columns:repeat(4,minmax(0,1fr))}.quick.primary-tile{grid-column:auto}.product-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.photo{height:470px}.sheet{max-width:760px;margin:0 auto}.hero{min-height:205px}.brand h1{font-size:38px}}
@media(min-width:1180px){.product-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.photo{height:430px}.content{max-width:1500px;margin:auto}.top{padding-left:24px;padding-right:24px}.hero{max-width:1500px;margin:auto}}
</style>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();try{tg?.requestFullscreen?.()}catch(e){}
const app=document.querySelector('#app');
const state={products:[],mode:'home',category:'Усі',query:'',cart:{},favorites:new Set(),recent:[]};
const esc=s=>String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const money=n=>`${Number(n||0).toLocaleString('uk-UA')} грн`;
const countCart=()=>Object.values(state.cart).reduce((s,x)=>s+x.qty,0);const total=()=>Object.values(state.cart).reduce((s,x)=>s+x.qty*x.product.price,0);
function cats(){const c={};state.products.forEach(p=>c[p.category||'Інші товари']=(c[p.category||'Інші товари']||0)+1);return c}
function iconFor(x){const s=(x||'').toLowerCase();if(s.includes('подар'))return'🎁';if(s.includes('печ'))return'🍪';if(s.includes('шокол'))return'🍫';if(s.includes('карам'))return'🍭';if(s.includes('ваф'))return'🧇';if(s.includes('батонч'))return'🍫';if(s.includes('цук'))return'🍬';return'📦'}
function isGift(p){const s=`${p.category||''} ${p.title||''}`.toLowerCase();return s.includes('подарун')||s.includes('набір')||s.includes('набор')}
function filtered(){let a=[...state.products];if(state.mode==='favorites')a=a.filter(p=>state.favorites.has(p.id));if(state.category!=='Усі')a=a.filter(p=>(p.category||'Інші товари')===state.category);const q=state.query.trim().toLowerCase();if(q)a=a.filter(p=>`${p.title} ${p.article||''} ${p.category||''}`.toLowerCase().includes(q));return a}
function card(p){return `<article class="product-card" data-card="${p.id}"><div class="photo"><img src="${esc(p.image||'')}" alt="${esc(p.title)}"><button class="heart" data-fav="${p.id}">${state.favorites.has(p.id)?'❤️':'🤍'}</button><span class="stock">● В наявності</span></div><div class="product-info"><div class="tags"><span class="tag hot">🔥 Вигідно</span><span class="tag">OKVEJ</span></div><h3 class="product-title">${esc(p.title)}</h3><div class="meta">${esc(p.category||'Товар')}</div><div class="price">${money(p.price)}</div><div class="actions"><button class="details" data-details="${p.id}">ℹ️</button><button class="add" data-add="${p.id}">🛒 У кошик</button></div></div></article>`}
function nav(){return `<nav class="bottom"><div class="bottom-inner"><button class="nav ${state.mode==='home'?'active':''}" data-nav="home"><i>🏠</i>Головна</button><button class="nav ${state.mode==='catalog'?'active':''}" data-nav="catalog"><i>📂</i>Каталог</button><button class="nav" data-nav="search"><i>🔍</i>Пошук</button><button class="nav ${state.mode==='favorites'?'active':''}" data-nav="favorites"><i>❤️</i>Обране</button><button class="nav" data-nav="cart"><i>🛒</i>Кошик <span class="badge">${countCart()}</span></button></div></nav>`}
function shell(main){return `<main class="app"><header class="top"><div class="hero"><div class="brand"><div class="brand-main"><div class="logo">🍬</div><div><h1>OKVEJ</h1><p>Солодощі з доставкою по Україні</p></div></div><div class="count">${state.products.length} товарів</div></div><div class="search-row"><div class="search"><input id="search" value="${esc(state.query)}" placeholder="Пошук товару..."><span>🔍</span></div><button class="cat-button" id="filters">📂</button></div></div></header><div class="content">${main}</div>${nav()}</main>`}
function home(){const c=cats();const all=Object.keys(c).sort((a,b)=>c[b]-c[a]);const gifts=state.products.filter(isGift).sort((a,b)=>b.price-a.price).slice(0,3);return `<section class="section"><div class="quick-grid"><button class="quick primary-tile" data-home="catalog"><span class="ico">🛍️</span><b>Відкрити каталог</b><small>${state.products.length} товарів</small></button><button class="quick" data-home="favorites"><span class="ico">❤️</span><b>Обране</b><small>${state.favorites.size} товарів</small></button><button class="quick" data-home="recent"><span class="ico">🕓</span><b>Переглянуті</b><small>${state.recent.length} товарів</small></button><button class="quick" data-home="cart"><span class="ico">🛒</span><b>Кошик</b><small>${countCart()} товарів</small></button></div></section><section class="section"><div class="section-head"><h2>Популярні категорії</h2><button class="view-all" data-home="catalog">Дивитись усі ›</button></div><div class="quick-grid">${all.slice(0,8).map(x=>`<button class="quick" data-cat="${esc(x)}"><span class="ico">${iconFor(x)}</span><b>${esc(x)}</b><small>${c[x]} товарів</small></button>`).join('')}</div></section><section class="section"><button class="site-card" id="site"><span><b>🌐 Перейти на наш сайт</b><small>Більше товарів та акцій на okvej.com.ua</small></span><span class="arrow">›</span></button><div class="delivery">🚚 Безкоштовна доставка по Києву від 10 000 грн</div></section>${gifts.length?`<section class="section"><div class="section-head"><h2>🎁 Подарункові набори</h2><button class="view-all" data-gifts="1">Дивитись усі ›</button></div><div class="product-grid">${gifts.map(card).join('')}</div></section>`:''}`}
function catalog(){const list=filtered();const c=cats();const top=['Усі',...Object.keys(c).sort((a,b)=>c[b]-c[a]).slice(0,8)];return `<section class="section"><div class="category-strip">${top.map(x=>`<button class="chip ${state.category===x?'active':''}" data-chip="${esc(x)}">${esc(x)}</button>`).join('')}</div></section><section class="section"><div class="section-head"><h2>${state.mode==='favorites'?'Обране':state.category==='Усі'?'Каталог':esc(state.category)}</h2><span>${list.length} товарів</span></div><div class="product-grid">${list.map(card).join('')||'<div class="empty">Нічого не знайдено</div>'}</div></section>`}
function render(){app.innerHTML=shell(state.mode==='home'?home():catalog());bind()}
function bind(){document.querySelectorAll('[data-nav]').forEach(b=>b.onclick=()=>{const m=b.dataset.nav;if(m==='cart')return openCart();if(m==='search'){state.mode='catalog';render();setTimeout(()=>document.querySelector('#search')?.focus(),60);return}state.mode=m;state.category='Усі';render()});document.querySelectorAll('[data-home]').forEach(b=>b.onclick=()=>{const m=b.dataset.home;if(m==='cart')openCart();else if(m==='recent')openRecent();else{state.mode=m;state.category='Усі';render()}});document.querySelectorAll('[data-cat]').forEach(b=>b.onclick=()=>{state.category=b.dataset.cat;state.mode='catalog';render()});document.querySelectorAll('[data-chip]').forEach(b=>b.onclick=()=>{state.category=b.dataset.chip;render()});document.querySelector('[data-gifts]')?.addEventListener('click',()=>{const cat=Object.keys(cats()).find(x=>x.toLowerCase().includes('подарун'));state.category=cat||'Усі';state.mode='catalog';render()});document.querySelector('#site')?.addEventListener('click',()=>{if(tg?.openLink)tg.openLink('https://okvej.com.ua');else location.href='https://okvej.com.ua'});document.querySelectorAll('[data-card]').forEach(el=>el.onclick=e=>{if(e.target.closest('button'))return;details(el.dataset.card)});document.querySelectorAll('[data-details]').forEach(b=>b.onclick=()=>details(b.dataset.details));document.querySelectorAll('[data-add]').forEach(b=>b.onclick=()=>add(b.dataset.add));document.querySelectorAll('[data-fav]').forEach(b=>b.onclick=()=>toggleFav(b.dataset.fav));document.querySelector('#filters')?.addEventListener('click',openCategories);let timer;document.querySelector('#search')?.addEventListener('input',e=>{state.query=e.target.value.toLowerCase();clearTimeout(timer);timer=setTimeout(()=>{const caret=e.target.selectionStart||state.query.length;state.mode='catalog';render();requestAnimationFrame(()=>{const i=document.querySelector('#search');if(i){i.focus();i.setSelectionRange(caret,caret)}})},280)})}
function add(id){const p=state.products.find(x=>x.id===id);if(!p)return;state.cart[id]??={product:p,qty:0};state.cart[id].qty++;tg?.HapticFeedback?.impactOccurred('light');toast('Додано у кошик');render()}
function toggleFav(id){state.favorites.has(id)?state.favorites.delete(id):state.favorites.add(id);tg?.HapticFeedback?.selectionChanged();render()}
function remember(id){state.recent=[id,...state.recent.filter(x=>x!==id)].slice(0,20)}
function details(id){const p=state.products.find(x=>x.id===id);if(!p)return;remember(id);const m=document.createElement('div');m.className='modal';m.innerHTML=`<div class="sheet"><div class="grab"></div><button class="close">×</button><img class="sheet-image" src="${esc(p.image||'')}" alt="${esc(p.title)}"><div class="tags"><span class="tag">${esc(p.category||'Товар')}</span><span class="tag hot">✅ В наявності</span></div><h2>${esc(p.title)}</h2><div class="sheet-price">${money(p.price)}</div><p class="description">${esc(p.description||'Опис товару уточнюється.')}</p><div class="sheet-actions"><button class="secondary">${state.favorites.has(id)?'❤️':'🤍'}</button><button class="primary">🛒 Додати у кошик</button></div></div>`;document.body.appendChild(m);m.onclick=e=>{if(e.target===m)m.remove()};m.querySelector('.close').onclick=()=>m.remove();m.querySelector('.primary').onclick=()=>{add(id);m.remove()};m.querySelector('.secondary').onclick=()=>{toggleFav(id);m.remove();details(id)}}
function openCategories(){const c=cats(),arr=['Усі',...Object.keys(c).sort((a,b)=>c[b]-c[a])],m=document.createElement('div');m.className='modal';m.innerHTML=`<div class="sheet"><div class="grab"></div><button class="close">×</button><h2>📂 Категорії</h2><div class="cat-list">${arr.map(x=>`<button class="cat-item ${state.category===x?'active':''}" data-x="${esc(x)}"><span>${iconFor(x)} ${esc(x)}</span><b>${x==='Усі'?state.products.length:c[x]}</b></button>`).join('')}</div></div>`;document.body.appendChild(m);m.onclick=e=>{if(e.target===m)m.remove()};m.querySelector('.close').onclick=()=>m.remove();m.querySelectorAll('[data-x]').forEach(b=>b.onclick=()=>{state.category=b.dataset.x;state.mode='catalog';m.remove();render()})}
function openRecent(){const items=state.recent.map(id=>state.products.find(p=>p.id===id)).filter(Boolean),m=document.createElement('div');m.className='modal';m.innerHTML=`<div class="sheet"><div class="grab"></div><button class="close">×</button><h2>🕓 Переглянуті</h2><div class="product-grid">${items.map(card).join('')||'<div class="empty">Поки порожньо</div>'}</div></div>`;document.body.appendChild(m);m.onclick=e=>{if(e.target===m)m.remove()};m.querySelector('.close').onclick=()=>m.remove();m.querySelectorAll('[data-card]').forEach(el=>el.onclick=e=>{if(e.target.closest('button'))return;m.remove();details(el.dataset.card)});m.querySelectorAll('[data-add]').forEach(b=>b.onclick=()=>{add(b.dataset.add);m.remove()})}
function openCart(){const items=Object.values(state.cart),m=document.createElement('div');m.className='modal';m.innerHTML=`<div class="sheet"><div class="grab"></div><button class="close">×</button><h2>🛒 Кошик</h2>${items.length?items.map(x=>`<div class="cart-item"><img src="${esc(x.product.image||'')}" alt=""><div><b>${esc(x.product.title)}</b><div class="meta">${money(x.product.price)}</div></div><div class="qty"><button data-minus="${x.product.id}">−</button><b>${x.qty}</b><button data-plus="${x.product.id}">+</button></div></div>`).join('')+`<div class="summary"><span>Разом</span><span>${money(total())}</span></div><div class="checkout"><input id="name" placeholder="Ім’я"><input id="phone" type="tel" placeholder="Телефон"><input id="city" placeholder="Місто"><input id="branch" placeholder="Відділення Нової пошти"><textarea id="comment" placeholder="Коментар"></textarea><button id="order" class="primary">Оформити замовлення</button></div>`:'<div class="empty">Кошик порожній</div>'}</div>`;document.body.appendChild(m);m.onclick=e=>{if(e.target===m)m.remove()};m.querySelector('.close').onclick=()=>m.remove();m.querySelectorAll('[data-plus]').forEach(b=>b.onclick=()=>{state.cart[b.dataset.plus].qty++;m.remove();openCart();render()});m.querySelectorAll('[data-minus]').forEach(b=>b.onclick=()=>{const x=state.cart[b.dataset.minus];x.qty--;if(x.qty<=0)delete state.cart[b.dataset.minus];m.remove();openCart();render()});m.querySelector('#order')?.addEventListener('click',()=>submitOrder(m))}
async function submitOrder(m){const body={initData:tg?.initData||'',customer:{name:document.querySelector('#name').value.trim(),phone:document.querySelector('#phone').value.trim(),city:document.querySelector('#city').value.trim(),branch:document.querySelector('#branch').value.trim(),comment:document.querySelector('#comment').value.trim()},items:Object.values(state.cart).map(x=>({id:x.product.id,title:x.product.title,price:x.product.price,qty:x.qty,article:x.product.article}))};if(!body.customer.name||!body.customer.phone){tg?.showAlert?.('Вкажіть ім’я та телефон');return}const btn=document.querySelector('#order');btn.disabled=true;btn.textContent='Надсилаємо…';try{const r=await fetch('/api/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error();state.cart={};m.remove();render();tg?.showAlert?.('✅ Замовлення передано менеджеру!')}catch(e){btn.disabled=false;btn.textContent='Оформити замовлення';tg?.showAlert?.('Не вдалося оформити замовлення')}}
function toast(t){const x=document.createElement('div');x.className='toast';x.textContent=t;document.body.appendChild(x);setTimeout(()=>x.remove(),1300)}
app.innerHTML='<div class="loader">Завантаження каталогу…</div>';fetch('/api/products').then(r=>r.json()).then(d=>{state.products=d.products||[];render()}).catch(()=>app.innerHTML='<div class="empty">Не вдалося завантажити каталог</div>');
</script>
"""


def mini_product(product):
    description = clean_product_description(localize(product.get("short_description") or product.get("description") or ""))
    return {
        "id": product_key(product),
        "article": product_article(product),
        "title": clean_product_title(localize(product.get("title"))),
        "price": price_number(product),
        "image": get_image_url(product) or "",
        "category": category_name(product),
        "description": description[:700],
        "link": product_link(product),
    }


async def miniapp_page(request):
    return web.Response(text=MINI_APP_HTML, content_type="text/html", charset="utf-8")


async def miniapp_products(request):
    try:
        products = await get_in_stock_products()
        payload = [mini_product(p) for p in products]
        payload = [p for p in payload if p["title"] and p["price"] >= 0]
        return web.json_response({"products": payload}, dumps=lambda x: json.dumps(x, ensure_ascii=False))
    except Exception as exc:
        logging.exception("Mini App products error")
        return web.json_response({"error": str(exc)}, status=500)


async def miniapp_order(request):
    try:
        data = await request.json()
        customer = data.get("customer") or {}
        items = data.get("items") or []
        if not customer.get("name") or not customer.get("phone") or not items:
            return web.json_response({"ok": False, "error": "missing fields"}, status=400)
        lines = ["🛍 <b>Нове замовлення з Mini App</b>", "", f"👤 {html.escape(str(customer.get('name')))}", f"📞 {html.escape(str(customer.get('phone')))}"]
        if customer.get("city"): lines.append(f"🏙 {html.escape(str(customer.get('city')))}")
        if customer.get("branch"): lines.append(f"📦 НП: {html.escape(str(customer.get('branch')))}")
        lines += ["", "<b>Товари:</b>"]
        total = 0.0
        for item in items[:50]:
            qty = max(1, int(item.get("qty") or 1)); price = float(item.get("price") or 0); total += qty * price
            lines.append(f"• {html.escape(str(item.get('title') or 'Товар'))} × {qty} — {qty*price:.0f} грн")
        lines += ["", f"💰 <b>Разом: {total:.0f} грн</b>"]
        if customer.get("comment"): lines += ["", f"💬 {html.escape(str(customer.get('comment')))}"]
        target = MANAGER_CHAT_ID or ADMIN_USER_ID
        if not target:
            return web.json_response({"ok": False, "error": "manager not configured"}, status=503)
        await bot.send_message(target, "\n".join(lines), parse_mode="HTML")
        track_event("miniapp_order")
        return web.json_response({"ok": True})
    except Exception as exc:
        logging.exception("Mini App order error")
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def health(request):
    return web.json_response({"ok": True, "version": BOT_VERSION})


async def start_web_server():
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get("/", miniapp_page)
    app.router.add_get("/miniapp", miniapp_page)
    app.router.add_get("/api/products", miniapp_products)
    app.router.add_post("/api/order", miniapp_order)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Mini App web server started on port %s", PORT)
    return runner


@dp.message(F.text == "🛍 Відкрити магазин")
async def open_mini_app_fallback(message: Message):
    if not MINI_APP_URL:
        await message.answer("⚙️ Mini App майже готовий. Додайте змінну MINI_APP_URL у Railway.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛍 Відкрити магазин", web_app=WebAppInfo(url=MINI_APP_URL))
    ]])
    await message.answer("Відкрийте магазин OKVEJ 👇", reply_markup=keyboard)


# =============================================================
# УВЕДОМЛЕНИЯ О НОВЫХ ЗАКАЗАХ ХОРОШОП
# Получатели: ADMIN_USER_ID и MANAGER_CHAT_ID.
# ORDER_NOTIFY_CHAT_IDS можно использовать дополнительно, но он не обязателен.
# =============================================================

ORDER_NOTIFY_STATE_PATH = Path(os.getenv("ORDER_STATE_FILE", "/data/order_notifier_state.json"))
if not ORDER_NOTIFY_STATE_PATH.parent.exists():
    ORDER_NOTIFY_STATE_PATH = Path("order_notifier_state.json")

ORDER_POLL_SECONDS = max(20, int(os.getenv("ORDER_POLL_SECONDS", "45")))
HOROSHOP_ORDERS_ENDPOINT = os.getenv("HOROSHOP_ORDERS_ENDPOINT", "orders/export").strip("/")
HOROSHOP_ORDERS_ADMIN_URL = os.getenv(
    "HOROSHOP_ORDERS_ADMIN_URL", "https://okvej.com.ua/admin/orders/"
)


def _notify_chat_ids():
    values = []
    raw = os.getenv("ORDER_NOTIFY_CHAT_IDS", "")
    values.extend(item.strip() for item in raw.split(",") if item.strip())
    values.extend([ADMIN_USER_ID, MANAGER_CHAT_ID])
    result = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def _order_value(obj, *keys, default=""):
    if not isinstance(obj, dict):
        return default
    for key in keys:
        value = obj.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _order_text(value):
    if isinstance(value, dict):
        return str(
            value.get("ua") or value.get("uk") or value.get("ru")
            or value.get("en") or next(iter(value.values()), "")
        )
    return str(value or "")


def _order_money(value):
    if isinstance(value, dict):
        value = _order_value(value, "value", "amount", "price", default=0)
    try:
        number = float(str(value).replace(" ", "").replace(",", "."))
        return f"{number:,.2f}".replace(",", " ").replace(".00", "")
    except (TypeError, ValueError):
        return str(value or "0")


def _order_id(order):
    return str(_order_value(
        order, "id", "order_id", "orderId", "number", "order_number", default=""
    )).strip()


def _order_products(order):
    for key in ("products", "items", "order_products", "cart"):
        value = order.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _load_order_state():
    try:
        if ORDER_NOTIFY_STATE_PATH.exists():
            data = json.loads(ORDER_NOTIFY_STATE_PATH.read_text("utf-8"))
            return bool(data.get("initialized")), {
                str(x) for x in data.get("seen_order_ids", [])
            }
    except Exception:
        logging.exception("Cannot read order notification state")
    return False, set()


def _save_order_state(initialized, seen_ids):
    try:
        ORDER_NOTIFY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = ORDER_NOTIFY_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "initialized": initialized,
            "seen_order_ids": list(seen_ids)[-1000:],
        }, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(ORDER_NOTIFY_STATE_PATH)
    except Exception:
        logging.exception("Cannot save order notification state")


async def _horoshop_api_post(endpoint, payload):
    domain = os.getenv("HOROSHOP_DOMAIN", "okvej.com.ua")
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    url = f"https://{domain}/api/{endpoint.strip('/')}/"
    timeout = aiohttp.ClientTimeout(total=35)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url, json=payload, headers={"Content-Type": "application/json"}
        ) as response:
            body = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"Horoshop HTTP {response.status}: {body[:400]}")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Horoshop returned invalid JSON: {body[:400]}") from exc


async def _horoshop_recent_orders(limit=30):
    login = os.getenv("HOROSHOP_LOGIN")
    password = os.getenv("HOROSHOP_PASSWORD")
    if not login or not password:
        raise RuntimeError("HOROSHOP_LOGIN or HOROSHOP_PASSWORD is not set")

    auth = await _horoshop_api_post("auth", {"login": login, "password": password})
    if auth.get("status") != "OK":
        raise RuntimeError(f"Horoshop auth error: {auth}")
    token = str(auth.get("response", {}).get("token") or "")
    if not token:
        raise RuntimeError("Horoshop auth token is empty")

    data = await _horoshop_api_post(HOROSHOP_ORDERS_ENDPOINT, {
        "token": token,
        "offset": 0,
        "limit": limit,
    })
    if data.get("status") != "OK":
        raise RuntimeError(f"Horoshop orders error: {data}")

    response = data.get("response", {})
    if isinstance(response, list):
        return [x for x in response if isinstance(x, dict)]
    if isinstance(response, dict):
        for key in ("orders", "items", "data"):
            value = response.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _format_order_notification(order):
    order_number = _order_id(order) or "без номера"
    customer = _order_value(order, "customer", "user", "client", default={})
    recipient = _order_value(order, "recipient", "delivery_recipient", default={})

    name = (
        _order_text(_order_value(recipient, "name", "title", default=""))
        or _order_text(_order_value(customer, "name", "title", "full_name", default=""))
        or _order_text(_order_value(order, "name", "customer_name", default=""))
    )
    phone = (
        _order_text(_order_value(recipient, "phone", default=""))
        or _order_text(_order_value(customer, "phone", "telephone", default=""))
        or _order_text(_order_value(order, "phone", "telephone", default=""))
    )
    email = (
        _order_text(_order_value(customer, "email", default=""))
        or _order_text(_order_value(order, "email", default=""))
    )
    delivery = _order_text(_order_value(order, "delivery", "delivery_type", "shipping", default=""))
    payment = _order_text(_order_value(order, "payment", "payment_type", default=""))
    city = _order_text(_order_value(order, "city", "delivery_city", default=""))
    address = _order_text(_order_value(order, "address", "delivery_address", "warehouse", default=""))
    comment = _order_text(_order_value(order, "comment", "customer_comment", default=""))
    total = _order_value(order, "total", "total_sum", "amount", "sum", "price", default=0)

    lines = ["🔔 <b>НОВЕ ЗАМОВЛЕННЯ</b>", "", f"🧾 Номер: <b>#{html.escape(order_number)}</b>"]
    if name:
        lines.append(f"👤 Клієнт: <b>{html.escape(name)}</b>")
    if phone:
        lines.append(f"📞 Телефон: <code>{html.escape(phone)}</code>")
    if email:
        lines.append(f"✉️ Email: {html.escape(email)}")

    products = _order_products(order)
    if products:
        lines.extend(["", "🛒 <b>Товари:</b>"])
        for index, item in enumerate(products[:25], start=1):
            title = _order_text(_order_value(item, "title", "name", "product_title", default="Товар"))
            qty = _order_value(item, "quantity", "qty", "count", default=1)
            price = _order_value(item, "price", "cost", "amount", default="")
            row = f"{index}. {html.escape(title)} × {html.escape(str(qty))}"
            if price not in ("", None):
                row += f" — {_order_money(price)} грн"
            lines.append(row)
        if len(products) > 25:
            lines.append(f"…ще {len(products) - 25} позицій")

    lines.extend(["", f"💰 Сума: <b>{_order_money(total)} грн</b>"])
    if delivery:
        lines.append(f"🚚 Доставка: {html.escape(delivery)}")
    if city:
        lines.append(f"🏙 Місто: {html.escape(city)}")
    if address:
        lines.append(f"📍 Адреса/відділення: {html.escape(address)}")
    if payment:
        lines.append(f"💳 Оплата: {html.escape(payment)}")
    if comment:
        lines.extend(["", f"💬 Коментар: {html.escape(comment)}"])
    return "\n".join(lines)


async def _send_new_order(order, recipients):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📦 Відкрити замовлення", url=HOROSHOP_ORDERS_ADMIN_URL)
    ]])
    message = _format_order_notification(order)
    delivered = 0
    for chat_id in recipients:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            delivered += 1
        except Exception:
            logging.exception("Cannot send order notification to chat %s", chat_id)
    return delivered


async def order_notification_loop():
    recipients = _notify_chat_ids()
    if not recipients:
        logging.warning(
            "Order notifications disabled: ADMIN_USER_ID and MANAGER_CHAT_ID are empty"
        )
        return

    initialized, seen_ids = _load_order_state()
    logging.info(
        "Order notifier started: chats=%s interval=%ss",
        ",".join(recipients), ORDER_POLL_SECONDS,
    )

    while True:
        try:
            orders = await _horoshop_recent_orders()
            orders = sorted(orders, key=lambda item: _order_id(item))

            if not initialized:
                seen_ids.update(
                    order_id for order in orders if (order_id := _order_id(order))
                )
                initialized = True
                _save_order_state(initialized, seen_ids)
                logging.info("Order notifier initialized with %s existing orders", len(orders))
            else:
                changed = False
                for order in orders:
                    order_id = _order_id(order)
                    if not order_id or order_id in seen_ids:
                        continue
                    delivered = await _send_new_order(order, recipients)
                    if delivered:
                        seen_ids.add(order_id)
                        changed = True
                        logging.info("New Horoshop order sent: %s", order_id)
                if changed:
                    _save_order_state(initialized, seen_ids)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Order notifier check failed")

        await asyncio.sleep(ORDER_POLL_SECONDS)

async def main():
    logging.info("Starting OKVEJ bot v%s (%s)", BOT_VERSION, BOT_BUILD)
    await bot.delete_webhook(drop_pending_updates=True)
    runner = await start_web_server()
    order_notifier_task = asyncio.create_task(
        order_notification_loop(), name="okvej-order-notifier"
    )
    try:
        await dp.start_polling(bot)
    finally:
        order_notifier_task.cancel()
        await asyncio.gather(order_notifier_task, return_exceptions=True)
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
