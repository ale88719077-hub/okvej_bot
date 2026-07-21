import asyncio
import html
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("okvej.orders")

DEFAULT_POLL_SECONDS = 45
DEFAULT_STATE_FILE = "/data/order_notifier_state.json"


def _first(value: Any, *keys: str, default: Any = "") -> Any:
    if not isinstance(value, dict):
        return default
    for key in keys:
        current = value.get(key)
        if current not in (None, "", [], {}):
            return current
    return default


def _localized(value: Any) -> str:
    if isinstance(value, dict):
        return str(
            value.get("ua")
            or value.get("uk")
            or value.get("ru")
            or value.get("en")
            or next(iter(value.values()), "")
        )
    return str(value or "")


def _money(value: Any) -> str:
    if isinstance(value, dict):
        value = _first(value, "value", "amount", "price", default=0)
    try:
        number = float(str(value).replace(" ", "").replace(",", "."))
        return f"{number:,.2f}".replace(",", " ").replace(".00", "")
    except (TypeError, ValueError):
        return str(value or "0")


def _order_id(order: dict) -> str:
    return str(
        _first(
            order,
            "id",
            "order_id",
            "orderId",
            "number",
            "order_number",
            default="",
        )
    ).strip()


def _order_timestamp(order: dict) -> float:
    raw = _first(order, "created_at", "createdAt", "date", "created", default=0)
    if isinstance(raw, (int, float)):
        return float(raw)
    return 0.0


class HoroshopOrdersClient:
    def __init__(self) -> None:
        domain = os.getenv("HOROSHOP_DOMAIN", "okvej.com.ua")
        domain = domain.replace("https://", "").replace("http://", "").strip("/")
        self.base_url = f"https://{domain}/api"
        self.login = os.environ["HOROSHOP_LOGIN"]
        self.password = os.environ["HOROSHOP_PASSWORD"]
        self.token = ""
        self.token_until = 0.0
        self.timeout = aiohttp.ClientTimeout(total=35)

    async def _post(self, endpoint: str, payload: dict) -> dict:
        url = f"{self.base_url}/{endpoint.strip('/')}/"
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                body = await response.text()
                if response.status >= 400:
                    raise RuntimeError(
                        f"Horoshop HTTP {response.status} for {endpoint}: {body[:500]}"
                    )
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Horoshop returned invalid JSON for {endpoint}: {body[:500]}"
                    ) from exc

    async def get_token(self) -> str:
        if self.token and time.time() < self.token_until:
            return self.token

        data = await self._post(
            "auth",
            {"login": self.login, "password": self.password},
        )
        if data.get("status") != "OK":
            raise RuntimeError(f"Horoshop auth error: {data}")

        self.token = str(data["response"]["token"])
        self.token_until = time.time() + 540
        return self.token

    async def get_recent_orders(self, limit: int = 30) -> list[dict]:
        token = await self.get_token()

        # Основной метод Хорошоп API. Если в конкретном магазине используется
        # другая версия API, endpoint можно изменить переменной HOROSHOP_ORDERS_ENDPOINT.
        endpoint = os.getenv("HOROSHOP_ORDERS_ENDPOINT", "orders/export")
        payload = {
            "token": token,
            "offset": 0,
            "limit": limit,
        }

        data = await self._post(endpoint, payload)
        if data.get("status") != "OK":
            raise RuntimeError(f"Horoshop orders error: {data}")

        response = data.get("response", {})
        if isinstance(response, list):
            return [x for x in response if isinstance(x, dict)]

        for key in ("orders", "items", "data"):
            orders = response.get(key) if isinstance(response, dict) else None
            if isinstance(orders, list):
                return [x for x in orders if isinstance(x, dict)]

        return []


class OrderNotifier:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.client = HoroshopOrdersClient()
        # Приоритет: специальный список получателей. Если он не задан,
        # автоматически используем уже существующие переменные владельца
        # и менеджера из основного бота.
        configured_ids = os.getenv("ORDER_NOTIFY_CHAT_IDS", "").strip()
        fallback_ids = [
            os.getenv("ORDER_NOTIFY_CHAT_ID", "").strip(),
            os.getenv("ADMIN_USER_ID", "").strip(),
            os.getenv("MANAGER_CHAT_ID", "").strip(),
        ]

        raw_ids = []
        if configured_ids:
            raw_ids.extend(configured_ids.split(","))
        else:
            raw_ids.extend(fallback_ids)

        # Убираем пустые значения и повторы, сохраняя порядок.
        self.chat_ids = list(dict.fromkeys(
            chat_id.strip()
            for chat_id in raw_ids
            if chat_id and chat_id.strip() and chat_id.strip() != "0"
        ))

        self.enabled = bool(self.chat_ids)
        if not self.enabled:
            log.warning(
                "Order notifications disabled: set ORDER_NOTIFY_CHAT_IDS "
                "or ADMIN_USER_ID / MANAGER_CHAT_ID"
            )
        self.poll_seconds = max(
            20,
            int(os.getenv("ORDER_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))),
        )
        self.admin_url = os.getenv(
            "HOROSHOP_ORDERS_ADMIN_URL",
            "https://okvej.com.ua/admin/orders/",
        )
        self.state_path = Path(
            os.getenv("ORDER_STATE_FILE", DEFAULT_STATE_FILE)
        )
        self.seen_ids: set[str] = set()
        self.initialized = False
        self._load_state()

    def _load_state(self) -> None:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text("utf-8"))
                self.seen_ids = {str(x) for x in data.get("seen_order_ids", [])}
                self.initialized = bool(data.get("initialized", False))
        except Exception:
            log.exception("Cannot read order notifier state")

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            ids = list(self.seen_ids)[-1000:]
            temp = self.state_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(
                    {
                        "initialized": self.initialized,
                        "seen_order_ids": ids,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "utf-8",
            )
            temp.replace(self.state_path)
        except Exception:
            log.exception("Cannot save order notifier state")

    def _products(self, order: dict) -> list[dict]:
        for key in ("products", "items", "order_products", "cart"):
            value = order.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return []

    def _message(self, order: dict) -> str:
        order_id = _order_id(order) or "без номера"

        customer = _first(order, "customer", "user", "client", default={})
        recipient = _first(order, "recipient", "delivery_recipient", default={})

        name = (
            _localized(_first(recipient, "name", "title", default=""))
            or _localized(_first(customer, "name", "title", "full_name", default=""))
            or _localized(_first(order, "name", "customer_name", default=""))
        )
        phone = (
            _localized(_first(recipient, "phone", default=""))
            or _localized(_first(customer, "phone", "telephone", default=""))
            or _localized(_first(order, "phone", "telephone", default=""))
        )
        email = (
            _localized(_first(customer, "email", default=""))
            or _localized(_first(order, "email", default=""))
        )

        delivery = _localized(
            _first(order, "delivery", "delivery_type", "shipping", default="")
        )
        payment = _localized(
            _first(order, "payment", "payment_type", default="")
        )
        city = _localized(
            _first(order, "city", "delivery_city", default="")
        )
        address = _localized(
            _first(order, "address", "delivery_address", "warehouse", default="")
        )
        comment = _localized(_first(order, "comment", "customer_comment", default=""))

        total = _first(
            order,
            "total",
            "total_sum",
            "amount",
            "sum",
            "price",
            default=0,
        )

        lines = [
            "🔔 <b>НОВЕ ЗАМОВЛЕННЯ</b>",
            "",
            f"🧾 Номер: <b>#{html.escape(order_id)}</b>",
        ]

        if name:
            lines.append(f"👤 Клієнт: <b>{html.escape(name)}</b>")
        if phone:
            safe_phone = html.escape(phone)
            lines.append(f"📞 Телефон: <code>{safe_phone}</code>")
        if email:
            lines.append(f"✉️ Email: {html.escape(email)}")

        products = self._products(order)
        if products:
            lines.extend(["", "🛒 <b>Товари:</b>"])
            for index, item in enumerate(products[:25], start=1):
                title = _localized(
                    _first(item, "title", "name", "product_title", default="Товар")
                )
                qty = _first(item, "quantity", "qty", "count", default=1)
                price = _first(item, "price", "cost", "amount", default="")
                line = f"{index}. {html.escape(title)} × {html.escape(str(qty))}"
                if price not in ("", None):
                    line += f" — {_money(price)} грн"
                lines.append(line)
            if len(products) > 25:
                lines.append(f"…ще {len(products) - 25} позицій")

        lines.extend(["", f"💰 Сума: <b>{_money(total)} грн</b>"])

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

    async def send_order(self, order: dict) -> None:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📦 Відкрити замовлення",
                        url=self.admin_url,
                    )
                ]
            ]
        )
        message = self._message(order)
        errors = []

        for chat_id in self.chat_ids:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                errors.append((chat_id, exc))
                log.exception(
                    "Cannot send order notification to Telegram chat %s",
                    chat_id,
                )

        if len(errors) == len(self.chat_ids):
            raise RuntimeError(
                "Order notification was not delivered to any configured chat"
            )

    async def check_once(self) -> None:
        if not self.enabled:
            return
        orders = await self.client.get_recent_orders()
        orders = sorted(
            orders,
            key=lambda order: (_order_timestamp(order), _order_id(order)),
        )

        # При первом запуске запоминаем уже существующие заказы, чтобы бот
        # не прислал десятки старых уведомлений.
        if not self.initialized:
            self.seen_ids.update(
                order_id for order in orders
                if (order_id := _order_id(order))
            )
            self.initialized = True
            self._save_state()
            log.info("Order notifier initialized with %s existing orders", len(orders))
            return

        changed = False
        for order in orders:
            order_id = _order_id(order)
            if not order_id or order_id in self.seen_ids:
                continue

            await self.send_order(order)
            self.seen_ids.add(order_id)
            changed = True
            log.info("New Horoshop order sent to Telegram: %s", order_id)

        if changed:
            self._save_state()

    async def run_forever(self) -> None:
        if not self.enabled:
            log.warning("Order notifier task is idle because no recipients are configured")
            while True:
                await asyncio.sleep(3600)

        log.info(
            "Order notifier started: chats=%s interval=%ss",
            ",".join(self.chat_ids),
            self.poll_seconds,
        )
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Order notifier check failed")
            await asyncio.sleep(self.poll_seconds)


async def start_order_notifier(bot: Bot) -> asyncio.Task:
    """Запускает фоновую проверку заказов внутри основного процесса бота."""
    notifier = OrderNotifier(bot)
    return asyncio.create_task(
        notifier.run_forever(),
        name="okvej-order-notifier",
    )
