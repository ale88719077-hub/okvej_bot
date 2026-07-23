import asyncio
import base64
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import aiohttp
from google.oauth2 import service_account
from googleapiclient.discovery import build


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, dict):
        for key in ("value", "amount", "price", "total", "sum"):
            if key in value:
                return _num(value[key])
        return 0.0
    try:
        return float(str(value).replace("грн", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("ua") or value.get("uk") or value.get("ru") or value.get("en") or "")
    return str(value or "")


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    for parser in (
        lambda: datetime.fromisoformat(text),
        lambda: datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S"),
        lambda: datetime.strptime(text[:10], "%Y-%m-%d"),
        lambda: datetime.strptime(text[:10], "%d.%m.%Y"),
    ):
        try:
            return parser()
        except ValueError:
            pass
    return None


def _service_account_info() -> dict:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    raw_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64", "").strip()

    if raw_b64:
        raw = base64.b64decode(raw_b64).decode("utf-8")

    if not raw:
        raise RuntimeError(
            "Не задан GOOGLE_SERVICE_ACCOUNT_JSON или GOOGLE_SERVICE_ACCOUNT_JSON_BASE64"
        )
    return json.loads(raw)


def _google_credentials(scopes: list[str]):
    return service_account.Credentials.from_service_account_info(
        _service_account_info(),
        scopes=scopes,
    )


class HoroshopOrdersClient:
    """Читает заказы через API Хорошоп.

    Endpoint можно изменить переменной HOROSHOP_ORDERS_ENDPOINT.
    По умолчанию используется orders/export.
    """

    def __init__(self):
        domain = os.getenv("HOROSHOP_DOMAIN", "okvej.com.ua")
        domain = domain.replace("https://", "").replace("http://", "").strip("/")
        self.base_url = f"https://{domain}/api"
        self.login = os.getenv("HOROSHOP_LOGIN")
        self.password = os.getenv("HOROSHOP_PASSWORD")
        self.endpoint = os.getenv("HOROSHOP_ORDERS_ENDPOINT", "orders/export").strip("/")
        self.token: str | None = None

    async def _post(self, endpoint: str, payload: dict) -> dict:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.base_url}/{endpoint.strip('/')}/",
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                body = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"Horoshop HTTP {response.status}: {body[:500]}")
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Horoshop вернул не JSON: {body[:500]}") from exc

    async def get_token(self) -> str:
        if self.token:
            return self.token
        if not self.login or not self.password:
            raise RuntimeError("Не заданы HOROSHOP_LOGIN/HOROSHOP_PASSWORD")

        data = await self._post("auth", {"login": self.login, "password": self.password})
        if data.get("status") != "OK":
            raise RuntimeError(f"Ошибка авторизации Хорошоп: {data}")
        self.token = data["response"]["token"]
        return self.token

    async def get_orders(self, start: date, end: date, limit: int = 1000) -> list[dict]:
        token = await self.get_token()
        payload = {
            "token": token,
            "offset": 0,
            "limit": limit,
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
        }
        data = await self._post(self.endpoint, payload)
        if data.get("status") != "OK":
            raise RuntimeError(
                f"Хорошоп не принял запрос заказов. "
                f"Проверь HOROSHOP_ORDERS_ENDPOINT. Ответ: {data}"
            )

        response = data.get("response", {})
        if isinstance(response, list):
            return response
        for key in ("orders", "items", "data"):
            if isinstance(response.get(key), list):
                return response[key]
        return []


def _order_total(order: dict) -> float:
    for key in ("total", "sum", "amount", "total_price", "order_sum", "cost"):
        if key in order:
            return _num(order[key])
    return 0.0


def _order_status(order: dict) -> str:
    for key in ("status", "status_title", "order_status"):
        if key in order:
            return _text(order[key]).lower()
    return ""


def _order_items(order: dict) -> list[dict]:
    for key in ("products", "items", "goods", "order_products"):
        value = order.get(key)
        if isinstance(value, list):
            return value
    return []


def _item_name(item: dict) -> str:
    return _text(item.get("title") or item.get("name") or item.get("product_title"))


def _item_qty(item: dict) -> float:
    return _num(item.get("quantity") or item.get("qty") or item.get("count") or 1)


def sales_report_text(orders: list[dict], title: str) -> str:
    excluded = ("отмен", "скас", "test", "тест")
    valid = [o for o in orders if not any(x in _order_status(o) for x in excluded)]

    revenue = sum(_order_total(o) for o in valid)
    avg = revenue / len(valid) if valid else 0

    status_counts = Counter(_order_status(o) or "без статуса" for o in valid)
    product_counts: Counter[str] = Counter()
    for order in valid:
        for item in _order_items(order):
            name = _item_name(item)
            if name:
                product_counts[name] += _item_qty(item)

    lines = [
        f"📊 <b>Продажи OKVEJ — {title}</b>",
        "",
        f"🛒 Заказов: <b>{len(valid)}</b>",
        f"💰 Выручка: <b>{revenue:,.0f} грн</b>".replace(",", " "),
        f"💵 Средний чек: <b>{avg:,.0f} грн</b>".replace(",", " "),
    ]

    if status_counts:
        lines += ["", "<b>Статусы:</b>"]
        for status, count in status_counts.most_common(5):
            lines.append(f"• {status}: {count}")

    if product_counts:
        lines += ["", "<b>ТОП товаров:</b>"]
        for index, (name, qty) in enumerate(product_counts.most_common(5), start=1):
            lines.append(f"{index}. {name} — {qty:g}")

    return "\n".join(lines)


def _search_console_service():
    credentials = _google_credentials(
        ["https://www.googleapis.com/auth/webmasters.readonly"]
    )
    return build("searchconsole", "v1", credentials=credentials, cache_discovery=False)


async def search_console_summary(start: date, end: date) -> dict:
    site_url = os.getenv("GSC_SITE_URL", "sc-domain:okvej.com.ua")
    service = _search_console_service()

    def execute():
        return service.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "dimensions": [],
                "rowLimit": 1,
                "dataState": "final",
            },
        ).execute()

    result = await asyncio.to_thread(execute)
    row = (result.get("rows") or [{}])[0]
    return {
        "clicks": float(row.get("clicks", 0)),
        "impressions": float(row.get("impressions", 0)),
        "ctr": float(row.get("ctr", 0)),
        "position": float(row.get("position", 0)),
    }


async def search_console_top(start: date, end: date, dimension: str, limit: int = 5) -> list[dict]:
    site_url = os.getenv("GSC_SITE_URL", "sc-domain:okvej.com.ua")
    service = _search_console_service()

    def execute():
        return service.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "dimensions": [dimension],
                "rowLimit": limit,
                "dataState": "final",
            },
        ).execute()

    result = await asyncio.to_thread(execute)
    return result.get("rows") or []


def _pct_delta(current: float, previous: float) -> str:
    if previous == 0:
        return "—"
    delta = (current - previous) / previous * 100
    return f"{delta:+.1f}%"


async def seo_report_text(days: int = 7) -> str:
    # Search Console часто имеет задержку данных, поэтому берём период до позавчера.
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    current, previous, queries, pages = await asyncio.gather(
        search_console_summary(start, end),
        search_console_summary(prev_start, prev_end),
        search_console_top(start, end, "query", 5),
        search_console_top(start, end, "page", 5),
    )

    lines = [
        f"📈 <b>SEO OKVEJ — {start:%d.%m}–{end:%d.%m}</b>",
        "",
        f"👀 Показы: <b>{current['impressions']:,.0f}</b> ({_pct_delta(current['impressions'], previous['impressions'])})".replace(",", " "),
        f"🖱 Клики: <b>{current['clicks']:,.0f}</b> ({_pct_delta(current['clicks'], previous['clicks'])})".replace(",", " "),
        f"📍 Средняя позиция: <b>{current['position']:.1f}</b>",
        f"🎯 CTR: <b>{current['ctr'] * 100:.2f}%</b>",
    ]

    if queries:
        lines += ["", "<b>ТОП запросов:</b>"]
        for row in queries:
            query = row.get("keys", [""])[0]
            lines.append(f"• {query} — {row.get('clicks', 0):.0f} кликов")

    if pages:
        lines += ["", "<b>ТОП страниц:</b>"]
        for row in pages:
            page = row.get("keys", [""])[0]
            short = page.replace("https://okvej.com.ua", "") or "/"
            lines.append(f"• {short[:70]} — {row.get('clicks', 0):.0f}")

    lines += [
        "",
        "ℹ️ Search Console API показывает клики, показы, CTR и позиции.",
        "Индексация и список 404 подключаются отдельно через URL Inspection/сканирование сайта.",
    ]
    return "\n".join(lines)


async def sales_period_text(period: str) -> str:
    today = date.today()
    if period == "today":
        start = end = today
        title = "сегодня"
    elif period == "week":
        start, end = today - timedelta(days=6), today
        title = "7 дней"
    elif period == "month":
        start, end = today.replace(day=1), today
        title = "месяц"
    else:
        start, end = today - timedelta(days=29), today
        title = "30 дней"

    orders = await HoroshopOrdersClient().get_orders(start, end)
    return sales_report_text(orders, title)
