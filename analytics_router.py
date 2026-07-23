import asyncio
import html
import logging
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup

from analytics_seo import google_config_diagnostics, growth_opportunities_text, improve_page_text, page_report_text, query_report_text, seo_report_text

router = Router(name="okvej_seo_free")

class SeoInput(StatesGroup):
    query = State()
    page = State()

def admin_id():
    return (os.getenv("ADMIN_USER_ID") or os.getenv("ADMIN_CHAT_ID") or os.getenv("MANAGER_CHAT_ID") or "").strip()

def is_admin(user_id):
    return bool(admin_id()) and str(user_id) == admin_id()

def seo_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="SEO за 7 дней", callback_data="seo:7")],
        [InlineKeyboardButton(text="SEO за 28 дней", callback_data="seo:28")],
        [InlineKeyboardButton(text="📉 Что просело", callback_data="seo:losses:28")],
        [InlineKeyboardButton(text="🔎 Проверить запрос", callback_data="seo:ask_query")],
        [InlineKeyboardButton(text="📄 Анализ страницы", callback_data="seo:ask_page")],
        [InlineKeyboardButton(text="🏆 Возможности роста", callback_data="seo:growth")],
    ])

def add_admin_buttons(old_menu):
    try:
        rows = [list(row) for row in old_menu.keyboard]
    except Exception:
        rows = []
    rows = [[b for b in row if getattr(b, "text", "") != "📊 Аналитика"] for row in rows]
    rows = [r for r in rows if r]
    labels = {getattr(b, "text", "") for row in rows for b in row}
    for label in ("📈 SEO", "🔎 Проверить запрос", "📄 Анализ страницы", "🏆 Возможности роста"):
        if label not in labels:
            rows.append([KeyboardButton(text=label)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)

async def safe(message, coro):
    try:
        text = await asyncio.wait_for(coro, timeout=90)
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    except asyncio.TimeoutError:
        await message.answer("⏱ Google Search Console не ответил за 90 секунд.")
    except Exception as exc:
        logging.exception("SEO request failed")
        await message.answer(f"❌ <b>Ошибка SEO</b>\n<code>{html.escape(str(exc)[:1000])}</code>", parse_mode="HTML")

@router.message(Command("seo"))
@router.message(F.text == "📈 SEO")
async def menu(message: Message):
    if message.from_user and is_admin(message.from_user.id):
        await message.answer("📈 <b>SEO-панель OKVEJ</b>", parse_mode="HTML", reply_markup=seo_keyboard())

@router.callback_query(F.data.startswith("seo:"))
async def callback(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    action = (callback.data or "").split(":")[1]
    await callback.answer()
    if action == "ask_query":
        await state.set_state(SeoInput.query); await callback.message.answer("Введите поисковый запрос:"); return
    if action == "ask_page":
        await state.set_state(SeoInput.page); await callback.message.answer("Введите страницу, например /ua/:"); return
    if action == "growth":
        await safe(callback.message, growth_opportunities_text()); return
    parts = callback.data.split(":")
    await safe(callback.message, seo_report_text(days=int(parts[-1]), losses_only=action == "losses"))

@router.message(F.text == "🔎 Проверить запрос")
async def query_button(message: Message, state: FSMContext):
    if message.from_user and is_admin(message.from_user.id):
        await state.set_state(SeoInput.query); await message.answer("Введите поисковый запрос:")

@router.message(F.text == "📄 Анализ страницы")
async def page_button(message: Message, state: FSMContext):
    if message.from_user and is_admin(message.from_user.id):
        await state.set_state(SeoInput.page); await message.answer("Введите страницу, например /ua/:")

@router.message(F.text == "🏆 Возможности роста")
async def growth_button(message: Message):
    if message.from_user and is_admin(message.from_user.id):
        await safe(message, growth_opportunities_text())

@router.message(SeoInput.query)
async def query_state(message: Message, state: FSMContext):
    await state.clear(); await safe(message, query_report_text(message.text or ""))

@router.message(SeoInput.page)
async def page_state(message: Message, state: FSMContext):
    await state.clear(); await safe(message, page_report_text(message.text or ""))

@router.message(Command("query"))
async def query_cmd(message: Message):
    if message.from_user and is_admin(message.from_user.id):
        await safe(message, query_report_text((message.text or "").partition(" ")[2]))

@router.message(Command("page"))
async def page_cmd(message: Message):
    if message.from_user and is_admin(message.from_user.id):
        await safe(message, page_report_text((message.text or "").partition(" ")[2]))

@router.message(Command("improve"))
async def improve_cmd(message: Message):
    if message.from_user and is_admin(message.from_user.id):
        await safe(message, improve_page_text((message.text or "").partition(" ")[2]))

@router.message(Command("panel"))
async def panel(message: Message):
    if message.from_user and is_admin(message.from_user.id):
        await message.answer("⚙️ <b>SEO-панель OKVEJ</b>", parse_mode="HTML", reply_markup=seo_keyboard())

@router.message(Command("diag"))
async def diag(message: Message):
    if message.from_user and is_admin(message.from_user.id):
        info = google_config_diagnostics()
        await message.answer(f"🧪 <b>Диагностика SEO</b>\nGoogle-ключ: <b>{info['method']}</b>\nGSC_SITE_URL: <b>{'задан' if info['gsc_site_url_set'] else 'не задан'}</b>", parse_mode="HTML")
