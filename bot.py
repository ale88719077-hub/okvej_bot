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

BOT_VERSION = "13.2"
BOT_BUILD = "2026-07-15-product-badges-hits-new"

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

ADMIN_DATA_PATH = Path(
    os.getenv("ADMIN_DATA_PATH", "/data/admin_data.json")
)
if not ADMIN_DATA_PATH.parent.exists():
    ADMIN_DATA_PATH = Path("admin_data.json")


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
            text="🛒 Додати в кошик",
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


def grouped_categories(products):
    groups = {}
    for product in products:
        name = category_name(product)
        groups.setdefault(name, []).append(product)

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
    article = product.get("article") or "—"
    weight = product_weight(product)

    description = localize(
        product.get("description")
        or product.get("short_description")
        or product.get("description_short")
    )
    description = clean_product_description(description)

    badges = []
    article_key = str(article).strip()

    if article_key and article_key in section_articles("hits"):
        badges.append("🔥 <b>ХІТ ПРОДАЖУ</b> 🔥")

    if article_key and article_key in section_articles("new_products"):
        badges.append("🆕 <b>НОВИНКА</b>")

    if article_key and article_key in section_articles("recommended"):
        badges.append("⭐ <b>РЕКОМЕНДОВАНО</b>")

    lines = []

    if badges:
        lines.extend(badges)
        lines.append("")

    lines.extend([
        f"🍬 <b>{title}</b>",
        "",
        f"💰 Ціна: <b>{price:g} грн</b>",
    ])

    if weight:
        lines.append(f"⚖️ Фасування: <b>{weight}</b>")

    lines.extend([
        f"🏷 Артикул: <code>{article}</code>",
        "✅ В наявності",
    ])

    if description:
        lines.extend(["", f"📝 {description[:700]}"])

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

        recent = user_recent[callback.from_user.id]
        if key in recent:
            recent.remove(key)
        recent.insert(0, key)
        del recent[10:]

        keyboard = card_keyboard(
            product,
            callback.from_user.id,
            InlineKeyboardButton(
                text="⬅️ Назад до категорії",
                callback_data=f"catalog_page:{category_id}:{page}",
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
    await message.answer("https://t.me/okvej")


async def main():
    logging.info("Starting OKVEJ bot v%s (%s)", BOT_VERSION, BOT_BUILD)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
