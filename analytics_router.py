import asyncio
import html
import logging
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from analytics_seo import google_config_diagnostics, seo_report_text

router = Router(name="okvej_seo_pro")


def admin_id() -> str:
    return (
        os.getenv("ADMIN_USER_ID")
        or os.getenv("ADMIN_CHAT_ID")
        or os.getenv("MANAGER_CHAT_ID")
        or ""
    ).strip()


def is_admin(user_id: int) -> bool:
    configured = admin_id()
    return bool(configured) and str(user_id) == configured


def seo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="SEO за 7 дней", callback_data="seo:7")],
            [InlineKeyboardButton(text="SEO за 28 дней", callback_data="seo:28")],
            [
                InlineKeyboardButton(
                    text="📉 Что просело за 7 дней",
                    callback_data="seo:losses:7",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📉 Что просело за 28 дней",
                    callback_data="seo:losses:28",
                )
            ],
        ]
    )


def add_admin_buttons(old_menu):
    try:
        rows = [list(row) for row in old_menu.keyboard]
    except Exception:
        rows = []

    cleaned_rows = []
    for row in rows:
        filtered = [
            button
            for button in row
            if getattr(button, "text", "") != "📊 Аналитика"
        ]
        if filtered:
            cleaned_rows.append(filtered)
    rows = cleaned_rows

    button_texts = {
        getattr(button, "text", "")
        for row in rows
        for button in row
    }
    if "📈 SEO" not in button_texts:
        rows.append([KeyboardButton(text="📈 SEO")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=getattr(old_menu, "is_persistent", True),
    )


async def deny(message_or_callback) -> None:
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.answer("Нет доступа", show_alert=True)


@router.message(Command("seo"))
@router.message(F.text == "📈 SEO")
async def seo_menu(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await deny(message)
        return

    await message.answer(
        "📈 <b>SEO-мониторинг OKVEJ</b>\n\n"
        "Выберите период или посмотрите страницы, которые просели:",
        parse_mode="HTML",
        reply_markup=seo_keyboard(),
    )


@router.callback_query(F.data.startswith("seo:"))
async def seo_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await deny(callback)
        return

    parts = (callback.data or "").split(":")
    losses_only = len(parts) >= 3 and parts[1] == "losses"

    try:
        days = int(parts[-1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный период", show_alert=True)
        return

    await callback.answer("Получаю данные Search Console…")

    try:
        text = await asyncio.wait_for(
            seo_report_text(days=days, losses_only=losses_only),
            timeout=75,
        )
        if callback.message:
            await callback.message.answer(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    except asyncio.TimeoutError:
        logging.error("Google Search Console request timed out")
        if callback.message:
            await callback.message.answer(
                "⏱ <b>Google Search Console не ответил за 75 секунд.</b>\n\n"
                "Повторите запрос позже.",
                parse_mode="HTML",
            )
    except Exception as exc:
        logging.exception("Cannot load Google Search Console analytics")
        if callback.message:
            await callback.message.answer(
                "❌ <b>Не удалось получить SEO-данные.</b>\n\n"
                f"<code>{html.escape(str(exc)[:1200])}</code>\n\n"
                "Проверьте GSC_SITE_URL и доступ сервисного аккаунта.",
                parse_mode="HTML",
            )


@router.message(Command("panel"))
async def admin_panel(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await deny(message)
        return

    await message.answer(
        "⚙️ <b>SEO-панель OKVEJ</b>",
        parse_mode="HTML",
        reply_markup=seo_keyboard(),
    )


@router.message(Command("diag"))
async def diagnostics_panel(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await deny(message)
        return

    info = google_config_diagnostics()
    text = (
        "🧪 <b>Диагностика SEO OKVEJ</b>\n\n"
        f"Администратор: <b>{'найден' if admin_id() else 'не задан'}</b>\n"
        f"Google-ключ: <b>{info['method']}</b>\n"
        f"GOOGLE_SERVICE_ACCOUNT_JSON: "
        f"<b>{'есть' if info['json_set'] else 'нет'}</b> "
        f"({info['json_length']} символов)\n"
        f"GOOGLE_SERVICE_ACCOUNT_JSON_BASE64: "
        f"<b>{'есть' if info['base64_set'] else 'нет'}</b> "
        f"({info['base64_length']} символов)\n"
        f"GOOGLE_SERVICE_ACCOUNT_FILE: "
        f"<b>{'есть' if info['file_set'] else 'нет'}</b>\n"
        f"Файл существует: <b>{'да' if info['file_exists'] else 'нет'}</b>\n"
        f"GSC_SITE_URL: "
        f"<b>{'задан' if info['gsc_site_url_set'] else 'не задан'}</b>"
    )
    await message.answer(text, parse_mode="HTML")
