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

BOT_VERSION = "17.3"
BOT_BUILD = "2026-07-18-candy-gift-recommendations"

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
        [
            KeyboardButton(text="🛍 Відкрити магазин", web_app=WebAppInfo(url=MINI_APP_URL)) if MINI_APP_URL else KeyboardButton(text="🛍 Відкрити магазин"),
        ],
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
            KeyboardButton(text="🌐 Сайт"),
        ],
        [KeyboardButton(text="📢 Канал OKVEJ")],
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
:root{color-scheme:light dark;--blue:#1597dc;--blue2:#0f7fc2;--line:#dce8ef;--muted:#6c7b86;--card:var(--tg-theme-secondary-bg-color,#fff);--bg:var(--tg-theme-bg-color,#f4f8fb);--text:var(--tg-theme-text-color,#16212b);--green:#159f6a}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);-webkit-tap-highlight-color:transparent;overflow-x:hidden}button,input,textarea{font:inherit}.shell{max-width:760px;margin:auto;padding:10px 10px 104px}.hero{background:linear-gradient(135deg,#54c3f1 0%,#1597dc 58%,#0f7fc2 100%);color:#fff;padding:16px;border-radius:20px;margin-bottom:10px;display:flex;align-items:center;justify-content:space-between;gap:12px}.brand{display:flex;align-items:center;gap:10px}.logo{width:46px;height:46px;border-radius:15px;background:rgba(255,255,255,.2);display:grid;place-items:center;font-size:26px}.hero h1{margin:0;font-size:22px}.hero p{margin:3px 0 0;font-size:13px;opacity:.9}.hero-stat{font-size:12px;background:rgba(255,255,255,.18);padding:7px 9px;border-radius:999px;white-space:nowrap}.toolbar{position:sticky;top:0;z-index:3;background:var(--bg);padding:2px 0 8px}.search-wrap{position:relative}.search{width:100%;padding:12px 42px 12px 14px;border:1px solid var(--line);border-radius:14px;background:var(--card);color:inherit;outline:none}.search:focus{border-color:var(--blue)}.search-icon{position:absolute;right:13px;top:10px;opacity:.55}.quick-row{display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:8px}.category-main,.reset{border:0;border-radius:13px;padding:11px 12px;font-weight:750}.category-main{background:#e8f5fc;color:#1679b6;text-align:left}.reset{background:var(--card);color:var(--muted)}.home-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0}.home-tile{border:0;background:var(--card);color:inherit;border-radius:17px;padding:17px 12px;text-align:left;border:1px solid rgba(20,90,120,.07)}.home-tile b{display:block;font-size:16px;margin-top:7px}.home-tile span{font-size:12px;color:var(--muted)}.section-head{display:flex;justify-content:space-between;align-items:center;margin:13px 2px 10px}.section-head h2{font-size:18px;margin:0}.count-label{font-size:13px;color:var(--muted)}.list{display:grid;grid-template-columns:1fr;gap:12px}.card{position:relative;background:var(--card);border-radius:19px;overflow:hidden;border:1px solid rgba(20,90,120,.08);display:block;width:100%;min-width:0}.photo-wrap{position:relative;background:#fff;width:100%;height:300px;min-height:0}.photo{width:100%;height:100%;object-fit:contain;display:block}.fav{position:absolute;right:8px;top:8px;border:0;width:36px;height:36px;border-radius:50%;background:rgba(255,255,255,.92);font-size:18px}.stock{position:absolute;left:8px;bottom:8px;background:rgba(255,255,255,.92);color:var(--green);border-radius:999px;padding:5px 8px;font-size:11px;font-weight:800}.body{padding:14px;display:flex;flex-direction:column;min-width:0;width:100%}.badges{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:7px}.sale-badge,.new-badge{font-size:11px;font-weight:800;border-radius:999px;padding:4px 7px}.sale-badge{background:#fff0e7;color:#c95a16}.new-badge{background:#e8f5fc;color:#1679b6}.title{font-size:16px;line-height:1.35;margin-bottom:7px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}.meta{font-size:12px;color:var(--muted);margin-bottom:8px}.price{font-size:24px;font-weight:850;margin-top:4px}.row{display:flex;gap:8px;margin-top:10px}.add,.details-btn{border:0;border-radius:12px;padding:11px 9px;font-weight:800}.add{flex:1;background:var(--blue);color:#fff}.details-btn{width:44px;background:#e8f5fc;color:#1679b6}.empty{text-align:center;padding:44px 10px;color:var(--muted)}.bottom{position:fixed;left:0;right:0;bottom:0;padding:8px 8px calc(8px + env(safe-area-inset-bottom));background:color-mix(in srgb,var(--bg) 94%,transparent);backdrop-filter:blur(14px);border-top:1px solid var(--line);z-index:4}.bottom-inner{width:min(740px,100%);margin:auto;display:grid;grid-template-columns:repeat(5,1fr);gap:5px}.navbtn{border:0;border-radius:12px;padding:9px 3px;background:var(--card);color:inherit;font-weight:700;font-size:11px;line-height:1.15}.navbtn.active{background:var(--blue);color:#fff}.nav-ico{display:block;font-size:18px;margin-bottom:3px}.badge{display:inline-grid;place-items:center;min-width:18px;height:18px;padding:0 4px;border-radius:999px;background:#fff;color:var(--blue);font-size:10px;margin-left:2px}.modal{position:fixed;inset:0;background:rgba(0,0,0,.48);display:flex;align-items:flex-end;z-index:10}.sheet{width:100%;max-height:94vh;overflow:auto;background:var(--bg);border-radius:24px 24px 0 0;padding:12px 16px calc(20px + env(safe-area-inset-bottom))}.grab{width:42px;height:5px;background:#c7d2d9;border-radius:999px;margin:0 auto 8px}.close{float:right;border:0;background:var(--card);border-radius:50%;width:36px;height:36px;font-size:22px}.sheet-img{width:100%;height:320px;object-fit:contain;background:#fff;border-radius:18px}.sheet h2{font-size:21px;line-height:1.25;margin:14px 0 7px}.sheet-price{font-size:25px;font-weight:850;margin:4px 0 10px}.description{line-height:1.5;color:var(--muted);white-space:pre-line}.submit{width:100%;border:0;border-radius:14px;padding:14px;background:var(--blue);color:#fff;font-weight:800}.detail-actions{display:grid;grid-template-columns:auto 1fr;gap:10px;margin-top:14px}.detail-fav{border:0;border-radius:14px;padding:14px;background:var(--card);font-size:20px}.category-list{display:grid;grid-template-columns:1fr;gap:8px;clear:both;padding-top:10px}.category-item{border:0;border-radius:14px;padding:13px;background:var(--card);color:inherit;display:flex;justify-content:space-between;text-align:left}.category-item.active{outline:2px solid var(--blue);background:#eaf7fe}.cart-item{display:grid;grid-template-columns:64px 1fr auto;gap:10px;align-items:center;border-bottom:1px solid var(--line);padding:10px 0}.cart-item img{width:64px;height:64px;object-fit:contain;background:#fff;border-radius:11px}.cart-title{font-size:13px;line-height:1.25}.muted{color:var(--muted);font-size:13px}.qty{display:flex;align-items:center;gap:8px}.qty button{border:0;border-radius:10px;width:34px;height:34px;background:#e5f4fd;color:#1679b6;font-size:20px}.summary{background:var(--card);border-radius:15px;padding:13px;margin:13px 0;display:flex;justify-content:space-between;font-weight:800}.checkout input,.checkout textarea{width:100%;margin:5px 0 8px;padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--card);color:inherit}.checkout textarea{min-height:70px;resize:vertical}.loader{text-align:center;padding:60px}.toast{position:fixed;left:50%;bottom:94px;transform:translateX(-50%);background:#17212b;color:#fff;padding:10px 14px;border-radius:999px;z-index:20;font-size:13px}
@media(max-width:420px){.shell{padding-left:8px;padding-right:8px}.card{display:block;width:100%;min-width:0}.photo-wrap{height:270px;min-height:0}.body{padding:12px}.title{font-size:15px}.price{font-size:22px}.row{display:grid;grid-template-columns:46px 1fr}.bottom-inner{gap:3px}.navbtn{font-size:10px;padding:8px 2px}}
</style>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const state={products:[],category:'Усі',cart:{},favorites:new Set(),recent:[],mode:'home',query:''};
const app=document.getElementById('app');
const money=n=>`${Math.round(Number(n)||0)} грн`;
const esc=s=>String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const total=()=>Object.values(state.cart).reduce((s,x)=>s+x.product.price*x.qty,0);
const cartCount=()=>Object.values(state.cart).reduce((s,x)=>s+x.qty,0);
function toast(text){const el=document.createElement('div');el.className='toast';el.textContent=text;document.body.appendChild(el);setTimeout(()=>el.remove(),1200)}
function counts(){const m={};state.products.forEach(p=>m[p.category]=(m[p.category]||0)+1);return m}
function visibleProducts(){return state.products.filter(p=>{const cat=state.category==='Усі'||p.category===state.category;const q=!state.query||p.title.toLowerCase().includes(state.query);const fav=state.mode!=='favorites'||state.favorites.has(p.id);return cat&&q&&fav})}
function hero(){return `<section class="hero"><div class="brand"><div class="logo">🍬</div><div><h1>OKVEJ</h1><p>Солодощі з доставкою по Україні</p></div></div><div class="hero-stat">${state.products.length} товарів</div></section>`}
function toolbar(){return `<div class="toolbar"><div class="search-wrap"><input id="search" class="search" value="${esc(state.query)}" placeholder="Пошук товару"><span class="search-icon">🔍</span></div><div class="quick-row"><button id="categoryOpen" class="category-main">📂 ${state.category==='Усі'?'Категорії':esc(state.category)} ▾</button>${state.category!=='Усі'?'<button id="reset" class="reset">Скинути</button>':''}</div></div>`}
function bottom(){return `<div class="bottom"><div class="bottom-inner">${nav('home','🏠','Головна')}${nav('catalog','📂','Каталог')}${nav('search','🔍','Пошук')}${nav('favorites','❤️','Обране')}${nav('cart','🛒',`Кошик <span class="badge">${cartCount()}</span>`)}</div></div>`}
function nav(mode,ico,label){return `<button class="navbtn ${state.mode===mode?'active':''}" data-nav="${mode}"><span class="nav-ico">${ico}</span>${label}</button>`}
function card(p){const fav=state.favorites.has(p.id);return `<article class="card" data-card="${p.id}"><div class="photo-wrap"><img class="photo" src="${esc(p.image||'')}" alt="${esc(p.title)}"><button class="fav" data-fav="${p.id}">${fav?'❤️':'🤍'}</button><span class="stock">● В наявності</span></div><div class="body"><div class="badges">${Number(p.price)<=500?'<span class="sale-badge">🔥 Вигідно</span>':''}<span class="new-badge">OKVEJ</span></div><div class="title">${esc(p.title)}</div><div class="meta">${esc(p.category||'Інші товари')}</div><div class="price">${money(p.price)}</div><div class="row"><button class="details-btn" data-details="${p.id}">ℹ️</button><button class="add" data-add="${p.id}">🛒 У кошик</button></div></div></article>`}
function render(){
 const products=visibleProducts();
 let body='';
 if(state.mode==='home') body=`${home()}`;
 else if(state.mode==='cart'){openCart();state.mode='catalog';return}
 else body=`${toolbar()}<div class="section-head"><h2>${state.mode==='favorites'?'❤️ Обране':state.category==='Усі'?'Каталог':esc(state.category)}</h2><div class="count-label">${products.length} товарів</div></div><div class="list">${products.map(card).join('')||'<div class="empty">Товарів не знайдено</div>'}</div>`;
 app.innerHTML=`<div class="shell">${hero()}${body}</div>${bottom()}`;
 bind();
}
function home(){const c=counts();const popular=Object.entries(c).sort((a,b)=>b[1]-a[1]).slice(0,4);const recommendedCandies=state.products.filter(p=>{const c=String(p.category||'').toLowerCase();return c.startsWith('цукерк')||c.startsWith('подарункові набори')}).sort((a,b)=>Number(b.price||0)-Number(a.price||0)).slice(0,6);return `<div class="home-grid"><button class="home-tile" data-home="catalog">📂<b>Каталог</b><span>Усі товари</span></button><button class="home-tile" data-home="favorites">❤️<b>Обране</b><span>${state.favorites.size} товарів</span></button><button class="home-tile" data-home="recent">🕓<b>Переглянуті</b><span>${state.recent.length} товарів</span></button><button class="home-tile" data-home="cart">🛒<b>Кошик</b><span>${cartCount()} товарів</span></button></div><div class="section-head"><h2>Популярні категорії</h2></div><div class="home-grid">${popular.map(([name,n])=>`<button class="home-tile" data-cat-home="${esc(name)}">🍬<b>${esc(name)}</b><span>${n} товарів</span></button>`).join('')}</div><div class="section-head"><h2>💎 Цукерки та подарункові набори</h2><div class="count-label">преміум</div></div><div class="list">${recommendedCandies.map(card).join('')||'<div class="empty">Цукерки та подарункові набори не знайдено</div>'}</div>`}
function bind(){
 document.querySelectorAll('[data-nav]').forEach(b=>b.onclick=()=>{const m=b.dataset.nav;if(m==='search'){state.mode='catalog';state.category='Усі';render();setTimeout(()=>document.querySelector('#search')?.focus(),50)}else if(m==='cart'){openCart()}else{state.mode=m;state.category='Усі';render()}});
 document.querySelectorAll('[data-home]').forEach(b=>b.onclick=()=>{const m=b.dataset.home;if(m==='cart')openCart();else if(m==='recent')openRecent();else{state.mode=m;render()}});
 document.querySelectorAll('[data-cat-home]').forEach(b=>b.onclick=()=>{state.category=b.dataset.catHome;state.mode='catalog';render()});
 document.querySelectorAll('[data-card]').forEach(el=>el.onclick=e=>{if(e.target.closest('button'))return;details(el.dataset.card)});
 document.querySelectorAll('[data-details]').forEach(b=>b.onclick=()=>details(b.dataset.details));
 document.querySelectorAll('[data-add]').forEach(b=>b.onclick=()=>add(b.dataset.add));
 document.querySelectorAll('[data-fav]').forEach(b=>b.onclick=()=>toggleFavorite(b.dataset.fav));
 document.querySelector('#search')?.addEventListener('input',e=>{state.query=e.target.value.trim().toLowerCase();render()});
 document.querySelector('#categoryOpen')?.addEventListener('click',openCategories);
 document.querySelector('#reset')?.addEventListener('click',()=>{state.category='Усі';render()});
}
function add(id){const p=state.products.find(x=>x.id===id);if(!p)return;state.cart[id]??={product:p,qty:0};state.cart[id].qty++;tg?.HapticFeedback?.impactOccurred('light');toast('Додано у кошик');render()}
function toggleFavorite(id){state.favorites.has(id)?state.favorites.delete(id):state.favorites.add(id);tg?.HapticFeedback?.selectionChanged();render()}
function remember(id){state.recent=[id,...state.recent.filter(x=>x!==id)].slice(0,20)}
function details(id){const p=state.products.find(x=>x.id===id);if(!p)return;remember(id);const modal=document.createElement('div');modal.className='modal';modal.innerHTML=`<div class="sheet"><div class="grab"></div><button class="close">×</button><img class="sheet-img" src="${esc(p.image||'')}" alt="${esc(p.title)}"><div class="badges"><span class="new-badge">${esc(p.category||'Товар')}</span><span class="sale-badge">✅ В наявності</span></div><h2>${esc(p.title)}</h2><div class="sheet-price">${money(p.price)}</div><p class="description">${esc(p.description||'Опис товару уточнюється.')}</p><div class="detail-actions"><button class="detail-fav">${state.favorites.has(id)?'❤️':'🤍'}</button><button class="submit">🛒 Додати у кошик</button></div></div>`;document.body.appendChild(modal);modal.onclick=e=>{if(e.target===modal)modal.remove()};modal.querySelector('.close').onclick=()=>modal.remove();modal.querySelector('.submit').onclick=()=>{add(id);modal.remove()};modal.querySelector('.detail-fav').onclick=()=>{toggleFavorite(id);modal.remove();details(id)}}
function openCategories(){const c=counts();const cats=['Усі',...Object.keys(c).sort((a,b)=>(c[b]||0)-(c[a]||0))];const modal=document.createElement('div');modal.className='modal';modal.innerHTML=`<div class="sheet"><div class="grab"></div><button class="close">×</button><h2>📂 Категорії</h2><div class="category-list">${cats.map(name=>`<button class="category-item ${state.category===name?'active':''}" data-cat="${esc(name)}"><span>${esc(name)}</span><b>${name==='Усі'?state.products.length:c[name]||0}</b></button>`).join('')}</div></div>`;document.body.appendChild(modal);modal.onclick=e=>{if(e.target===modal)modal.remove()};modal.querySelector('.close').onclick=()=>modal.remove();modal.querySelectorAll('[data-cat]').forEach(b=>b.onclick=()=>{state.category=b.dataset.cat;state.mode='catalog';modal.remove();render()})}
function openRecent(){const items=state.recent.map(id=>state.products.find(p=>p.id===id)).filter(Boolean);const modal=document.createElement('div');modal.className='modal';modal.innerHTML=`<div class="sheet"><div class="grab"></div><button class="close">×</button><h2>🕓 Переглянуті</h2><div class="list">${items.map(card).join('')||'<div class="empty">Ви ще не переглядали товари</div>'}</div></div>`;document.body.appendChild(modal);modal.onclick=e=>{if(e.target===modal)modal.remove()};modal.querySelector('.close').onclick=()=>modal.remove();modal.querySelectorAll('[data-card]').forEach(el=>el.onclick=e=>{if(e.target.closest('button'))return;modal.remove();details(el.dataset.card)});modal.querySelectorAll('[data-add]').forEach(b=>b.onclick=()=>{add(b.dataset.add);modal.remove()});modal.querySelectorAll('[data-fav]').forEach(b=>b.onclick=()=>{toggleFavorite(b.dataset.fav);modal.remove();openRecent()})}
function openCart(){const modal=document.createElement('div');modal.className='modal';const items=Object.values(state.cart);modal.innerHTML=`<div class="sheet"><div class="grab"></div><button class="close">×</button><h2>🛒 Кошик</h2>${items.length?items.map(x=>`<div class="cart-item"><img src="${esc(x.product.image||'')}" alt=""><div><div class="cart-title"><b>${esc(x.product.title)}</b></div><div class="muted">${money(x.product.price)}</div></div><div class="qty"><button data-minus="${x.product.id}">−</button><b>${x.qty}</b><button data-plus="${x.product.id}">+</button></div></div>`).join('')+`<div class="summary"><span>Разом</span><span>${money(total())}</span></div><div class="checkout"><input id="name" placeholder="Ім’я"><input id="phone" type="tel" placeholder="Телефон"><input id="city" placeholder="Місто"><input id="branch" placeholder="Відділення Нової пошти"><textarea id="comment" placeholder="Коментар до замовлення"></textarea><button id="order" class="submit">Оформити замовлення</button></div>`:'<div class="empty">Кошик поки порожній</div>'}</div>`;document.body.appendChild(modal);modal.onclick=e=>{if(e.target===modal)modal.remove()};modal.querySelector('.close').onclick=()=>modal.remove();modal.querySelectorAll('[data-plus]').forEach(b=>b.onclick=()=>{state.cart[b.dataset.plus].qty++;modal.remove();openCart();render()});modal.querySelectorAll('[data-minus]').forEach(b=>b.onclick=()=>{const x=state.cart[b.dataset.minus];x.qty--;if(x.qty<=0)delete state.cart[b.dataset.minus];modal.remove();openCart();render()});modal.querySelector('#order')?.addEventListener('click',()=>submitOrder(modal))}
async function submitOrder(modal){const body={initData:tg?.initData||'',customer:{name:document.querySelector('#name').value.trim(),phone:document.querySelector('#phone').value.trim(),city:document.querySelector('#city').value.trim(),branch:document.querySelector('#branch').value.trim(),comment:document.querySelector('#comment').value.trim()},items:Object.values(state.cart).map(x=>({id:x.product.id,title:x.product.title,price:x.product.price,qty:x.qty,article:x.product.article}))};if(!body.customer.name||!body.customer.phone){tg?.showAlert?.('Вкажіть ім’я та телефон');return}const button=document.querySelector('#order');button.disabled=true;button.textContent='Надсилаємо…';try{const r=await fetch('/api/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(r.ok){state.cart={};modal.remove();render();tg?.showAlert?.('✅ Замовлення передано менеджеру!')}else throw new Error()}catch(e){button.disabled=false;button.textContent='Оформити замовлення';tg?.showAlert?.('Не вдалося оформити замовлення')}}
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

async def main():
    logging.info("Starting OKVEJ bot v%s (%s)", BOT_VERSION, BOT_BUILD)
    await bot.delete_webhook(drop_pending_updates=True)
    runner = await start_web_server()
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
