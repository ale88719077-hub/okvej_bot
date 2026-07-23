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

from analytics_seo import sales_period_text, seo_report_text

router = Router(name="okvej_analytics_seo")


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


def analytics_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="stats:today"),
                InlineKeyboardButton(text="7 дней", callback_data="stats:week"),
            ],
            [InlineKeyboardButton(text="Месяц", callback_data="stats:month")],
        ]
    )


def seo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="SEO за 7 дней", callback_data="seo:7")],
            [InlineKeyboardButton(text="SEO за 28 дней", callback_data="seo:28")],
        ]
    )


def add_admin_buttons(old_menu):
    try:
        rows = [list(row) for row in old_menu.keyboard]
    except Exception:
        rows = []

    button_texts = {
        getattr(button, "text", "")
        for row in rows
        for button in row
    }

    new_row = []
    if "📊 Аналитика" not in button_texts:
        new_row.append(KeyboardButton(text="📊 Аналитика"))
    if "📈 SEO" not in button_texts:
        new_row.append(KeyboardButton(text="📈 SEO"))
    if new_row:
        rows.append(new_row)

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def deny(message_or_callback) -> None:
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.answer("Нет доступа", show_alert=True)


@router.message(Command("stats"))
@router.message(F.text == "📊 Аналитика")
async def stats_menu(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await deny(message)
        return

    await message.answer(
        "📊 <b>Аналитика продаж OKVEJ</b>\n\nВыберите период:",
        parse_mode="HTML",
        reply_markup=analytics_keyboard(),
    )


@router.callback_query(F.data.startswith("stats:"))
async def stats_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await deny(callback)
        return

    period = (callback.data or "").split(":", 1)[1]
    await callback.answer("Получаю данные Хорошоп…")

    try:
        text = await sales_period_text(period)
        if callback.message:
            await callback.message.answer(text, parse_mode="HTML")
    except Exception as exc:
        logging.exception("Cannot load Horoshop sales analytics")
        if callback.message:
            await callback.message.answer(
                "❌ <b>Не удалось получить аналитику продаж.</b>\n\n"
                f"<code>{str(exc)[:1200]}</code>\n\n"
                "Проверьте HOROSHOP_LOGIN, HOROSHOP_PASSWORD и "
                "HOROSHOP_ORDERS_ENDPOINT в Railway.",
                parse_mode="HTML",
            )


@router.message(Command("seo"))
@router.message(F.text == "📈 SEO")
async def seo_menu(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await deny(message)
        return

    await message.answer(
        "📈 <b>SEO-мониторинг OKVEJ</b>\n\nВыберите период:",
        parse_mode="HTML",
        reply_markup=seo_keyboard(),
    )


@router.callback_query(F.data.startswith("seo:"))
async def seo_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await deny(callback)
        return

    try:
        days = int((callback.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный период", show_alert=True)
        return

    await callback.answer("Получаю данные Search Console…")

    try:
        text = await seo_report_text(days)
        if callback.message:
            await callback.message.answer(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    except Exception as exc:
        logging.exception("Cannot load Google Search Console analytics")
        if callback.message:
            await callback.message.answer(
                "❌ <b>Не удалось получить SEO-данные.</b>\n\n"
                f"<code>{str(exc)[:1200]}</code>\n\n"
                "Проверьте GSC_SITE_URL и ключ сервисного аккаунта Google.",
                parse_mode="HTML",
            )


@router.message(Command("panel"))
async def admin_panel(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await deny(message)
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Продажи", callback_data="stats:today"),
                InlineKeyboardButton(text="📈 SEO", callback_data="seo:7"),
            ]
        ]
    )

    await message.answer(
        "⚙️ <b>Панель OKVEJ</b>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
