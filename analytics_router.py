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
        os.getenv("ADMIN_CHAT_ID")
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
            [
                InlineKeyboardButton(
                    text="SEO за 7 дней",
                    callback_data="seo:7",
                )
            ],
            [
                InlineKeyboardButton(
                    text="SEO за 28 дней",
                    callback_data="seo:28",
                )
            ],
        ]
    )


def add_admin_buttons(old_menu):
    """Adds the private analytics row while preserving the current menu."""
    try:
        rows = [list(row) for row in old_menu.keyboard]
    except Exception:
        rows = []

    button_texts = {
        getattr(button, "text", "")
        for row in rows
        for button in row
    }

    if "📊 Аналитика" not in button_texts and "📈 SEO" not in button_texts:
        rows.append(
            [
                KeyboardButton(text="📊 Аналитика"),
                KeyboardButton(text="📈 SEO"),
            ]
        )

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


async def deny(message_or_callback):
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.answer("Нет доступа", show_alert=True)
    # For normal users the hidden admin text buttons should not be advertised.


@router.message(Command("stats"))
@router.message(F.text == "📊 Аналитика")
async def stats_menu(message: Message):
    if not is_admin(message.from_user.id):
        await deny(message)
        return
    await message.answer(
        "📊 <b>Аналитика продаж OKVEJ</b>\n\nВыберите период:",
        parse_mode="HTML",
        reply_markup=analytics_keyboard(),
    )


@router.callback_query(F.data.startswith("stats:"))
async def stats_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await deny(callback)
        return

    period = callback.data.split(":", 1)[1]
    await callback.answer("Получаю данные Хорошоп…")

    try:
        text = await sales_period_text(period)
        await callback.message.answer(text, parse_mode="HTML")
    except Exception as exc:
        await callback.message.answer(
            "❌ <b>Не удалось получить аналитику продаж.</b>\n\n"
            f"<code>{str(exc)[:1200]}</code>\n\n"
            "Проверьте HOROSHOP_LOGIN, HOROSHOP_PASSWORD и "
            "HOROSHOP_ORDERS_ENDPOINT в Railway.",
            parse_mode="HTML",
        )


@router.message(Command("seo"))
@router.message(F.text == "📈 SEO")
async def seo_menu(message: Message):
    if not is_admin(message.from_user.id):
        await deny(message)
        return
    await message.answer(
        "📈 <b>SEO-мониторинг OKVEJ</b>\n\nВыберите период:",
        parse_mode="HTML",
        reply_markup=seo_keyboard(),
    )


@router.callback_query(F.data.startswith("seo:"))
async def seo_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await deny(callback)
        return

    days = int(callback.data.split(":", 1)[1])
    await callback.answer("Получаю данные Search Console…")

    try:
        text = await seo_report_text(days)
        await callback.message.answer(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        await callback.message.answer(
            "❌ <b>Не удалось получить SEO-данные.</b>\n\n"
            f"<code>{str(exc)[:1200]}</code>\n\n"
            "Проверьте GSC_SITE_URL и ключ сервисного аккаунта Google.",
            parse_mode="HTML",
        )


@router.message(Command("panel"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await deny(message)
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Продажи",
                    callback_data="stats:today",
                ),
                InlineKeyboardButton(
                    text="📈 SEO",
                    callback_data="seo:7",
                ),
            ]
        ]
    )

    await message.answer(
        "⚙️ <b>Панель OKVEJ</b>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
