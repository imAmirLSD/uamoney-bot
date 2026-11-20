# -*- coding: utf-8 -*-
"""Вспомогательное: клавиатуры, рассылки, пагинация."""
import asyncio
import sqlite3
import json
import time
import random
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import core
from core import *
from states import *

from db import get_all_users, get_setting, get_user, get_withdrawal, update_user_notification_time

# Главное меню (Reply клавиатура внизу экрана)
EMOJI = {
    "tasks": "5438434143069314644",
    "tasks2": "5438434143069314644",
    "tasks3": "5438434143069314644",
    "tasks4": "5438434143069314644",
    "refs": "5411350246804461567",
    "cabinet": "5413740297320440148",
    "promo": "5413822103562524971",
    "info": "5411277545893044259",
    "admin": "5438434143069314644",
}

def get_main_keyboard(user_id: int):
    """Главное нижнее меню: только custom/Premium Emoji, без обычных emoji в подписях."""
    keyboard = ReplyKeyboardBuilder()
    keyboard.button(text="Задания",      icon_custom_emoji_id=EMOJI["tasks"])
    keyboard.button(text="Задания №2",   icon_custom_emoji_id=EMOJI["tasks2"])
    keyboard.button(text="Задания №3",   icon_custom_emoji_id=EMOJI["tasks3"])
    keyboard.button(text="Задания №4",   icon_custom_emoji_id=EMOJI["tasks4"])
    keyboard.button(text="Рефералы",     icon_custom_emoji_id=EMOJI["refs"])
    keyboard.button(text="Кабинет",      icon_custom_emoji_id=EMOJI["cabinet"])
    keyboard.button(text="Продвижение",  icon_custom_emoji_id=EMOJI["promo"])
    keyboard.button(text="Инфо",         icon_custom_emoji_id=EMOJI["info"])
    if user_id in ADMIN_IDS:
        keyboard.button(text="Админка",   icon_custom_emoji_id=EMOJI["admin"])
    keyboard.adjust(2, 2, 2, 2, 1)
    return keyboard.as_markup(resize_keyboard=True)

def promo_pagination_keyboard(tasks, page, total_pages, mode):
    """Создаёт клавиатуру для страниц"""
    kb = InlineKeyboardBuilder()
    for task in tasks:
        kb.button(
            text=f"📢 {task['channel_username']} ({task['current_completions']}/{task['max_completions']})",
            callback_data=f"promo_info_{task['id']}"
        )
    kb.adjust(1)

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"promo_{mode}_page_{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️ Далее", callback_data=f"promo_{mode}_page_{page+1}"))
    if nav:
        kb.row(*nav)

    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="promo_back_menu"))
    return kb.as_markup()


# Функція розсилки
async def notify_all_users_about_task():
    try:
        users = await get_all_users()
        notification_text = (
            f"🎉 <b>Появилось новое задание!</b>\n\n"
            f"➡️ Перейдите в раздел <b>\"📝 Задания №2\"</b> чтобы выполнить его!"
        )

        async def send_notification(user_id: int, delay: int):
            try:
                await asyncio.sleep(delay)
                await bot.send_message(user_id, notification_text, parse_mode='HTML')
                await update_user_notification_time(user_id)
            except Exception as e:
                print(f"❌ Ошибка уведомления пользователя {user_id}: {e}")

        tasks = []
        for user in users:
            uid = user[0]
            tasks.append(asyncio.create_task(send_notification(uid, 0)))

        # Запускаємо без блокування
        asyncio.create_task(_gather_notifications(tasks))

    except Exception as e:
        print(f"❌ Ошибка при рассылке уведомлений: {e}")


# Админ клавиатура
def get_admin_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💰 Баланс резерва")],
            [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="🧹 Проверка неактивных")],  # ← НОВАЯ КНОПКА
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="🧾 Заявки на вывод")],
            [KeyboardButton(text="🔙 Главное меню")]
        ],
        resize_keyboard=True
    )
    return keyboard


def paginate_list(items, page: int, page_size: int = 12):
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = start + page_size

    return {
        "items": items[start:end],
        "page": page,
        "total_pages": total_pages
    }

# --- Меню вибору мов з пагінацією ---
def build_lang_page_markup(page: int, allowed: list[str]):
    # page: 1-based
    start = (page - 1) * LANGS_PER_PAGE
    end = start + LANGS_PER_PAGE
    slice_langs = ALL_LANGUAGES[start:end]

    kb = InlineKeyboardBuilder()

    # Додаємо кнопки мов — 3 в ряд
    row = []
    for i, (code, flag, name) in enumerate(slice_langs, 1):
        checked = "🟢" if code in allowed else "🔴"
        row.append(InlineKeyboardButton(text=f"{checked} {flag} {name}", callback_data=f"toggle_lang_{code}_page_{page}"))
        # вставка по 3 в ряд
        if len(row) == 3:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)

    # Навігація сторінок
    total_pages = (len(ALL_LANGUAGES) + LANGS_PER_PAGE - 1) // LANGS_PER_PAGE
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"edit_allowed_langs_page_{page-1}"))

    nav.append(InlineKeyboardButton(text=f"Стр. {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️ Далее", callback_data=f"edit_allowed_langs_page_{page+1}"))
    kb.row(*nav)

    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="withdraw_requirements"))
    return kb.as_markup()

async def send_withdraw_request_to_admins(withdrawal_id):
    wd = await get_withdrawal(withdrawal_id)
    user = await get_user(wd["user_id"])

    payout_method = wd.get("payout_method", "usdt") or "usdt"
    # Резерв CryptoBot нужен только для автоматического вывода USDT.
    if payout_method == "usdt":
        from services import get_crypto_bot_balance
        total_balance = await get_crypto_bot_balance()
        reserve_status, reserve_emoji, rec = await get_reserve_status(total_balance)
    else:
        total_balance = None
        rec = "Заявка на ручную выплату Telegram Stars."

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"wd_confirm:{withdrawal_id}")
    kb.button(text="❌ Отклонить (без возврата)", callback_data=f"wd_norefund:{withdrawal_id}")
    kb.button(text="↩️ Отклонить (с возвратом)", callback_data=f"wd_refund:{withdrawal_id}")
    kb.button(text="👤 О пользователе", callback_data=f"wd_userinfo:{wd['user_id']}")
    kb.adjust(1, 2, 1)

    text = f"""
📨 <b>НОВАЯ ЗАЯВКА НА ВЫВОД</b>

🆔 User ID: <code>{user['user_id']}</code>
👤 Username: @{user['username'] or 'нет'}

💵 Сумма вывода: <b>{wd['amount']:.3f}$</b>
{f"⭐ Количество Stars: <b>{int(wd.get('stars_amount') or 0)}</b>" if payout_method == 'stars' else '💎 Способ: <b>USDT</b>'}
🔖 ID заявки: <code>{withdrawal_id}</code>
⏳ Статус: <b>Ожидает</b>
━━━━━━━━━━━━━━━━━━
{f"💰 <b>Резерв Системы</b>\n\n🔢 Баланс: <b>{total_balance:.2f} USDT</b>\n\n" if total_balance is not None else ""}
<i>{rec}</i>
"""

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb.as_markup())
        except:
            pass

async def send_payment_notification(user_id: int, amount: float):
    channel = await get_setting("payments_channell")
    if not channel:
        return

    # Створюємо кликабельне слово "пользователю"
    user_link = f"<a href='tg://user?id={user_id}'>Пользователю</a>"

    text = (
        f"<b>✅ {user_link} выплачено {amount}$ на 💎 CryptoBot</b>"
    )

    # Твій бот
    me = await bot.get_me()
    bot_url = f"https://t.me/{me.username}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="💎 Открыть бота",
                url=bot_url
            )
        ]]
    )

    try:
        await bot.send_message(
            chat_id=channel,
            text=text,
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        print("Ошибка отправки уведомления:", e)

async def notify_admins_about_withdrawal(withdrawal_id, amount, username, receipt_url):
    """Отправляет отчет админам о выводе с защитой от дублирования"""

    # Добавляем получение текущего времени
    current_time = time.time()

    # Проверяем, не отправляли ли мы уже уведомление для этого withdrawal_id в последние 10 секунд
    if (withdrawal_id in core.last_withdrawal_notification and 
        current_time - core.last_withdrawal_notification[withdrawal_id] < 10):
        print(f"⚠️ Пропускаем дублирующее уведомление для вывода {withdrawal_id}")
        return

    # Используем реальный баланс кошелька
    from services import get_crypto_bot_balance
    total_balance = await get_crypto_bot_balance()

    # Определяем статус резерва
    if total_balance < 1:
        reserve_status = "🚨 КРИТИЧЕСКИ НИЗКИЙ"
        reserve_emoji = "🔴"
    elif total_balance < 10:
        reserve_status = "⚠️ НИЗКИЙ"
        reserve_emoji = "🟡"
    elif total_balance < 50:
        reserve_status = "ℹ️ СРЕДНИЙ"
        reserve_emoji = "🟢"
    else:
        reserve_status = "✅ ДОСТАТОЧНЫЙ"
        reserve_emoji = "💚"

    # Создаем сообщение для админа
    admin_message = f"""
📊 <b>АВТОМАТИЧЕСКИЙ ВЫВОД СРЕДСТВ</b>

👤 <b>Пользователь:</b> @{username or 'нет'}
🔹 <b>ID вывода:</b> <code>{withdrawal_id}</code>
💵 <b>Сумма:</b> <code>{amount:.3f}$</code>
📎 <b>Чек:</b> {receipt_url}

💰 <b>БАЛАНС КОШЕЛЬКА:</b>
├─ Доступно: <b>{total_balance:.2f} USDT</b>
└─ Статус: {reserve_emoji} <b>{reserve_status}</b>

⏰ <i>{datetime.now().strftime('%d.%m.%Y %H:%M')}</i>
"""

    # Отправляем сообщение всем админам
    sent_count = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_message, parse_mode='HTML')
            sent_count += 1
        except Exception as e:
            print(f"❌ Не удалось уведомить админа {admin_id}: {e}")

    # Сохраняем время отправки
    core.last_withdrawal_notification[withdrawal_id] = current_time
    print(f"📨 Отчет о выводе {withdrawal_id} отправлен {sent_count} админам")

async def send_private_message_to_user(user_id, message):
    """Отправляет приватное сообщение пользователю"""
    try:
        await bot.send_message(
            user_id,
            message,
            parse_mode='HTML'
        )
        print(f"📩 [PRIVATE] Сообщение отправлено пользователю {user_id}")
    except Exception as e:
        print(f"❌ Не удалось отправить сообщение пользователю {user_id}: {e}")


async def _gather_notifications(tasks):
    """Ожидает все задачи рассылки внутри корректной coroutine."""
    await asyncio.gather(*tasks)
