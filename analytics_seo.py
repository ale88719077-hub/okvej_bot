import asyncio
import base64
import html
import json
import logging
import os
from datetime import date, timedelta
from urllib.parse import urlsplit

from google.oauth2 import service_account
from googleapiclient.discovery import build


def google_config_diagnostics() -> dict:
    file_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    raw_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64", "").strip()
    site_url = os.getenv("GSC_SITE_URL", "").strip()

    if file_path:
        method = "FILE"
    elif raw:
        method = "JSON"
    elif raw_b64:
        method = "BASE64"
    else:
        method = "NONE"

    return {
        "method": method,
        "file_set": bool(file_path),
        "file_exists": bool(file_path and os.path.isfile(os.path.abspath(file_path))),
        "json_set": bool(raw),
        "json_length": len(raw),
        "base64_set": bool(raw_b64),
        "base64_length": len(raw_b64),
        "gsc_site_url_set": bool(site_url),
        "gsc_site_url": site_url,
    }


def log_google_config_status() -> None:
    info = google_config_diagnostics()
    logging.info(
        "Google config: method=%s file_set=%s file_exists=%s json_set=%s "
        "json_length=%s base64_set=%s base64_length=%s gsc_site_url_set=%s",
        info["method"],
        info["file_set"],
        info["file_exists"],
        info["json_set"],
        info["json_length"],
        info["base64_set"],
        info["base64_length"],
        info["gsc_site_url_set"],
    )


def _service_account_info() -> dict:
    file_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    raw_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64", "").strip()

    if file_path:
        path = os.path.abspath(file_path)
        if not os.path.isfile(path):
            raise RuntimeError(f"Файл сервисного аккаунта Google не найден: {path}")
        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Не удалось прочитать JSON-файл сервисного аккаунта: {path}"
            ) from exc
    else:
        if raw_b64:
            try:
                raw = base64.b64decode(raw_b64).decode("utf-8")
            except Exception as exc:
                raise RuntimeError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 содержит некорректный Base64"
                ) from exc

        if not raw:
            raise RuntimeError(
                "Не задан GOOGLE_SERVICE_ACCOUNT_FILE, "
                "GOOGLE_SERVICE_ACCOUNT_JSON или "
                "GOOGLE_SERVICE_ACCOUNT_JSON_BASE64"
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Некорректный JSON сервисного аккаунта Google") from exc

    if data.get("type") != "service_account":
        raise RuntimeError("Указан не service_account JSON")
    return data


def _google_credentials(scopes: list[str]):
    return service_account.Credentials.from_service_account_info(
        _service_account_info(),
        scopes=scopes,
    )


def _search_console_service():
    credentials = _google_credentials(
        ["https://www.googleapis.com/auth/webmasters.readonly"]
    )
    return build("searchconsole", "v1", credentials=credentials, cache_discovery=False)


def _site_url() -> str:
    return os.getenv("GSC_SITE_URL", "sc-domain:okvej.com.ua").strip()


async def search_console_summary(start: date, end: date) -> dict:
    service = _search_console_service()

    def execute():
        return service.searchanalytics().query(
            siteUrl=_site_url(),
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


async def search_console_rows(
    start: date,
    end: date,
    dimension: str,
    limit: int = 1000,
) -> list[dict]:
    service = _search_console_service()

    def execute():
        return service.searchanalytics().query(
            siteUrl=_site_url(),
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
    return f"{((current - previous) / previous * 100):+.1f}%"


def _short_page(page: str, max_length: int = 78) -> str:
    try:
        parsed = urlsplit(page)
        short = parsed.path or "/"
        if parsed.query:
            short += "?" + parsed.query
    except Exception:
        short = page.replace("https://okvej.com.ua", "") or "/"

    if len(short) > max_length:
        short = short[: max_length - 1] + "…"
    return html.escape(short)


def _rows_by_key(rows: list[dict]) -> dict[str, dict]:
    result = {}
    for row in rows:
        keys = row.get("keys") or []
        if not keys:
            continue
        result[str(keys[0])] = {
            "clicks": float(row.get("clicks", 0)),
            "impressions": float(row.get("impressions", 0)),
            "ctr": float(row.get("ctr", 0)),
            "position": float(row.get("position", 0)),
        }
    return result


def _lost_pages(
    current_rows: list[dict],
    previous_rows: list[dict],
    limit: int = 7,
) -> list[dict]:
    current = _rows_by_key(current_rows)
    previous = _rows_by_key(previous_rows)
    losses = []

    for page in set(current) | set(previous):
        now = current.get(page, {"clicks": 0.0, "impressions": 0.0})
        before = previous.get(page, {"clicks": 0.0, "impressions": 0.0})
        row = {
            "page": page,
            "click_delta": now["clicks"] - before["clicks"],
            "impression_delta": now["impressions"] - before["impressions"],
        }
        if row["click_delta"] < 0 or row["impression_delta"] < 0:
            losses.append(row)

    losses.sort(key=lambda row: (row["click_delta"], row["impression_delta"]))
    return losses[:limit]


def _losses_block(losses: list[dict]) -> list[str]:
    if not losses:
        return [
            "📉 <b>Страницы с падением не найдены</b>",
            "Среди полученных URL заметного снижения нет.",
        ]

    lines = ["📉 <b>Сильнее всего просели страницы:</b>"]
    for row in losses:
        clicks = int(round(row["click_delta"]))
        impressions = int(round(row["impression_delta"]))
        lines.extend(
            [
                f"• <b>{_short_page(row['page'])}</b>",
                f"  клики: <b>{clicks:+d}</b> · показы: <b>{impressions:+d}</b>",
            ]
        )
    return lines


async def seo_report_text(days: int = 7, losses_only: bool = False) -> str:
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    current_summary, previous_summary, current_queries, current_pages, previous_pages = (
        await asyncio.gather(
            search_console_summary(start, end),
            search_console_summary(prev_start, prev_end),
            search_console_rows(start, end, "query", 5),
            search_console_rows(start, end, "page", 1000),
            search_console_rows(prev_start, prev_end, "page", 1000),
        )
    )

    losses = _lost_pages(current_pages, previous_pages, limit=7)

    if losses_only:
        return "\n".join(
            [
                f"📉 <b>Падение страниц OKVEJ — {start:%d.%m}–{end:%d.%m}</b>",
                f"Сравнение с {prev_start:%d.%m}–{prev_end:%d.%m}",
                "",
                *_losses_block(losses),
                "",
                "ℹ️ Сначала исправляйте URL с потерей кликов, затем — показов.",
            ]
        )

    top_pages = sorted(
        current_pages,
        key=lambda row: float(row.get("clicks", 0)),
        reverse=True,
    )[:5]

    lines = [
        f"📈 <b>SEO OKVEJ — {start:%d.%m}–{end:%d.%m}</b>",
        "",
        (
            f"👀 Показы: <b>{current_summary['impressions']:,.0f}</b> "
            f"({_pct_delta(current_summary['impressions'], previous_summary['impressions'])})"
        ).replace(",", " "),
        (
            f"🖱 Клики: <b>{current_summary['clicks']:,.0f}</b> "
            f"({_pct_delta(current_summary['clicks'], previous_summary['clicks'])})"
        ).replace(",", " "),
        f"📍 Средняя позиция: <b>{current_summary['position']:.1f}</b>",
        f"🎯 CTR: <b>{current_summary['ctr'] * 100:.2f}%</b>",
    ]

    if current_queries:
        lines += ["", "<b>Топ запросов:</b>"]
        for row in current_queries:
            query = html.escape(str((row.get("keys") or [""])[0]))
            lines.append(f"• {query} — {float(row.get('clicks', 0)):.0f} кликов")

    if top_pages:
        lines += ["", "<b>Топ страниц:</b>"]
        for row in top_pages:
            page = str((row.get("keys") or [""])[0])
            lines.append(f"• {_short_page(page)} — {float(row.get('clicks', 0)):.0f}")

    lines += ["", *_losses_block(losses)]
    lines += ["", "ℹ️ Сравнение выполнено с предыдущим равным периодом."]
    return "\n".join(lines)
