# -*- coding: utf-8 -*-
"""Все Telegram-обработчики. Разделы расположены по назначению."""
import asyncio, html, json, os, random, re, sqlite3, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import aiohttp, aiogram
try:
    from aiogram.types import WebAppInfo
except ImportError:  # на старых версиях aiogram кнопка просто не появится
    WebAppInfo = None
from aiogram.types import FSInputFile as _AiogramFSInputFile
import core
from core import *
from states import *
from db import *
from services import *
from utils import *
from aiogram.types import LinkPreviewOptions


# ===== handlers_user.py =====

async def _promo_target_is_member(user_id: int, target_username: str) -> bool:
    """Проверяет, состоит ли пользователь в продвигаемом канале или чате."""
    try:
        username = str(target_username or "").strip()
        if not username:
            return False
        if not username.startswith("@"):
            username = "@" + username
        chat = await bot.get_chat(username)
        member = await bot.get_chat_member(chat.id, user_id)
        status = getattr(member, "status", None)
        if status in ("member", "administrator", "creator"):
            return True
        return status == "restricted" and bool(getattr(member, "is_member", False))
    except Exception as exc:
        print(f"⚠️ Не удалось проверить членство пользователя {user_id} в {target_username}: {exc}")
        return False


# ===== Контроль прав администратора для рекламных каналов и чатов =====
_promo_pause_schema_ready = False
_promo_pause_monitor_task = None
_promo_bot_id_cache = None
async def _ensure_promo_pause_schema():
    """Добавляет флаг паузы, не меняя существующие статусы заданий."""
    global _promo_pause_schema_ready
    if _promo_pause_schema_ready:
        return
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute("PRAGMA table_info(user_tasks)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        migrations = (
            ("is_paused", "ALTER TABLE user_tasks ADD COLUMN is_paused INTEGER NOT NULL DEFAULT 0"),
            ("is_closed", "ALTER TABLE user_tasks ADD COLUMN is_closed INTEGER NOT NULL DEFAULT 0"),
            ("refunded_amount", "ALTER TABLE user_tasks ADD COLUMN refunded_amount REAL NOT NULL DEFAULT 0"),
        )
        for column_name, statement in migrations:
            if column_name not in columns:
                try:
                    await db.execute(statement)
                except Exception as exc:
                    # Защита от одновременного запуска миграции несколькими обработчиками.
                    if "duplicate column" not in str(exc).lower():
                        raise
        await db.commit()
    _promo_pause_schema_ready = True


def _promo_admin_missing_text(task_type: str) -> str:
    """Уведомление, если владелец не добавил бота администратором в канал или чат."""
    if task_type == "chat":
        object_line = "Администратором в этот чат."
    else:
        object_line = "Администратором в этот канал."

    return (
        '<tg-emoji emoji-id="5411418403640484720">⚠️</tg-emoji> '
        'Чтобы создать задание, добавьте нашего бота '
        '<a href="https://t.me/UAMONEY_BOT">@UAMONEY_BOT</a>\n'
        f'{object_line}\n\n'
        'Затем отправьте ссылку ещё раз.'
    )


async def _promo_bot_admin_status(target_username: str):
    """True — бот администратор; False — бот не добавлен/не администратор; None — временная ошибка API."""
    global _promo_bot_id_cache
    username = str(target_username or "").strip().lstrip("@")
    if not username:
        return None

    # Сначала отдельно убеждаемся, что публичный объект существует и доступен по username.
    try:
        chat = await bot.get_chat("@" + username)
    except Exception as exc:
        print(f"⚠️ Не удалось получить чат/канал @{username}: {exc}")
        return None

    try:
        if _promo_bot_id_cache is None:
            _promo_bot_id_cache = (await bot.get_me()).id
        member = await bot.get_chat_member(chat.id, _promo_bot_id_cache)
        return getattr(member, "status", None) in ("administrator", "creator")
    except (aiogram.exceptions.TelegramBadRequest, aiogram.exceptions.TelegramForbiddenError) as exc:
        # Telegram часто отвечает Bad Request / Forbidden, когда самого бота
        # ещё нет среди участников. Для владельца это означает, что бота нужно
        # добавить администратором, поэтому показываем подготовленное уведомление.
        print(f"ℹ️ Бот ещё не имеет админ-доступа к @{username}: {exc}")
        return False
    except Exception as exc:
        # Сетевые и прочие непредвиденные ошибки не выдаём за отсутствие прав.
        print(f"⚠️ Временная ошибка проверки прав бота в @{username}: {exc}")
        return None


async def _refresh_promo_task_pauses():
    """Ставит channel/chat-заказы на паузу без админ-прав и снимает её после восстановления прав."""
    await _ensure_promo_pause_schema()
    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, channel_username
            FROM user_tasks
            WHERE is_active = TRUE
              AND task_type IN ('channel', 'chat')
              AND current_completions < max_completions
        """) as cursor:
            rows = await cursor.fetchall()

    status_cache = {}
    changes = []
    for row in rows:
        username = str(row["channel_username"] or "").strip().lstrip("@")
        if not username:
            continue
        if username not in status_cache:
            status_cache[username] = await _promo_bot_admin_status(username)
        has_admin = status_cache[username]
        if has_admin is not None:
            changes.append((0 if has_admin else 1, row["id"]))

    if changes:
        async with aiosqlite.connect("bot_database.db") as db:
            await db.executemany(
                "UPDATE user_tasks SET is_paused = ? WHERE id = ? AND is_active = TRUE",
                changes,
            )
            await db.commit()


async def _promo_pause_monitor():
    """Периодически отслеживает удаление/возврат бота в администраторах."""
    while True:
        try:
            await _refresh_promo_task_pauses()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"⚠️ Ошибка мониторинга рекламных заданий: {exc}")
        await asyncio.sleep(45)


def _ensure_promo_pause_monitor_started():
    """Запускает монитор один раз при первом обращении пользователя к боту."""
    global _promo_pause_monitor_task
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    if _promo_pause_monitor_task is None or _promo_pause_monitor_task.done():
        _promo_pause_monitor_task = asyncio.create_task(_promo_pause_monitor())


@dp.my_chat_member()
async def promo_bot_membership_changed(update: types.ChatMemberUpdated):
    """Сразу ставит задания на паузу/снимает паузу при изменении статуса бота в проекте."""
    username = getattr(update.chat, "username", None)
    if not username:
        return

    new_status = getattr(update.new_chat_member, "status", None)
    is_admin = new_status in ("administrator", "creator")
    await _ensure_promo_pause_schema()
    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute(
            "UPDATE user_tasks SET is_paused = ? "
            "WHERE channel_username = ? COLLATE NOCASE "
            "AND task_type IN ('channel', 'chat') AND is_active = TRUE",
            (0 if is_admin else 1, username),
        )
        await db.commit()

# -*- coding: utf-8 -*-
"""Пользовательские хендлеры: /start, задания, кабинет, рефералы."""



@dp.callback_query(F.data.regexp(r"promo_(active|done)_page_\d+"))
async def show_promo_page(call: types.CallbackQuery):
    """Показывает только задания на каналы и чаты."""
    _ensure_promo_pause_monitor_started()
    await _refresh_promo_task_pauses()
    parts = call.data.split("_")
    mode = parts[1]
    page = int(parts[-1])
    user_id = call.from_user.id
    per_page = 5
    offset = (page - 1) * per_page
    is_active = 1 if mode == "active" else 0

    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_tasks WHERE created_by=? AND is_active=? "
            "AND task_type IN ('channel', 'chat') "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (user_id, is_active, per_page, offset)
        ) as cursor:
            rows = await cursor.fetchall()
        async with db.execute(
            "SELECT COUNT(*) FROM user_tasks WHERE created_by=? AND is_active=? "
            "AND task_type IN ('channel', 'chat')",
            (user_id, is_active)
        ) as cursor:
            total = (await cursor.fetchone())[0]

    total_pages = max(1, ceil(total / per_page))
    if not rows:
        await call.message.edit_text(
            "❌ У вас пока нет таких заказов.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="promo_back_menu")]]
            )
        )
        await call.answer()
        return

    title = "📈 Активные заказы" if is_active else "✅ Завершённые заказы"
    await call.message.edit_text(
        f"{title}\n\nСтраница {page}/{total_pages}",
        reply_markup=promo_pagination_keyboard(rows, page, total_pages, mode),
        parse_mode="HTML"
    )
    await call.answer()

@dp.callback_query(F.data == "promo_back_menu")
async def promo_back_menu(call: types.CallbackQuery, state: FSMContext):
    """Возврат в меню продвижения с актуальными данными пользователя."""
    _ensure_promo_pause_monitor_started()
    await _refresh_promo_task_pauses()
    await call.message.delete()
    await _send_promotion_menu(call.message, call.from_user.id)
    await call.answer()


@dp.callback_query(F.data.regexp(r"promo_info_\d+"))
async def show_promo_info(call: types.CallbackQuery):
    _ensure_promo_pause_monitor_started()
    await _refresh_promo_task_pauses()
    task_id = int(call.data.split("_")[-1])
    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_tasks WHERE id=? AND created_by=? AND task_type IN ('channel', 'chat')",
            (task_id, call.from_user.id)
        ) as cursor:
            task = await cursor.fetchone()

    if not task:
        await call.answer("❌ Заказ не найден или этот тип заданий больше не поддерживается.", show_alert=True)
        return

    task_type = task["task_type"] if "task_type" in task.keys() else "channel"
    type_text, type_emoji = ("Канал", "📢") if task_type == "channel" else ("Чат", "💬")
    created_at = datetime.fromisoformat(task['created_at'])
    msk_time = created_at.astimezone(MSK).strftime("%d.%m.%Y %H:%M:%S")
    text = (
        f"{type_emoji} <b>Информация о заказе #{task['id']}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📂 Тип: <b>{type_text}</b>\n"
        f"📎 Ссылка: {task['channel_link']}\n"
        f"👤 Username: @{task['channel_username']}\n"
        f"💰 Цена за выполнение: <b>{task['price']:.3f}$</b>\n"
        f"📊 Выполнено: <b>{task['current_completions']}/{task['max_completions']}</b>\n"
        f"📅 Создано: {msk_time} (МСК)\n"
        f"🟠 Статус: "
        f"{'Закрыт владельцем' if task['is_closed'] else ('На паузе — бот не администратор' if task['is_active'] and task['is_paused'] else ('Активный' if task['is_active'] else 'Завершён'))}"
    )
    if task['is_closed']:
        text += f"\n💸 Возвращено на рекламный баланс: <b>{float(task['refunded_amount'] or 0):.3f}$</b>"
    buttons = []
    if task['is_active']:
        buttons.append(InlineKeyboardButton(
            text="⛔ Закрыть задание и вернуть остаток",
            callback_data=f"promo_close_{task_id}"
        ))
    buttons.append(InlineKeyboardButton(
        text="🔙 Назад",
        callback_data=f"promo_{'active' if task['is_active'] else 'done'}_page_1",
        icon_custom_emoji_id="5411126895620167819",
    ))
    kb = InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.regexp(r"promo_close_\d+"))
async def promo_close_prompt(call: types.CallbackQuery):
    """Показывает сумму возврата и просит подтвердить закрытие заказа."""
    await _ensure_promo_pause_schema()
    task_id = int(call.data.rsplit("_", 1)[1])
    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_tasks WHERE id=? AND created_by=? "
            "AND task_type IN ('channel', 'chat')",
            (task_id, call.from_user.id),
        ) as cursor:
            task = await cursor.fetchone()

    if not task or not task["is_active"] or task["is_closed"]:
        await call.answer("Это задание уже закрыто или недоступно.", show_alert=True)
        return

    remaining = max(0, int(task["max_completions"] or 0) - int(task["current_completions"] or 0))
    unit_price = max(0.0, float(task["price"] or 0))
    refund = round(remaining * unit_price, 6)
    text = (
        f"⛔ <b>Закрыть задание #{task_id}?</b>\n\n"
        f"Выполнено: <b>{task['current_completions']}/{task['max_completions']}</b>\n"
        f"Осталось выполнений: <b>{remaining}</b>\n"
        f"💰 Возврат на рекламный баланс: <b>{refund:.3f}$</b>\n\n"
        "После закрытия задание исчезнет из списка доступных исполнителям. "
        "Уже начисленные исполнителям награды не отменяются."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, закрыть и вернуть остаток", callback_data=f"promo_close_confirm_{task_id}")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"promo_info_{task_id}")],
    ])
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.regexp(r"promo_close_confirm_\d+"))
async def promo_close_confirm(call: types.CallbackQuery):
    """Атомарно закрывает активное задание и возвращает только неиспользованный остаток."""
    await _ensure_promo_pause_schema()
    task_id = int(call.data.rsplit("_", 1)[1])
    user_id = call.from_user.id

    try:
        async with aiosqlite.connect("bot_database.db") as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT * FROM user_tasks WHERE id=? AND created_by=? "
                "AND task_type IN ('channel', 'chat')",
                (task_id, user_id),
            ) as cursor:
                task = await cursor.fetchone()

            if not task or not task["is_active"] or task["is_closed"]:
                await db.rollback()
                await call.answer("Задание уже закрыто или недоступно.", show_alert=True)
                return

            max_count = max(0, int(task["max_completions"] or 0))
            completed_count = max(0, int(task["current_completions"] or 0))
            remaining = max(0, max_count - completed_count)
            unit_price = max(0.0, float(task["price"] or 0))
            refund = round(remaining * unit_price, 6)

            # Возвращаем деньги и закрываем заказ в одной транзакции.
            cursor = await db.execute(
                "UPDATE users SET ad_balance = COALESCE(ad_balance, 0) + ? WHERE user_id = ?",
                (refund, user_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                await call.answer("Не удалось найти рекламный баланс пользователя.", show_alert=True)
                return

            cursor = await db.execute(
                "UPDATE user_tasks SET is_active=0, is_paused=0, is_closed=1, refunded_amount=? "
                "WHERE id=? AND created_by=? AND is_active=1 AND COALESCE(is_closed, 0)=0",
                (refund, task_id, user_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                await call.answer("Задание уже было закрыто. Возврат не повторялся.", show_alert=True)
                return
            await db.commit()

        result_text = (
            f"✅ <b>Задание #{task_id} закрыто.</b>\n\n"
            f"Выполнено: <b>{completed_count}/{max_count}</b>\n"
            f"Возвращено на рекламный баланс: <b>{refund:.3f}$</b>\n"
            "Повторно вернуть средства за этот заказ нельзя."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📁 К завершённым заказам", callback_data="promo_done_page_1")
        ], [
            InlineKeyboardButton(text="🔙 В продвижение", callback_data="promo_back_menu")
        ]])
        await call.message.edit_text(result_text, parse_mode="HTML", reply_markup=kb)
        await call.answer("Задание закрыто, остаток возвращён.")
    except Exception as exc:
        print(f"❌ Ошибка закрытия рекламного задания #{task_id}: {exc}")
        await call.answer("Не удалось закрыть задание. Средства не должны были измениться.", show_alert=True)


# === Создание задания ===
@dp.callback_query(F.data == "promo_create")
async def promo_create_start(call: types.CallbackQuery, state: FSMContext):
    """Создание заданий только для каналов и чатов."""
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Канал", callback_data="promo_type_channel")],
        [InlineKeyboardButton(text="Чат", callback_data="promo_type_chat")],
        [InlineKeyboardButton(
            text="Отмена",
            callback_data="promo_back_menu",
            icon_custom_emoji_id="5411126895620167819",
        )]
    ])
    await call.message.answer(
        '<tg-emoji emoji-id="5413764443626576622">📋</tg-emoji> <b>Выберите тип задания:</b>\n\n'
        '<tg-emoji emoji-id="5893431652578758294">✅</tg-emoji> <b>Канал</b> — пользователи должны подписаться на канал\n'
        '<tg-emoji emoji-id="5893431652578758294">✅</tg-emoji> <b>Чат</b> — пользователи должны вступить в чат',
        parse_mode="HTML",
        reply_markup=kb
    )
    await call.answer()

@dp.callback_query(F.data == "promo_type_channel")
async def promo_type_channel(call: types.CallbackQuery, state: FSMContext):
    """Выбран тип «Канал»."""
    await state.update_data(task_type="channel")
    text = (
        '<tg-emoji emoji-id="5413822103562524971">📢</tg-emoji> '
        '<b>Отправьте ссылку на ваш канал</b>\n\n'
        '<tg-emoji emoji-id="5413436965255157438">👑</tg-emoji> '
        'Бот должен быть администратором канала.\n\n'
        '<blockquote>Перейди в настройки канала и добавь в администраторы — '
        '<a href="https://t.me/UAMONEY_BOT">@UAMONEY_BOT</a></blockquote>'
    )
    await call.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
    await state.set_state(PromotionStates.waiting_channel_link)
    await call.answer()

@dp.callback_query(F.data == "promo_type_chat")
async def promo_type_chat(call: types.CallbackQuery, state: FSMContext):
    """Выбран тип «Чат» (публичная группа/супергруппа)."""
    await state.update_data(task_type="chat")
    text = (
        '<tg-emoji emoji-id="5413822103562524971">💬</tg-emoji> '
        '<b>Отправьте ссылку на ваш чат</b>\n\n'
        '<tg-emoji emoji-id="5413822103562524971">👑</tg-emoji> '
        'Бот должен быть администратором чата.\n\n'
        '<blockquote>Перейди в настройки чата и добавь в администраторы — '
        '<a href="https://t.me/UAMONEY_BOT">@UAMONEY_BOT</a></blockquote>'
    )
    await call.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
    await state.set_state(PromotionStates.waiting_channel_link)
    await call.answer()




@dp.callback_query(F.data == "promo_type_bot")
async def promo_type_bot_removed(call: types.CallbackQuery):
    await call.answer("Создание заданий для ботов отключено. Доступны только каналы и чаты.", show_alert=True)


@dp.callback_query(F.data == "promo_confirm")
async def promo_confirm(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = call.from_user.id
    link = data.get("link")
    username = data.get("username")
    count = data.get("count")
    total = data.get("total")
    unit_price = float(data.get("unit_price", (float(total) / int(count)) if count else 0.015))
    task_type = data.get("task_type", "channel")

    # Запрещаем любые старые/устаревшие бот-задания до списания баланса.
    if task_type not in ("channel", "chat"):
        await state.clear()
        await call.answer("Создание заданий для ботов отключено. Доступны только каналы и чаты.", show_alert=True)
        return
    if not all((link, username, count, total)):
        await call.answer("Данные задания устарели. Создайте задание заново.", show_alert=True)
        return

    admin_status = await _promo_bot_admin_status(username)
    if admin_status is not True:
        if admin_status is False:
            await call.message.answer(
                _promo_admin_missing_text(task_type),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await call.answer()
        else:
            object_name = "канале" if task_type == "channel" else "чате"
            await call.answer(
                f"⚠️ Не удалось проверить права бота в {object_name}. Попробуйте ещё раз чуть позже.",
                show_alert=True,
            )
        return

    try:
        await _ensure_promo_pause_schema()
        async with aiosqlite.connect("bot_database.db") as db:
            cursor = await db.execute(
                "UPDATE users SET ad_balance = ad_balance - ? "
                "WHERE user_id = ? AND ad_balance >= ?",
                (total, user_id, total),
            )
            if cursor.rowcount == 0:
                await db.rollback()
                await call.answer("Недостаточно средств на рекламном балансе.", show_alert=True)
                return
            await db.execute(
                "INSERT INTO user_tasks "
                "(channel_link, channel_username, price, max_completions, created_by, task_type, is_paused) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (link, username, unit_price, count, user_id, task_type),
            )
            await db.commit()

        _ensure_promo_pause_monitor_started()
        success_text = (
            "✅ Задание на подписку на канал успешно создано!"
            if task_type == "channel"
            else "✅ Задание на вступление в чат успешно создано!"
        )
        await call.message.edit_text(
            f"{success_text}\n\nОно появится в разделе «📝 Задания №2».",
            parse_mode="HTML"
        )
        await state.clear()
        await notify_all_users_about_task()
    except Exception as exc:
        print(f"❌ Ошибка при создании задания: {exc}")
        await call.message.answer(f"⚠️ Ошибка при создании задания: {exc}", parse_mode='HTML')


@dp.callback_query(F.data == "promo_cancel")
async def promo_cancel(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await call.message.edit_text("❌ Создание задания отменено.")


@dp.message(F.text == "Задания №2")
async def show_user_tasks(message: types.Message):
    user_id = message.from_user.id
    _ensure_promo_pause_monitor_started()
    await _refresh_promo_task_pauses()

    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        all_tasks = await db.execute(
            "SELECT * FROM user_tasks "
            "WHERE is_active = TRUE AND task_type IN ('channel', 'chat') "
            "AND COALESCE(is_paused, 0) = 0 "
            "AND current_completions < max_completions "
            "AND id NOT IN (SELECT task_id FROM user_task_completions WHERE user_id = ?)",
            (user_id,),
        )
        all_tasks = await all_tasks.fetchall()

    reward = float(await get_setting("user_task_reward") or 0.015)
    channel_tasks = []
    chat_tasks = []
    for task in all_tasks:
        task_type = task["task_type"] if "task_type" in task.keys() else "channel"
        already_member = await _promo_target_is_member(user_id, task["channel_username"])
        if not already_member:
            if task_type == "chat":
                chat_tasks.append(task)
            elif task_type == "channel":
                channel_tasks.append(task)

    total_tasks = len(channel_tasks) + len(chat_tasks)
    if total_tasks == 0:
        no_tasks_text = (
            "<tg-emoji emoji-id='5411637614476305607'>⏳</tg-emoji> "
            "<b>Вы выполнили все доступные задания!</b>\n\n"
            "Пока нет новых, но они появляются каждый час. Заходите позже!\n\n"
            "<blockquote>"
            "•<a href='https://telegra.ph/U-menya-net-zadaniya-11-08'>Что делать если нет задания?</a>"
            "</blockquote>"
        )
        await message.answer(
            no_tasks_text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        return

    total_reward = total_tasks * reward
    main_keyboard = InlineKeyboardBuilder()
    for task in channel_tasks:
        main_keyboard.button(text="📢 Подписаться", url=normalize_tg_link(task["channel_link"]))
    for task in chat_tasks:
        main_keyboard.button(text="💬 Вступить в чат", url=normalize_tg_link(task["channel_link"]))

    main_keyboard.adjust(2)
    main_keyboard.row(InlineKeyboardButton(
        text="Проверить все задания",
        callback_data="check_user_tasks",
        icon_custom_emoji_id="5411444087544913716"
    ))
    text = (
        f'<tg-emoji emoji-id="5312161417372142817">📝</tg-emoji> '
        f'<b>Доступных заданий: {total_tasks}</b>\n'
        f'<tg-emoji emoji-id="5309795500277403547">💫</tg-emoji> '
        f'<b>Можно заработать: {total_reward:.3f}$</b>\n\n'
        f'<tg-emoji emoji-id="5411418403640484720">❗️</tg-emoji> '
        f'Перейдите по кнопкам к каналам и чатам, затем вернитесь сюда и нажмите «Проверить все задания»\n\n'
        f'<tg-emoji emoji-id="5411101937565214389">⚠️</tg-emoji> '
        f'Важно: если отписаться или выйти раньше чем через 24 часа, выплата будет списана'
    )
    await message.answer(text, reply_markup=main_keyboard.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "noop")
async def noop_handler(call: CallbackQuery):
    await call.answer()

# Обработчик проверки нескольких заданий
@dp.callback_query(F.data.startswith("check_tasks_"))
async def check_multiple_tasks(call: types.CallbackQuery):
    signatures = call.data.replace("check_tasks_", "").split('_')
    user_id = call.from_user.id

    if not core.flyer_enabled or not core.flyer:
        await call.answer("❌ Система проверки временно недоступна", show_alert=True)
        return

    try:
        completed_tasks = 0
        total_reward = 0

        tasks = await flyer_get_tasks(user_id, "ru", limit=20)
        task_prices = {
            task.get("signature"): float(task.get("price", 0) or 0)
            for task in tasks
            if task.get("signature")
        }

        for signature in signatures:
            status = await flyer_check_task(signature, user_id)

            if status not in [True, "completed", "done", "complete"]:
                continue

            saved_status = await get_task_status(user_id, signature)
            if saved_status in ["completed", "done", "complete", "waiting"]:
                continue

            reward = task_prices.get(signature, float(FLZAD))
            if reward <= 0:
                continue

            balance_ok = await update_user_balance(user_id, frozen_amount=reward)
            tasks_ok = await update_user_balance(user_id, completed_tasks=1)
            if not balance_ok or not tasks_ok:
                raise RuntimeError(f"Не удалось сохранить награду Flyer для {signature}")

            await add_frozen_transaction(user_id, reward, "core.flyer", signature)
            await set_task_status(user_id, signature, "completed")

            completed_tasks += 1
            total_reward += reward

        if completed_tasks > 0:
            if total_reward > 0:
                await call.answer(
                    f"✅ Проверка завершена!\n"
                    f"Выполнено заданий: {completed_tasks}\n"
                    f"Начислено: {total_reward:.4f}$", 
                    show_alert=True
                )
            else:
                await call.answer(
                    f"✅ Проверка завершена!\n"
                    f"Выполнено заданий: {completed_tasks}", 
                    show_alert=True
                )

            try:
                new_text = call.message.text + f"\n\n✅ <b>Проверено! Выполнено: {completed_tasks}</b>"
                await call.message.edit_text(new_text, parse_mode='HTML')
            except:
                pass
        else:
            await call.answer("Выполненных подписок не найдено.\nПодпишись через кнопки из списка и нажми проверку.", show_alert=True)

    except Exception as e:
        print(f"Ошибка проверки заданий: {e}")
        await call.answer("❌ Ошибка при проверке заданий", show_alert=True)

    await call.answer()

# Обработчик проверки одного задания (оставляем для не-канальных заданий)
@dp.callback_query(F.data.startswith("check_task_"))
async def check_task_completion(call: types.CallbackQuery):
    signature = call.data.replace("check_task_", "")
    user_id = call.from_user.id

    try:
        if core.flyer_enabled and core.flyer:
            status = await flyer_check_task(signature, user_id)
            print(f"🔍 Результат проверки задания {signature}: {status}")

            if status in [True, "completed", "done", "complete", "waiting"]:
                reward = float(FLZAD)

                # Проверяем старый статус ДО записи нового результата.
                saved_status = await get_task_status(user_id, signature)
                if saved_status not in ["completed", "done", "complete", "waiting"]:
                    balance_ok = await update_user_balance(user_id, frozen_amount=reward)
                    tasks_ok = await update_user_balance(user_id, completed_tasks=1)
                    if not balance_ok or not tasks_ok:
                        raise RuntimeError("Не удалось сохранить начисление Flyer в базе данных")

                    await add_frozen_transaction(user_id, reward, "core.flyer", signature)
                    await set_task_status(user_id, signature, "completed")

                    if status == "waiting":
                        message_text = f"✅ Задание принято на проверку! Награда {reward:.3f}$ заморожена."
                    else:
                        message_text = f"✅ Задание выполнено! Награда {reward:.3f}$ заморожена на 24 часа."

                    await call.answer(message_text, show_alert=True)

                    # УДАЛЯЕМ сообщение с заданием
                    try:
                        await call.message.delete()
                    except Exception as e:
                        print(f"Ошибка при удалении сообщения: {e}")
                else:
                    await call.answer("✅ Задание уже проверено!", show_alert=True)
                    try:
                        await call.message.delete()
                    except Exception as e:
                        print(f"Ошибка при удалении сообщения: {e}")

            elif status in [False, "incomplete"]:
                await call.answer("❌ Задание еще не выполнено.", show_alert=True)

            else:
                await call.answer(f"📊 Статус: {status}", show_alert=True)

        else:
            await call.answer("❌ Система проверки недоступна", show_alert=True)

    except Exception as e:
        print(f"❌ Ошибка проверки задания: {e}")
        await call.answer("❌ Ошибка при проверке", show_alert=True)

    await call.answer()

@dp.callback_query(F.data == "ad_topup")
async def ad_topup_start(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.answer(
        "<tg-emoji emoji-id='5456348576214983692'>💰</tg-emoji> "
        "Введите сумму в $ для пополнения рекламного баланса:",
        parse_mode="HTML"
    )
    await state.set_state(AdTopupStates.waiting_for_amount)


@dp.message(AdTopupStates.waiting_for_amount)
async def ad_topup_process(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            await message.answer("❌ Сумма должна быть больше 0")
            return
        if amount < 0.1:
            await message.answer("❌ Минимальная сумма пополнения — 0.1 $")
            return

        invoice_url = await create_ad_invoice(message.from_user.id, amount)
        if invoice_url:
            await message.answer(
                f"💳 <b>ИНВОЙС ДЛЯ ПОПОЛНЕНИЯ</b>\n\n"
                f"💰 Сумма: <b>{amount:.2f} $</b>\n"
                f"📎 Ссылка для оплаты: {invoice_url}\n\n"
                f"После оплаты нажмите кнопку ниже:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data="confirm_ad_payment")]
                ]),
                parse_mode='HTML'
            )
        else:
            await message.answer("❌ Ошибка при создании инвойса. Попробуйте позже.")
    except ValueError:
        await message.answer("❌ Введите корректную сумму.")
    await state.clear()


@dp.callback_query(F.data == "confirm_ad_payment")
async def confirm_ad_payment(call: types.CallbackQuery):
    user_id = call.from_user.id

    # 🔍 Беремо тільки останній НЕоплачений інвойс (pending)
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute("""
            SELECT invoice_id 
            FROM ad_topups 
            WHERE user_id = ? AND status = 'pending'
            ORDER BY id DESC LIMIT 1
        """, (user_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            await call.answer("❌ У вас нет активных неоплаченных инвойсов", show_alert=True)
            return

        invoice_id = row[0]

    # 🧾 Перевіряємо оплату через CryptoBot API
    if not invoice_id:
        await call.answer("⚠️ Ошибка: пустой ID платежа", show_alert=True)
        return

    result = await check_ad_invoice(invoice_id)

    # ✅ Результат перевірки
    if result == "already_paid":
        await call.answer("⚠️ Этот платёж уже был зачислен ранее", show_alert=True)
    elif result is True:
        await call.answer("✅ Оплата подтверждена, баланс пополнен!", show_alert=True)
    elif result is False:
        await call.answer("❌ Оплата не найдена, попробуйте позже", show_alert=True)
    else:
        await call.answer(f"⚠️ Неизвестный ответ от системы: {result}", show_alert=True)



def referrals_menu_keyboard():
    """Кнопки основного экрана реферальной программы."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Мои рефералы", callback_data="my_referrals_latest", icon_custom_emoji_id="5411350246804461567")],
        [InlineKeyboardButton(
            text="Лидерборд",
            callback_data="open_leaderboard",
            icon_custom_emoji_id="5211137245335034133",
        )],
    ])


@dp.message(F.text == "Рефералы")
async def show_referrals(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    text = await build_referrals_text(user_id)
    await message.answer(text, parse_mode="HTML", reply_markup=referrals_menu_keyboard())


async def build_my_referrals_text(user_id: int, mode: str = "latest") -> str:
    """Список последних или лучших приглашённых рефералов первого уровня."""
    required_tasks = int(await get_setting("ref_tasks_required_level1") or 3)
    order_by = (
        "COALESCE(completed_tasks, 0) DESC, registered_at DESC, user_id DESC"
        if mode == "best"
        else "registered_at DESC, user_id DESC"
    )

    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute(
            f"""
            SELECT user_id, username, COALESCE(completed_tasks, 0) AS completed_tasks
            FROM users
            WHERE referrer_id = ?
            ORDER BY {order_by}
            LIMIT 10
            """,
            (user_id,),
        ) as cursor:
            referrals = await cursor.fetchall()

    title = "Лучшие рефералы" if mode == "best" else "Последние рефералы"
    text = (
        "<tg-emoji emoji-id='5413740297320440148'>👤</tg-emoji> <b>Имя</b> | "
        "<tg-emoji emoji-id='5413764443626576622'>📝</tg-emoji> <b>Выполненные задания</b>\n"
        f"<b>({title})</b>\n\n"
    )

    if not referrals:
        text += "Пока нет приглашённых рефералов. Поделитесь своей ссылкой, чтобы они появились здесь."
        return text

    rows = []
    for index, referral in enumerate(referrals, 1):
        referred_id, username, completed_tasks = referral
        display_name = str(username or f"Пользователь {referred_id}").strip()
        clean_username = display_name.lstrip("@")

        # Если значение похоже на Telegram username, делаем имя ссылкой;
        # иначе оставляем его обычным текстом, как в заданном макете.
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", clean_username):
            name_html = f"<a href='https://t.me/{html.escape(clean_username, quote=True)}'>@{html.escape(clean_username)}</a>"
        else:
            name_html = html.escape(display_name)

        is_completed = int(completed_tasks or 0) >= required_tasks
        status_emoji_id = (
            "5260416304224936047" if is_completed
            else "5260342697075416641"
        )
        # Telegram требует, чтобы fallback внутри tg-emoji был одним emoji,
        # а не символом-пулей (●), иначе возвращается ENTITY_TEXT_INVALID.
        status_fallback = "✅" if is_completed else "❌"
        rows.append(
            f"{index} - {name_html} | {int(completed_tasks or 0)} "
            f"<tg-emoji emoji-id='{status_emoji_id}'>{status_fallback}</tg-emoji>"
        )

    # Пользователь просил оформить нумерованный список цитатой.
    text += "<blockquote>" + "\n".join(rows) + "</blockquote>"
    return text


def my_referrals_keyboard(current: str = "latest"):
    latest_text = "Последние рефералы" + (" ✓" if current == "latest" else "")
    best_text = "Лучшие рефералы" + (" ✓" if current == "best" else "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=latest_text, callback_data="my_referrals_latest"),
            InlineKeyboardButton(text=best_text, callback_data="my_referrals_best"),
        ],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_ref_menu")],
    ])


@dp.callback_query(F.data.in_({"my_referrals_latest", "my_referrals_best"}))
async def show_my_referrals(call: types.CallbackQuery):
    # Сразу закрываем индикатор нажатия, до работы с базой и редактирования.
    try:
        await call.answer()
    except Exception as answer_error:
        # Сетевой сбой/устаревший callback не должен мешать открыть список.
        print(f"⚠️ Не удалось подтвердить callback списка рефералов: {answer_error}")

    mode = "best" if call.data == "my_referrals_best" else "latest"
    text = await build_my_referrals_text(call.from_user.id, mode)
    try:
        await call.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=my_referrals_keyboard(mode),
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as edit_error:
        # Нажатие уже выбранной вкладки не меняет текст и клавиатуру — это штатно.
        if "message is not modified" not in str(edit_error).lower():
            raise


async def build_referrals_text(user_id: int) -> str:
    ref_data = await get_referral_data(user_id)
    bonus_lvl1 = float(await get_setting('ref_bonus_level1') or 0.05)
    bonus_lvl2 = float(await get_setting('ref_bonus_level2') or 0.03)
    tasks_lvl1 = int(await get_setting('ref_tasks_required_level1') or 3)
    tasks_lvl2 = int(await get_setting('ref_tasks_required_level2') or 5)
    ref_link = f"https://t.me/{(await bot.get_me()).username}?start={user_id}"

    return (
        "<tg-emoji emoji-id='5413844214054166858'>🎯</tg-emoji><b>Реферальная система</b>\n\n"
        "<blockquote>"
        f"<tg-emoji emoji-id='5411350246804461567'>👥</tg-emoji><b>Ваши рефералы:</b>\n"
        f"├ 1 ур. — <b>{ref_data['level1_count']}</b>\n"
        f"╰ 2 ур. — <b>{ref_data.get('level2_count', 0)}</b>"
        "</blockquote>\n\n"
        "<blockquote>"
        f"<tg-emoji emoji-id='5456348576214983692'>💰</tg-emoji><b>Бонусы:</b>\n"
        f"├ 1 ур. — <b>{bonus_lvl1:.2f}$</b> за <b>{tasks_lvl1}</b> заданий реферала\n"
        f"╰ 2 ур. — <b>{bonus_lvl2:.2f}$</b> за <b>{tasks_lvl2}</b> заданий"
        "</blockquote>\n\n"
        f"<tg-emoji emoji-id='5413530342139143524'>🔗</tg-emoji><b>Ваша ссылка:</b>\n<code>{ref_link}</code>"
    )


def leaderboard_keyboard(current: str = "home"):
    # На странице любого рейтинга показываем ТОЛЬКО возврат к списку рейтингов.
    if current != "home":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔙 Назад к лидерборду",
                callback_data="open_leaderboard"
            )]
        ])

    # На главной странице лидерборда — три рейтинга и возврат к рефералам.
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Топ заданий", callback_data="leaderboard_tasks",
                icon_custom_emoji_id="5361767787129766930"
            ),
            InlineKeyboardButton(
                text="Топ рефоводов", callback_data="leaderboard_referrals",
                icon_custom_emoji_id="5361767787129766930"
            ),
        ],
        [InlineKeyboardButton(
            text="Топ богачей", callback_data="leaderboard_rich",
            icon_custom_emoji_id="5361767787129766930"
        )],
        [InlineKeyboardButton(
            text="Назад", callback_data="back_to_ref_menu",
            icon_custom_emoji_id="5413728889887298622"
        )],
    ])


@dp.callback_query(F.data == "open_leaderboard")
async def open_leaderboard(call: types.CallbackQuery):
    text = (
        "<tg-emoji emoji-id='5211137245335034133'>👑</tg-emoji>"
        "<b>Лидерборд всех пользователей проекта</b>\n\n"
        "Выберите рейтинг:"
    )

    await call.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=leaderboard_keyboard()
    )

    await call.answer()

@dp.callback_query(F.data.in_({"leaderboard_tasks", "leaderboard_referrals", "leaderboard_rich"}))
async def show_leaderboard(call: types.CallbackQuery):
    mode = call.data.removeprefix("leaderboard_")

    # Администратор не участвует в рейтингах (ни в списке, ни в расчёте места).
    excluded_ids = tuple(int(x) for x in getattr(core, "ADMIN_IDS", []))
    excluded_sql = f"({', '.join('?' for _ in excluded_ids)})" if excluded_ids else "(0)"

    # Замени ID_... на числовые ID своих Premium Emoji.
    premium_emoji_ids = {
        "tasks_title": "5361767787129766930",
        "referrals_title": "5361767787129766930",
        "rich_title": "5361767787129766930",
        "personal_place": "5411395283831527574",
        "empty": "5411395283831527574",
        1: "5188307557126524632",
        2: "5188417224821460749",
        3: "5188480700143129440",
        4: "5188179579986005169",
        5: "5188175671565764828",
        6: "5188594168884119951",
        7: "5188594168884119951",
        8: "5188377912985799970",
        9: "5188152972663608028",
        "10_first": "5188307557126524632",
        "10_second": "",
    }

    def premium_emoji(key, fallback):
        # ID-заглушки и пустые/нечисловые значения не отправляем Telegram.
        emoji_id = str(premium_emoji_ids.get(key, "")).strip()
        if not emoji_id.isdigit():
            return fallback
        return f"<tg-emoji emoji-id='{emoji_id}'>{fallback}</tg-emoji>"

    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row

        if mode == "tasks":
            title = f"{premium_emoji('tasks_title', '📝')}<b>Топ заданий</b>"
            query = f"""
                SELECT user_id, username, COALESCE(completed_tasks, 0) AS score
                FROM users
                WHERE COALESCE(completed_tasks, 0) > 0
                  AND user_id NOT IN {excluded_sql}
                ORDER BY score DESC, user_id ASC
                LIMIT 10
            """
            scores_query = f"""
                SELECT user_id, COALESCE(completed_tasks, 0) AS score
                FROM users
                WHERE user_id NOT IN {excluded_sql}
            """
            unit = "зад."

        elif mode == "referrals":
            title = f"{premium_emoji('referrals_title', '🏆')}<b>Топ Рефеводов</b>"
            query = f"""
                SELECT u.user_id, u.username, COUNT(r.user_id) AS score
                FROM users u
                LEFT JOIN users r ON r.referrer_id = u.user_id
                WHERE u.user_id NOT IN {excluded_sql}
                GROUP BY u.user_id, u.username
                HAVING COUNT(r.user_id) > 0
                ORDER BY score DESC, u.user_id ASC
                LIMIT 10
            """
            scores_query = f"""
                SELECT u.user_id, COUNT(r.user_id) AS score
                FROM users u
                LEFT JOIN users r ON r.referrer_id = u.user_id
                WHERE u.user_id NOT IN {excluded_sql}
                GROUP BY u.user_id
            """
            unit = "реф"

        else:
            title = f"{premium_emoji('rich_title', '💰')}<b>Топ богачей</b>"
            query = f"""
                SELECT user_id, username, COALESCE(lifetime_earned, 0) AS score
                FROM users
                WHERE COALESCE(lifetime_earned, 0) > 0
                  AND user_id NOT IN {excluded_sql}
                ORDER BY score DESC, user_id ASC
                LIMIT 10
            """
            scores_query = f"""
                SELECT user_id, COALESCE(lifetime_earned, 0) AS score
                FROM users
                WHERE user_id NOT IN {excluded_sql}
            """
            unit = "$"

        rows = await db.execute_fetchall(query, excluded_ids)

        # Место считается среди всех пользователей, в том числе с нулевым результатом.
        rank_query = f"""
            WITH scores AS (
                {scores_query}
            ),
            ranked AS (
                SELECT user_id,
                       ROW_NUMBER() OVER (ORDER BY score DESC, user_id ASC) AS place
                FROM scores
            )
            SELECT place FROM ranked WHERE user_id = ?
        """
        async with db.execute(rank_query, (*excluded_ids, call.from_user.id)) as cursor:
            rank_row = await cursor.fetchone()

    user_rank = rank_row["place"] if rank_row else None

    lines = [title, "━━━━━━━━━━━━━━━", ""]
    if rows:
        for place, row in enumerate(rows, start=1):
            user_id = int(row["user_id"])
            raw_name = str(row["username"] or "Пользователь").strip()
            if raw_name and " " not in raw_name and not raw_name.startswith("user_"):
                display_name = f"@{html.escape(raw_name.lstrip('@'))}"
            else:
                display_name = (
                    f"<a href='tg://user?id={user_id}'>"
                    f"{html.escape(raw_name or 'Пользователь')}</a>"
                )

            score = float(row["score"] or 0) if mode == "rich" else int(row["score"] or 0)
            score_text = f"{score:.3f}$" if mode == "rich" else f"{score} {unit}"
            if place == 10:
                place_emoji = (
                    premium_emoji("10_first", "1️⃣")
                    + premium_emoji("10_second", "0️⃣")
                )
            else:
                place_emoji = premium_emoji(place, f"{place}️⃣")
            lines.append(f"{place_emoji} {display_name} — <b>{score_text}</b>")
    else:
        lines.append(f"{premium_emoji('empty', '😕')} Пока нет данных для этого рейтинга.")

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━",
        (
            f"{premium_emoji('personal_place', '📊')} Вы на {user_rank} месте"
            if user_rank is not None
            else f"{premium_emoji('personal_place', '📊')} Вы пока не в рейтинге"
        )
    ])
    text = "\n".join(lines)

    try:
        await call.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=leaderboard_keyboard(mode)
        )
    except Exception as e:
        # Если Telegram отклонил один из числовых custom emoji ID,
        # повторяем отправку с обычными emoji вместо Premium-разметки.
        if "Invalid custom emoji identifier" not in str(e):
            raise
        safe_text = re.sub(
            r"<tg-emoji\s+emoji-id=['\"][^'\"]+['\"]>(.*?)</tg-emoji>",
            r"\1",
            text,
            flags=re.DOTALL
        )
        await call.message.edit_text(
            safe_text,
            parse_mode="HTML",
            reply_markup=leaderboard_keyboard(mode)
        )
    await call.answer()


@dp.callback_query(F.data == "back_to_ref_menu")
async def back_to_ref_menu(call: types.CallbackQuery):
    text = await build_referrals_text(call.from_user.id)
    try:
        await call.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=referrals_menu_keyboard(),
        )
    except TelegramBadRequest:
        await call.message.edit_caption(
            caption=text,
            parse_mode="HTML",
            reply_markup=referrals_menu_keyboard(),
        )
    await call.answer()


# ✅ Callback-кнопки
# ✅ URL-кнопки (добавляем только если URL не пустой)
# Кнопка главного меню «Инфо» — это ReplyKeyboard, поэтому нужен Message-хендлер.
# Оставляем оба варианта подписи для совместимости со старой клавиатурой.
@dp.message(F.text.in_({"Инфо", "📚 Инфо"}))
async def info_menu_message(message: types.Message):
    """Открывает исходную карточку статистики с inline-разделами."""
    user_id = message.from_user.id
    if await check_user_blocked(user_id):
        await message.answer("❌ Ваш аккаунт заблокирован. Обратитесь к администратору.")
        return

    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total_users = int((await cursor.fetchone())[0] or 0)
        async with db.execute("SELECT COALESCE(SUM(completed_tasks), 0) FROM users") as cursor:
            total_tasks = int((await cursor.fetchone())[0] or 0)
        async with db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE status = 'approved'"
        ) as cursor:
            total_withdrawn = float((await cursor.fetchone())[0] or 0)
        async with db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM ad_topups WHERE status = 'paid'"
        ) as cursor:
            total_ad_topups = float((await cursor.fetchone())[0] or 0)

    photo_path = core.render_stats_image(
        total_users, 0, total_tasks, total_withdrawn, 0, total_ad_topups
    )
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Чат", url="https://t.me/UAMONEYCHAT", icon_custom_emoji_id="5312103894875143512")
    keyboard.button(text="Канал", url="https://t.me/UAMONY", icon_custom_emoji_id="5312161417372142817")
    # Ссылки на чат и канал располагаются в одном ряду.
    keyboard.adjust(2)

    await message.answer_photo(
        FSInputFile(photo_path),
        caption=(
            '<tg-emoji emoji-id="5411395283831527574">📊</tg-emoji> '
            'Статистика обновляется в реальном времени\n\n'
            "<tg-emoji emoji-id='5411418403640484720'>⚙️</tg-emoji> Руководство пользователя (Частые вопросы)\n"
            "<blockquote>"
            "•<a href='https://telegra.ph/Skolko-ya-mogu-zarabot-11-08'>Сколько я могу заработать?</a>\n"
            "•<a href='https://telegra.ph/Kak-vyvesti-sredstva-11-08'>Как вывести средства?</a>\n"
            "•<a href='https://telegra.ph/U-menya-net-zadaniya-11-08'>У меня нет задания</a>\n"
            "</blockquote>"
        ),
        reply_markup=keyboard.as_markup(),
        parse_mode="HTML",
    )


@dp.message(F.text == "🔙 Главное меню")
async def back_to_main(message: types.Message):
    await message.answer("Возвращаемся в главное меню:", reply_markup=get_main_keyboard(message.from_user.id))


@dp.callback_query(F.data.startswith("all_users"))
async def show_all_users(callback: types.CallbackQuery):
    await callback.answer()
    _, page, sort_field, sort_dir = callback.data.split(":")
    page = int(page)

    # Загружаем пользователей
    users = await get_all_users()  # должен возвращать список (user_id, ...)
    users_data = []

    for user in users:
        data = await get_user(user[0])
        users_data.append({
            "id": user[0],
            "balance": data["balance"],
            "tasks": data["completed_tasks"]
        })

    # Сортировка
    reverse = (sort_dir == "desc")
    users_data.sort(key=lambda x: x[sort_field], reverse=reverse)

    # Пагинация
    start = page * USERS_PER_PAGE
    end = start + USERS_PER_PAGE
    page_users = users_data[start:end]
    users = await get_all_users()
    total_users = len(users)
    text = f"📋 <b>Пользователи: <b>{total_users}</b></b>\n"
    text += f"Страница <b>{page + 1}</b>/<b>{(len(users_data) - 1) // USERS_PER_PAGE + 1}</b>\n"
    text += f"Фильтр: <b>{'📝 Задания' if sort_field == 'tasks' else '💰 Баланс'}</b> ({'⇧' if not reverse else '⇩'})\n\n"

    for u in page_users:
        text += (
            f"👤 <code>{u['id']}</code> | 💰 {u['balance']:.2f}$ | 📝 {u['tasks']}\n"
            f"────────────────\n"
        )

    # Кнопки
    keyboard = InlineKeyboardBuilder()

    # ✅ Переключатель фильтра (Задания / Баланс)
    keyboard.button(
        text="📝 Задания" if sort_field != "tasks" else "💰 Баланс",
        callback_data=f"all_users:{page}:{'balance' if sort_field=='tasks' else 'tasks'}:{sort_dir}"
    )

    # ✅ Переключение направления сортировки
    keyboard.button(
        text="⇳ Сортировка",
        callback_data=f"all_users:{page}:{sort_field}:{'asc' if sort_dir == 'desc' else 'desc'}"
    )

    keyboard.adjust(2)

    # ✅ Пагинация
    if start > 0:
        keyboard.button(
            text="⇦",
            callback_data=f"all_users:{page - 1}:{sort_field}:{sort_dir}"
        )
    if end < len(users_data):
        keyboard.button(
            text="⇨",
            callback_data=f"all_users:{page + 1}:{sort_field}:{sort_dir}"
        )

    keyboard.adjust(2)

    await callback.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "edit_user_task_reward")
async def edit_user_task_reward(call: CallbackQuery, state: FSMContext):
    await call.message.answer(
        f"💸 <b>Текущая выплата за пользовательское задание:</b>\n\n"
        f"Введите новое значение:",
        parse_mode="HTML"
    )
    await state.set_state(EditUserTaskReward.waiting_for_new_value)
    await call.answer()


@dp.message(EditUserTaskReward.waiting_for_new_value)
async def process_new_user_task_reward(message: Message, state: FSMContext):
    try:
        new_value = float(message.text.strip())
        if new_value <= 0:
            await message.answer("❌ Сумма должна быть больше 0.")
            return

        await set_setting("user_task_reward", str(new_value))
        await message.answer(f"✅ Выплата за юзер-задание успешно изменена на <b>{new_value}$</b>.", parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите корректное число (например: 0.02).")
        await state.clear()

@dp.callback_query(F.data.startswith("cleanopt:"))
async def clean_userdb_confirm(call: CallbackQuery):
    _, action, user_id = call.data.split(":")
    user_id = int(user_id)

    if action == "cancel":
        await call.answer("Отменено")
        await call.message.delete()
        return

    # Запитуємо підтвердження
    kb = InlineKeyboardBuilder()
    kb.button(text="✔ Подтвердить", callback_data=f"cleanrun:{action}:{user_id}")
    kb.button(text="✖ Отмена", callback_data="cleanrun:cancel:0")
    kb.adjust(1)

    await call.message.edit_text(
        f"❗ Вы уверены, что хотите выполнить действие:\n<b>{action}</b>?",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "broadcast_clear_buttons")
async def broadcast_clear_buttons(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(broadcast_buttons=[])

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="➕ Добавить кнопку", callback_data="broadcast_add_button")
    keyboard.button(text="🚀 Начать рассылку", callback_data="broadcast_start")
    keyboard.button(text="❌ Отмена", callback_data="broadcast_cancel")
    keyboard.adjust(1)

    await call.message.edit_text(
        "✅ <b>Все кнопки очищены!</b>\n\n"
        "Теперь вы можете:\n"
        "• Добавить новые кнопки\n"
        "• Начать рассылку без кнопок",
        reply_markup=keyboard.as_markup(),
        parse_mode='HTML'
    )
    await call.answer("Кнопки очищены")

@dp.callback_query(F.data == "broadcast_start")
async def broadcast_start(call: CallbackQuery, state: FSMContext):
    await call.answer()

    data = await state.get_data()
    text = data.get('broadcast_text')
    media = data.get('broadcast_media')
    media_type = data.get('broadcast_media_type')
    buttons = data.get('broadcast_buttons', [])
    preview_message_id = data.get('preview_message_id')

    if not text and not media:
        await call.answer("❌ Нет контента для рассылки", show_alert=True)
        return

    # Создаем клавиатуру если есть кнопки
    reply_markup = None
    if buttons:
        keyboard = InlineKeyboardBuilder()
        for btn in buttons:
            keyboard.button(text=btn['text'], url=btn['url'])
        keyboard.adjust(1)
        reply_markup = keyboard.as_markup()

    # Получаем всех пользователей
    users = await get_all_users()
    total_users = len(users)

    # ОТПРАВЛЯЕМ НОВОЕ сообщение вместо редактирования старого
    status_message = await call.message.answer(
        f"🚀 <b>Начинаем рассылку...</b>\n\n👥 Получателей: {total_users}", 
        parse_mode='HTML'
    )

    success_count = 0
    fail_count = 0
    blocked_count = 0

    # Обновляем статус каждые 50 пользователей
    update_interval = 50

    for i, (user_id, _) in enumerate(users):
        try:
            if media_type == 'photo' and media:
                await bot.send_photo(
                    chat_id=user_id,
                    photo=media,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
            elif media_type == 'video' and media:
                await bot.send_video(
                    chat_id=user_id,
                    video=media,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
            elif media_type == 'animation' and media:
                await bot.send_animation(
                    chat_id=user_id,
                    animation=media,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
            elif media_type == 'document' and media:
                await bot.send_document(
                    chat_id=user_id,
                    document=media,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
            else:
                await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )

            success_count += 1

            # Обновляем статус каждые update_interval пользователей
            if (i + 1) % update_interval == 0 or (i + 1) == total_users:
                try:
                    await status_message.edit_text(
                        f"🚀 <b>Рассылка в процессе...</b>\n\n"
                        f"📊 Прогресс: {i + 1}/{total_users}\n"
                        f"✅ Успешно: {success_count}\n"
                        f"❌ Ошибок: {fail_count}\n"
                        f"🚫 Заблокировали: {blocked_count}",
                        parse_mode='HTML'
                    )
                except:
                    pass  # Игнорируем ошибки редактирования

            # Небольшая задержка чтобы не спамить
            await asyncio.sleep(0.05)

        except aiogram.exceptions.TelegramForbiddenError:
            # Пользователь заблокировал бота
            blocked_count += 1
        except Exception as e:
            fail_count += 1
            print(f"❌ Ошибка отправки пользователю {user_id}: {e}")

    # Итоговое сообщение
    result_text = (
        f"📊 <b>Рассылка завершена!</b>\n\n"
        f"👥 Всего получателей: {total_users}\n"
        f"✅ Успешно отправлено: {success_count}\n"
        f"❌ Ошибок отправки: {fail_count}\n"
        f"🚫 Заблокировали бота: {blocked_count}\n\n"
        f"📈 Процент доставки: {(success_count / total_users * 100):.1f}%"
    )

    try:
        await status_message.edit_text(result_text, parse_mode='HTML')
    except:
        # Если не удалось отредактировать, отправляем новое сообщение
        await call.message.answer(result_text, parse_mode='HTML')

    # Удаляем превью-сообщение если оно было
    if preview_message_id:
        try:
            await bot.delete_message(chat_id=call.message.chat.id, message_id=preview_message_id)
        except:
            pass

    await state.clear()


@dp.callback_query(F.data == "broadcast_cancel")
async def broadcast_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Рассылка отменена")
    await call.answer()


# ===== handlers_withdrawals.py =====
# -*- coding: utf-8 -*-
"""Заявки на вывод: пользователь и админ-подтверждение."""




class FSInputFile(_AiogramFSInputFile):
    """Stream a local file in chunks without blocking the event loop."""

    async def read(self, bot):
        with open(self.path, "rb") as file:
            while True:
                chunk = await asyncio.to_thread(file.read, self.chunk_size)
                if not chunk:
                    break
                yield chunk

@dp.message(F.text == "🧾 Заявки на вывод")
async def show_withdrawal_menu(message: types.Message):
    pending = await count_withdrawals_by_status("pending")
    approved = await count_withdrawals_by_status("approved")
    declined = await count_withdrawals_by_status("declined")

    kb = InlineKeyboardBuilder()
    kb.button(text=f"🕓 Ожидающие [{pending}]", callback_data="wd_list_pending")
    kb.button(text=f"✅ Подтвержд. [{approved}]", callback_data="wd_list_approved")
    kb.button(text=f"❌ Без возврата [{declined}]", callback_data="wd_list_declined")
    kb.adjust(2, 2)

    await message.answer(
        "<b>Выберите категорию заявок:</b>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("wd_list_"))
async def show_withdrawal_list(call: CallbackQuery):
    parts = call.data.split("_")

    # wd_list_pending_1
    status = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 1

    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT w.*, u.username
            FROM withdrawals w
            LEFT JOIN users u ON w.user_id = u.user_id
            WHERE w.status = ?
            ORDER BY w.created_at DESC
        """, (status,)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await call.answer("Пусто.", show_alert=True)
        return

    # --- ПАГІНАЦІЯ ---
    pagination = paginate_list(rows, page)
    chunk = pagination["items"]

    kb = InlineKeyboardBuilder()

    for row in chunk:
        wid = row["id"]
        amount = row["amount"]
        username = row["username"] or "Без имени"
        kb.button(text=f"{username} [{amount}$]", callback_data=f"withdraw_view_{wid}")

    kb.adjust(2)

    # Навігація сторінок
    nav = InlineKeyboardBuilder()

    if pagination["page"] > 1:
        nav.button(
            text="◀️",
            callback_data=f"wd_list_{status}_{pagination['page'] - 1}"
        )

    nav.button(
        text=f"📄 {pagination['page']}/{pagination['total_pages']}",
        callback_data="noop"
    )

    if pagination["page"] < pagination["total_pages"]:
        nav.button(
            text="▶️",
            callback_data=f"wd_list_{status}_{pagination['page'] + 1}"
        )

    nav.adjust(3)

    back_kb = InlineKeyboardBuilder()
    back_kb.button(text="🔙 Назад", callback_data="wd_back_menu")
    back_kb.adjust(1)

    await call.message.edit_text(
        f"<b>Заявки ({status}) — страница {pagination['page']}/{pagination['total_pages']}:</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=kb.export() + nav.export() + back_kb.export()
        ),
        parse_mode="HTML"
    )

# Кнопка назад
@dp.callback_query(F.data == "wd_back_menu")
async def wd_back_menu(call: CallbackQuery):
    pending = await count_withdrawals_by_status("pending")
    approved = await count_withdrawals_by_status("approved")
    declined = await count_withdrawals_by_status("declined")

    kb = InlineKeyboardBuilder()
    kb.button(text=f"🕓 Ожидающие [{pending}]", callback_data="wd_list_pending")
    kb.button(text=f"✅ Подтвержд. [{approved}]", callback_data="wd_list_approved")
    kb.button(text=f"❌ Без возврата [{declined}]", callback_data="wd_list_declined")
    kb.adjust(2, 2)

    await call.message.edit_text(
        "<b>Выберите категорию заявок:</b>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("withdraw_view_"))
async def view_withdraw_request(call: CallbackQuery):
    wid = int(call.data.split("_")[-1])
    wd = await get_withdrawal(wid)

    if not wd:
        await call.answer("❌ Заявка не найдена.", show_alert=True)
        return

    # Если pending — переиспользуем существующий обработчик
    if wd["status"] == "pending":
        await open_withdraw_request(call)
        return

    user = await get_user(wd["user_id"])

    # 🔥 Конвертация даты в МСК
    created_at = datetime.fromisoformat(wd["created_at"]) + timedelta(hours=3)
    created_at = created_at.strftime("%Y-%m-%d %H:%M:%S")

    text = f"""
📄 <b>Заявка #{wid}</b>

🆔 User ID: <code>{user['user_id']}</code>
👤 Username: @{user['username'] or 'нет'}

💵 Сумма: <b>{wd['amount']}$</b>
📅 Создано: <i>{created_at}</i>
⏳ Статус: <b>{wd['status']}</b>
"""

    # 🔥 Тут потрібно створити клавіатуру ДО додавання кнопок
    kb = InlineKeyboardBuilder()

    # Кнопка чека (якщо є)
    if wd["admin_receipt_url"]:
        kb.button(text="📎 Чек", url=wd["admin_receipt_url"])
    else:
        kb.button(text="❌ НЕ СОЗДАН", callback_data="noop")

    # Кнопка "О пользователе"
    kb.button(text="👤 О пользователе", callback_data=f"wd_userinfo:{wd['user_id']}")

    kb.adjust(1)

    await call.message.edit_text(
        text,
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("withdraw_open_"))
async def open_withdraw_request(call: types.CallbackQuery):
    wid = int(call.data.split("_")[-1])
    wd = await get_withdrawal(wid)

    if not wd:
        await call.answer("❌ Заявка не найдена.", show_alert=True)
        return

    user = await get_user(wd["user_id"])

    # Получаем резерв
    total_balance = await get_crypto_bot_balance()
    reserve_status, reserve_emoji, rec = await get_reserve_status(total_balance)

    # === КНОПКИ ТОЧНО КАК В send_withdraw_request_to_admins ===
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"wd_confirm:{wid}")
    kb.button(text="❌ Отклонить (без возврата)", callback_data=f"wd_norefund:{wid}")
    kb.button(text="👤 О пользователе", callback_data=f"wd_userinfo:{wd['user_id']}")
    kb.adjust(1, 1, 1)

    # === СООБЩЕНИЕ — ТОЧНО КАК ПРИ УВЕДОМЛЕНИИ АДМИНОВ ===
    text = f"""
📨 <b>ЗАЯВКА НА ВЫВОД</b>

🆔 User ID: <code>{user['user_id']}</code>
👤 Username: @{user['username'] or 'нет'}

💵 Сумма вывода: <b>{wd['amount']}$</b>
🔖 ID заявки: <code>{wid}</code>
⏳ Статус: <b>{wd['status']}</b>
━━━━━━━━━━━━━━━━━━
💰 <b>Резерв Системы</b>

🔢 Баланс: <b>{total_balance:.2f} USDT</b>

<i>{rec}</i>
"""

    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()

@dp.message(F.text == "Кабинет")
async def show_cabinet(message: types.Message, state: FSMContext):
    """Показывает баланс пользователя и действия с ним без конвертации."""
    await state.clear()
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user:
        await message.answer("❌ Не удалось загрузить ваш профиль. Попробуйте ещё раз.")
        return

    currency = await get_setting("currency") or "USD"
    expected_withdrawal = await get_expected_withdrawal(user_id)
    withdrawn = await get_withdrawn_amount(user_id)

    text = (
        f"<tg-emoji emoji-id='5413740297320440148'>📱</tg-emoji> <b>Ваш кабинет:</b>\n\n"
        f"<tg-emoji emoji-id='5411465691230411357'>🆔</tg-emoji> ID: <code>{user_id}</code>\n"
        f"╰ Выполнено заданий: <b>{user['completed_tasks']}</b>\n\n"
        f"<b><tg-emoji emoji-id='5456348576214983692'>💳</tg-emoji> Финансы:</b>\n"
        "<blockquote>"
        f"Баланс для вывода: <b>{float(user.get('balance', 0) or 0):.3f} {currency}</b> "
        f"(≈ {float(user.get('balance', 0) or 0) / 0.017:.2f} ⭐)\n"
        f"├ Ожидается к выплате: <b>{expected_withdrawal:.3f} {currency}</b>\n"
        f"╰ Выведено: <b>{withdrawn:.3f} USD</b>"
        "</blockquote>\n"
        "<blockquote>"
        f"Заморожено на 24ч: <b>{float(user.get('frozen_balance', 0) or 0):.3f} {currency}</b>\n"
        f"Рекламный баланс: <b>{float(user.get('ad_balance', 0) or 0):.3f} $</b>"
        "</blockquote>"
    )

    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="Пополнить",
        callback_data="ad_topup",
        icon_custom_emoji_id="5456348576214983692",
    )
    keyboard.button(
        text="Вывести",
        callback_data="withdraw",
        icon_custom_emoji_id="5456348576214983692",
        style="primary",
    )
    keyboard.button(
        text="Ежедневный бонус",
        callback_data="daily_bonus",
        icon_custom_emoji_id="5458870142924526130",
    )
    miniapp_url = getattr(core, "MINIAPP_URL", "https://uamoneybots.ru/miniapp/?v=20261125k")
    if WebAppInfo is not None and user_id in ADMIN_IDS:
        keyboard.button(text="🌐 Mini App", web_app=WebAppInfo(url=miniapp_url))
        keyboard.adjust(2, 1, 1)
    else:
        keyboard.adjust(2, 1)

    await message.answer(
        text,
        reply_markup=keyboard.as_markup(),
        parse_mode="HTML",
    )


DAILY_BONUS_AMOUNT = 0.01
DAILY_BONUS_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bonus.png")


def _daily_bonus_emoji(emoji_id: str, fallback: str) -> str:
    """HTML-тег Telegram Premium Emoji с обычным emoji как fallback."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


DAILY_BONUS_TEXT = (
    f'{_daily_bonus_emoji("5458870142924526130", "🎁")} '
    '<b>Бонус доступен только тем кто выполнил условие:</b>\n\n'
    f'{_daily_bonus_emoji("5188307557126524632", "👥")} '
    'Пригласил 3х друзей за все время\n'
    f'{_daily_bonus_emoji("5188417224821460749", "✅")} '
    'Выполнил 3 задания за последние 24ч\n'
    f'{_daily_bonus_emoji("5188480700143129440", "🔔")} '
    'Подписался на наш канал и чат'
)


async def _safe_daily_bonus_callback_ack(call: types.CallbackQuery) -> None:
    """Подтверждает callback один раз; просроченный callback повторно не отправляем."""
    try:
        await call.answer()
    except Exception as e:
        # При обрыве сети/устаревшем callback повторный call.answer() только
        # порождает вторую ошибку. Основную операцию с БД всё равно можно завершить.
        print(f"Не удалось подтвердить callback ежедневного бонуса: {e}")


async def _safe_daily_bonus_notice(call: types.CallbackQuery, text: str) -> None:
    """Показывает результат отдельным сообщением после подтверждения callback."""
    try:
        await call.message.answer(text, parse_mode="HTML")
    except Exception as e:
        print(f"Не удалось отправить уведомление о ежедневном бонусе: {e}")


async def _daily_bonus_is_member(user_id: int, chat_username: str):
    """True/False — результат проверки, None — Telegram не дал проверить участника."""
    try:
        member = await bot.get_chat_member(f"@{chat_username}", user_id)
        status = str(getattr(member, "status", "")).lower()
        if status == "restricted":
            return bool(getattr(member, "is_member", False))
        return status in {"member", "administrator", "creator"}
    except Exception as e:
        # Отдельная ошибка не должна разрешать выдачу бонуса без подтверждения подписки.
        print(f"Не удалось проверить подписку user_id={user_id} chat=@{chat_username}: {e}")
        return None


async def _daily_bonus_task_count(db, user_id: int) -> int:
    """Считает уникальные задания из всех трёх типов за последние 24 часа."""
    async with db.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT 'flyer:' || signature AS task_key
            FROM completed_tasks
            WHERE user_id = ? AND LOWER(status) IN ('completed', 'done', 'complete', 'true')
              AND datetime(completed_at) >= datetime('now', '-24 hours')
            UNION
            SELECT 'custom:' || task_id AS task_key
            FROM custom_task_completions
            WHERE user_id = ? AND datetime(completed_at) >= datetime('now', '-24 hours')
            UNION
            SELECT 'promo:' || task_id AS task_key
            FROM user_task_completions
            WHERE user_id = ? AND datetime(completed_at) >= datetime('now', '-24 hours')
        )
        """,
        (user_id, user_id, user_id),
    ) as cur:
        return int((await cur.fetchone())[0] or 0)


async def _daily_bonus_progress(user_id: int):
    """Возвращает число рефералов, заданий и подтверждённых подписок."""
    async with aiosqlite.connect("bot_database.db", timeout=30) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)
        ) as cur:
            referrals_count = int((await cur.fetchone())[0] or 0)
        tasks_count = await _daily_bonus_task_count(db, user_id)

    # Оба чата проверяются через Telegram. Для достоверной проверки бот должен
    # быть администратором в канале и чате.
    membership_results = await asyncio.gather(
        _daily_bonus_is_member(user_id, "UAMONY"),
        _daily_bonus_is_member(user_id, "UAMONEYCHAT"),
    )
    subscriptions_count = sum(result is True for result in membership_results)
    subscriptions_check_failed = any(result is None for result in membership_results)
    return referrals_count, tasks_count, subscriptions_count, subscriptions_check_failed


def _daily_bonus_progress_text(
    referrals_count: int,
    tasks_count: int,
    subscriptions_count: int,
    subscriptions_check_failed: bool = False,
) -> str:
    text = (
        f'{_daily_bonus_emoji("5411101937565214389", "❗️")} '
        '<b>Условия ещё не выполнены:</b>\n\n'
        f'Рефералы — {min(referrals_count, 3)}/3\n'
        f'Заданий за 24 часа — {min(tasks_count, 3)}/3\n'
        f'Подписан на канал и чат — {min(subscriptions_count, 2)}/2\n\n'
        f'{_daily_bonus_emoji("5850652674504003062", "⚠️")}'
    )
    if subscriptions_check_failed:
        text += (
            '\n\nНе удалось проверить подписку в Telegram. '
            'Проверьте, что бот добавлен администратором в канал и чат, '
            'и попробуйте ещё раз.'
        )
    return text


@dp.callback_query(F.data == "daily_bonus")
async def daily_bonus_open(call: types.CallbackQuery):
    """Показывает карточку ежедневного бонуса и кнопку получения."""
    await _safe_daily_bonus_callback_ack(call)

    kb = InlineKeyboardBuilder()
    kb.button(text="Забрать бонус", callback_data="daily_bonus_claim", style="primary")
    kb.adjust(1)
    try:
        await call.message.answer_photo(
            FSInputFile(DAILY_BONUS_IMAGE),
            caption=DAILY_BONUS_TEXT,
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception as e:
        print(f"Не удалось отправить изображение ежедневного бонуса: {e}")
        await _safe_daily_bonus_notice(call, DAILY_BONUS_TEXT)


@dp.callback_query(F.data == "daily_bonus_claim")
async def daily_bonus_claim(call: types.CallbackQuery):
    """Проверяет 3 условия и атомарно начисляет $0.01 не чаще раза в 24 часа."""
    user_id = call.from_user.id

    # Сначала быстро закрываем callback. Все результаты дальше отправляются сообщениями.
    await _safe_daily_bonus_callback_ack(call)

    try:
        referrals_count, tasks_count, subscriptions_count, subscriptions_check_failed = (
            await _daily_bonus_progress(user_id)
        )

        eligible = (
            referrals_count >= 3
            and tasks_count >= 3
            and subscriptions_count == 2
            and not subscriptions_check_failed
        )
        if not eligible:
            await _safe_daily_bonus_notice(
                call,
                _daily_bonus_progress_text(
                    referrals_count,
                    tasks_count,
                    subscriptions_count,
                    subscriptions_check_failed,
                ),
            )
            return

        # Транзакция сериализует параллельные нажатия и повторно проверяет условия
        # перед начислением, чтобы один пользователь не получил бонус дважды.
        async with aiosqlite.connect("bot_database.db", timeout=30) as db:
            await db.execute("BEGIN IMMEDIATE")

            async with db.execute(
                "SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)
            ) as cur:
                referrals_count = int((await cur.fetchone())[0] or 0)
            tasks_count = await _daily_bonus_task_count(db, user_id)

            if referrals_count < 3 or tasks_count < 3:
                await db.rollback()
                await _safe_daily_bonus_notice(
                    call,
                    _daily_bonus_progress_text(
                        referrals_count, tasks_count, subscriptions_count
                    ),
                )
                return

            async with db.execute(
                "SELECT claimed_at FROM daily_bonus_claims WHERE user_id = ? "
                "ORDER BY claimed_at DESC LIMIT 1",
                (user_id,),
            ) as cur:
                last_claim = await cur.fetchone()

            if last_claim:
                async with db.execute(
                    "SELECT (strftime('%s', 'now') - strftime('%s', ?))",
                    (last_claim[0],),
                ) as cur:
                    elapsed = int((await cur.fetchone())[0] or 0)

                if elapsed < 86400:
                    remaining = 86400 - max(0, elapsed)
                    hours, rem = divmod(remaining, 3600)
                    minutes = rem // 60
                    await db.rollback()
                    await _safe_daily_bonus_notice(
                        call,
                        f'{_daily_bonus_emoji("5850652674504003062", "⚠️")} '
                        f'<b>Бонус уже получен.</b> Следующий будет доступен через {hours} ч. {minutes} мин.',
                    )
                    return

            balance_update = await db.execute(
                "UPDATE users SET balance = COALESCE(balance, 0) + ?, "
                "lifetime_earned = COALESCE(lifetime_earned, 0) + ? WHERE user_id = ?",
                (DAILY_BONUS_AMOUNT, DAILY_BONUS_AMOUNT, user_id),
            )
            if balance_update.rowcount != 1:
                await db.rollback()
                raise RuntimeError(f"Пользователь {user_id} не найден при начислении бонуса")

            await db.execute(
                "INSERT INTO daily_bonus_claims (user_id, amount) VALUES (?, ?)",
                (user_id, DAILY_BONUS_AMOUNT),
            )
            await db.execute(
                "INSERT INTO balance_history (user_id, amount, type, reason) VALUES (?, ?, ?, ?)",
                (user_id, DAILY_BONUS_AMOUNT, "daily_bonus", "Ежедневный бонус"),
            )
            await db.commit()

        success_text = (
            f'{_daily_bonus_emoji("5456348576214983692", "🎉")} '
            '<b>Бонус 0.01$ получен</b>'
        )
        try:
            await call.message.edit_caption(
                caption=success_text,
                reply_markup=None,
                parse_mode="HTML",
            )
        except Exception as edit_error:
            print(f"Не удалось обновить карточку ежедневного бонуса: {edit_error}")
            await _safe_daily_bonus_notice(call, success_text)

    except Exception as e:
        print(f"Ошибка получения ежедневного бонуса для {user_id}: {e}")
        await _safe_daily_bonus_notice(
            call,
            "⚠️ Не удалось обработать бонус из-за ошибки. Проверьте соединение и попробуйте ещё раз.",
        )


@dp.message(AdminStates.waiting_user_id)
async def show_user_info(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        user = await get_user(user_id)

        if not user:
            await message.answer("❌ Пользователь не найден")
            await state.clear()
            return

        # Получаем данные о рефералах
        ref_data = await get_referral_data(user_id)

        # Получаем историю выводов
        async with aiosqlite.connect('bot_database.db') as db:
            async with db.execute('SELECT COUNT(*), SUM(amount) FROM withdrawals WHERE user_id = ? AND status = "approved"', (user_id,)) as cursor:
                result = await cursor.fetchone()
                withdrawals_count = result[0] or 0
                total_withdrawn = result[1] or 0

        referrer_text = "⭕"
        if user.get("referrer_id"):
            ref_user = await get_user(user["referrer_id"])
            if ref_user:
                referrer_text = f"@{ref_user['username']}" if ref_user["username"] else f"{ref_user['user_id']}"

        text = (
            f"👤 <b>Информация о пользователе</b>\n\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"📛 Имя: @{user['username']}\n"
            f"👥 Пригласил: <b>{referrer_text}</b>\n"
            f"📅 Зарегистрирован: {user.get('registered_at', 'Неизвестно')}\n\n"
            f"💰 <b>Финансы:</b>\n"
            f"• Баланс: <b>{user['balance']:.3f}$</b>\n"
            f"• Заморожено: <b>{user['frozen_balance']:.3f}$</b>\n"
            f"• Всего выведено: <b>{total_withdrawn:.3f}$</b> ({withdrawals_count} выводов)\n\n"
            f"📊 <b>Активность:</b>\n"
            f"• Выполнено заданий: <b>{user['completed_tasks']}</b>\n"
            f"• Рефералы 1 ур: <b>{ref_data['level1_count']}</b>\n"
            f"• Рефералы 2 ур: <b>{ref_data['level2_count']}</b>\n"
            f"• Статус: {'❌ Заблокирован' if user['is_blocked'] else '✅ Активен'}\n"
        )

        keyboard = InlineKeyboardBuilder()

        # Кнопки управления балансом
        keyboard.button(text="💰 Начислить", callback_data=f"add_balance_{user_id}")
        keyboard.button(text="➖ Снять", callback_data=f"remove_balance_{user_id}")

        # Кнопки блокировки/разблокировки
        if user['is_blocked']:
            keyboard.button(text="🔓 Разблокировать", callback_data=f"unban_{user_id}")
        else:
            keyboard.button(text="🔒 Заблокировать", callback_data=f"ban_{user_id}")

        # Кнопки рефералов
        keyboard.button(text="👥 Рефералы", callback_data=f"show_refs_{user_id}")
        keyboard.button(text="📊 Статистика", callback_data=f"user_stats_{user_id}")
        keyboard.button(text="🗑 Очистить БД", callback_data=f"clean_userdb:{user_id}")

        keyboard.button(text="🔙 Назад", callback_data="users_back")
        keyboard.adjust(2, 2, 1, 1)

        await message.answer(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')
        await state.clear()

    except ValueError:
        await message.answer("❌ Введите корректный ID пользователя (только цифры)")

# Обработчик кнопки "Статистика"
@dp.callback_query(F.data == "users_stats")
async def users_stats(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('SELECT COUNT(*) FROM users') as cursor:
            total_users = (await cursor.fetchone())[0]

        async with db.execute('SELECT COUNT(*) FROM users WHERE is_blocked = FALSE') as cursor:
            active_users = (await cursor.fetchone())[0]

        async with db.execute('SELECT COUNT(*) FROM users WHERE is_blocked = TRUE') as cursor:
            banned_users = (await cursor.fetchone())[0]

        async with db.execute('SELECT SUM(balance) FROM users') as cursor:
            total_balance = (await cursor.fetchone())[0] or 0

        async with db.execute('SELECT SUM(frozen_balance) FROM users') as cursor:
            total_frozen = (await cursor.fetchone())[0] or 0

        async with db.execute('SELECT SUM(completed_tasks) FROM users') as cursor:
            total_tasks = (await cursor.fetchone())[0] or 0

        async with db.execute('SELECT COUNT(*), SUM(amount) FROM withdrawals WHERE status = "approved"') as cursor:
            result = await cursor.fetchone()
            total_withdrawals = result[0] or 0
            total_withdrawn = result[1] or 0

    text = (
        "📊 <b>Общая статистика бота</b>\n\n"
        f"👥 <b>Пользователи:</b>\n"
        f"• Всего: <b>{total_users}</b>\n"
        f"• Активных: <b>{active_users}</b>\n"
        f"• Заблокировано: <b>{banned_users}</b>\n\n"
        f"💰 <b>Финансы:</b>\n"
        f"• Общий баланс: <b>{total_balance:.3f}$</b>\n"
        f"• Заморожено: <b>{total_frozen:.3f}$</b>\n"
        f"• Всего выведено: <b>{total_withdrawn:.3f}$</b>\n"
        f"• Количество выводов: <b>{total_withdrawals}</b>\n\n"
        f"📝 <b>Активность:</b>\n"
        f"• Выполнено заданий: <b>{total_tasks}</b>"
    )

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="users_back")

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')
    await call.answer()

# Визначаємо рефера
@dp.callback_query(F.data.startswith("user_stats_"))
async def user_stats_detail(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    user_id = int(call.data.split("_")[2])

    # Получаем детальную статистику пользователя
    async with aiosqlite.connect('bot_database.db') as db:
        # История баланса
        async with db.execute('SELECT COUNT(*), SUM(amount) FROM balance_history WHERE user_id = ? AND amount > 0', (user_id,)) as cursor:
            result = await cursor.fetchone()
            balance_additions = result[0] or 0
            total_added = result[1] or 0

        # История списаний
        async with db.execute('SELECT COUNT(*), SUM(amount) FROM balance_history WHERE user_id = ? AND amount < 0', (user_id,)) as cursor:
            result = await cursor.fetchone()
            balance_removals = result[0] or 0
            total_removed = result[1] or 0

        # Выводы
        async with db.execute('SELECT COUNT(*), SUM(amount) FROM withdrawals WHERE user_id = ?', (user_id,)) as cursor:
            result = await cursor.fetchone()
            withdrawals_count = result[0] or 0
            total_withdrawn = result[1] or 0

    user = await get_user(user_id)

    text = (
        f"📊 <b>Детальная статистика пользователя {user_id}</b>\n\n"
        f"👤 {user['username']}\n\n"
        f"💰 <b>Финансовая история:</b>\n"
        f"• Начислений: <b>{balance_additions}</b> на сумму <b>{total_added:.3f}$</b>\n"
        f"• Списаний: <b>{balance_removals}</b> на сумму <b>{abs(total_removed):.3f}$</b>\n"
        f"• Запросов на вывод: <b>{withdrawals_count}</b> на сумму <b>{total_withdrawn:.3f}$</b>\n\n"
        f"📝 <b>Текущие показатели:</b>\n"
        f"• Баланс: <b>{user['balance']:.3f}$</b>\n"
        f"• Заморожено: <b>{user['frozen_balance']:.3f}$</b>\n"
        f"• Выполнено заданий: <b>{user['completed_tasks']}</b>"
    )

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data=f"back_to_user_{user_id}")

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')
    await call.answer()

# --- Відкриваємо меню налаштувань вимог для виводу ---
@dp.callback_query(F.data == "withdraw_requirements")
async def withdraw_requirements_menu(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    username_req = await get_setting("req_username") == "1"
    avatar_req = await get_setting("req_avatar") == "1"
    lang_req = await get_setting("req_language") == "1"
    allowed = (await get_setting("allowed_languages") or "uk,ru").split(",")
    allowed_display = ", ".join([l for l in allowed if l])

    kb = InlineKeyboardBuilder()
    kb.button(text=f"👤 Юзернейм: {'🟢 Вкл' if username_req else '🔴 Выкл'}", callback_data="toggle_req_username")
    kb.button(text=f"🖼 Аватар: {'🟢 Вкл' if avatar_req else '🔴 Выкл'}", callback_data="toggle_req_avatar")
    kb.button(text=f"🌐 Язык: {'🟢 Вкл' if lang_req else '🔴 Выкл'}", callback_data="toggle_req_language")
    kb.button(text="🌎 Доступные языки", callback_data="edit_allowed_langs_page_1")
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="settings_back"))
    kb.adjust(2, 2, 1)

    await call.message.edit_text(
        "🛡 <b>Требования для вывода</b>\n\nВыберите необходимые проверки и допустимые языки:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await call.answer()

# --- Toggle handlers ---
@dp.callback_query(F.data == "toggle_req_username")
async def toggle_req_username(call: types.CallbackQuery):
    cur = await get_setting("req_username") or "0"
    await set_setting("req_username", "0" if cur == "1" else "1")
    await withdraw_requirements_menu(call)

@dp.callback_query(F.data == "toggle_req_avatar")
async def toggle_req_avatar(call: types.CallbackQuery):
    cur = await get_setting("req_avatar") or "0"
    await set_setting("req_avatar", "0" if cur == "1" else "1")
    await withdraw_requirements_menu(call)

@dp.callback_query(F.data == "toggle_req_language")
async def toggle_req_language(call: types.CallbackQuery):
    cur = await get_setting("req_language") or "0"
    await set_setting("req_language", "0" if cur == "1" else "1")
    await withdraw_requirements_menu(call)

# ----------------- КОНЕЦ: Блок управления требованиями для вывода -----------------
@dp.callback_query(F.data == "set_payment_channel")
async def ask_payment_channel(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.answer(
        "📡 Введите @username канала или ID (например: @mychannel или -1001234567890)"
    )
    await state.set_state(AdminStates.waiting_payments_channell)

def _withdrawal_subscription_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="Подписаться на канал", url="https://t.me/UAMONY")
    kb.button(text="Вступить в чат", url="https://t.me/UAMONEYCHAT")
    kb.button(
        text="Проверить подписку",
        callback_data="withdraw_check_subscriptions",
        icon_custom_emoji_id="5411444087544913716",
    )
    kb.adjust(1)
    return kb.as_markup()


async def _withdrawal_subscriptions_ok(user_id: int) -> bool:
    """Канал обязателен; чат проверяем отдельно, т.к. бот может быть кикнут."""
    channel_ok = await _daily_bonus_is_member(user_id, "UAMONY")
    if channel_ok is not True:
        return False

    try:
        member = await bot.get_chat_member("@UAMONEYCHAT", user_id)
    except aiogram.exceptions.TelegramForbiddenError:
        # Бот исключён из чата — не блокируем вывод из-за собственной ошибки.
        # Администратору нужно вернуть бота в @UAMONEYCHAT и выдать админа.
        print(
            f"⚠️ Вывод: бот кикнут из @UAMONEYCHAT, проверка чата пропущена "
            f"для user_id={user_id}; верните бота в чат"
        )
        return True
    except Exception as exc:
        print(f"⚠️ Вывод: не удалось проверить @UAMONEYCHAT для {user_id}: {exc}")
        return False

    status = str(getattr(member, "status", "")).lower()
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return status in {"member", "administrator", "creator"}


async def _notify_withdrawal_subscription_required(message):
    await message.answer(
        "<tg-emoji emoji-id='5411510242926172833'>🔒</tg-emoji> "
        "<b>Вывод средств доступен только подписчикам нашего канала и участникам чата.</b>\n\n"
        "1. Подпишитесь на канал.\n"
        "2. Вступите в чат.\n"
        "3. Нажмите «Проверить подписку».",
        reply_markup=_withdrawal_subscription_keyboard(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "withdraw_check_subscriptions")
async def check_withdrawal_subscriptions(call: types.CallbackQuery, state: FSMContext):
    await start_withdrawal(call, state)


@dp.callback_query(F.data == "withdraw")
async def start_withdrawal(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id

    # Обязательные проверки перед выбором способа вывода.
    if not await _withdrawal_subscriptions_ok(user_id):
        await _notify_withdrawal_subscription_required(call.message)
        await call.answer("Для вывода подпишитесь на канал и вступите в чат.", show_alert=True)
        return
    if await get_setting("req_username") == "1" and not call.from_user.username:
        await call.answer("❌ Для вывода требуется юзернейм!", show_alert=True)
        return
    if await get_setting("req_avatar") == "1":
        try:
            photos = await call.bot.get_user_profile_photos(user_id)
            if photos.total_count == 0:
                await call.answer("❌ Для вывода требуется аватарка!", show_alert=True)
                return
        except Exception:
            await call.answer("❌ Не удалось проверить аватарку. Попробуйте позже.", show_alert=True)
            return
    if await get_setting("req_language") == "1":
        allowed = (await get_setting("allowed_languages") or "uk,ru").split(",")
        user_lang = (call.from_user.language_code or "").split("-")[0]
        if user_lang not in allowed:
            await call.answer(
                f"❌ Язык Telegram не поддерживается!\nРазрешённые: {', '.join(allowed)}",
                show_alert=True,
            )
            return

    user = await get_user(user_id)
    if float(user.get("balance", 0) or 0) <= 0:
        await call.answer("❌ На балансе нет средств для вывода.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="USDT",
            callback_data="withdraw_method_usdt",
            icon_custom_emoji_id="5456348576214983692",
        )],
        [InlineKeyboardButton(
            text="Stars",
            callback_data="withdraw_method_stars",
            icon_custom_emoji_id="5967512159033234930",
        )],
    ])
    await call.message.answer("Выберите способ вывода:", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "withdraw_method_usdt")
async def withdraw_method_usdt(call: types.CallbackQuery, state: FSMContext):
    user = await get_user(call.from_user.id)
    min_amount = float(await get_setting('min_withdrawal') or 0.2)
    currency = await get_setting('currency') or 'USD'
    if user['balance'] < min_amount:
        await call.answer(f"❌ Минимальная сумма для вывода: {min_amount:.3f} {currency}", show_alert=True)
        return
    await call.message.answer(
        f"<tg-emoji emoji-id='5456348576214983692'>💰</tg-emoji> "
        f"Введите сумму для вывода USDT (доступно: {user['balance']:.3f} {currency}):",
        parse_mode="HTML",
    )
    await state.set_state(WithdrawalStates.waiting_for_amount)
    await call.answer()


@dp.callback_query(F.data == "withdraw_method_stars")
async def withdraw_method_stars(call: types.CallbackQuery):
    user = await get_user(call.from_user.id)
    balance = float(user.get("balance", 0) or 0)
    stars_emoji = "<tg-emoji emoji-id='5967512159033234930'>⭐</tg-emoji>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="15", callback_data="withdraw_stars_15", icon_custom_emoji_id="5967512159033234930"),
         InlineKeyboardButton(text="25", callback_data="withdraw_stars_25", icon_custom_emoji_id="5967512159033234930")],
        [InlineKeyboardButton(text="50", callback_data="withdraw_stars_50", icon_custom_emoji_id="5967512159033234930"),
         InlineKeyboardButton(text="100", callback_data="withdraw_stars_100", icon_custom_emoji_id="5967512159033234930")],
    ])
    await call.message.answer(
        f"{stars_emoji} <b>Вывод Telegram Stars</b>\n\n"
        f"Курс: 1 {stars_emoji} = 0,017 $\n"
        f"Ваш баланс: <b>{balance:.3f} $</b> (≈ {balance / 0.017:.2f} {stars_emoji})\n"
        "Выберите количество звёзд:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await call.answer()


@dp.callback_query(F.data.regexp(r"^withdraw_stars_(15|25|50|100)$"))
async def create_stars_withdrawal(call: types.CallbackQuery):
    stars_amount = int(call.data.rsplit("_", 1)[1])
    usd_amount = round(stars_amount * 0.017, 6)
    user_id = call.from_user.id

    if not await _withdrawal_subscriptions_ok(user_id):
        await _notify_withdrawal_subscription_required(call.message)
        await call.answer("Для вывода подпишитесь на канал и вступите в чат.", show_alert=True)
        return

    user = await get_user(user_id)
    if float(user.get("balance", 0) or 0) + 1e-9 < usd_amount:
        await call.answer(
            f"Недостаточно средств: для {stars_amount} ⭐ нужно {usd_amount:.3f} $.",
            show_alert=True,
        )
        return

    # Списываем долларовый эквивалент и регистрируем заявку в общей очереди выводов.
    changed = await update_user_balance(user_id, amount=-usd_amount)
    if changed is False:
        await call.answer("Не удалось списать средства. Попробуйте ещё раз.", show_alert=True)
        return
    withdrawal_id = await create_withdrawal_request(
        user_id=user_id,
        amount=usd_amount,
        payout_method="stars",
        stars_amount=stars_amount,
    )
    await call.message.answer(
        "<tg-emoji emoji-id='5458870142924526130'>📨</tg-emoji> "
        "<b>Заявка на вывод Stars отправлена!</b>\n\n"
        f"Количество: <b>{stars_amount} <tg-emoji emoji-id='5967512159033234930'>⭐</tg-emoji></b>\n\n"
        f"Списано с баланса: <b>{usd_amount:.3f} $</b>\n"
        "Ожидайте решения администрации.",
        parse_mode="HTML",
    )
    await send_withdraw_request_to_admins(withdrawal_id)
    await call.answer("Заявка отправлена")


@dp.message(
    WithdrawalStates.waiting_for_amount,
    F.text.regexp(r"^-?\d+(?:[.,]\d+)?$")
)
async def process_withdrawal_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(",", "."))

        # Нулевая сумма означает отмену вывода: сбрасываем FSM,
        # чтобы пользователь мог свободно пользоваться остальными разделами бота.
        if amount == 0:
            await state.clear()
            await message.answer(
                "❌ Вывод отменён. Вы можете выбрать другой раздел бота."
            )
            return

        # Повторная проверка непосредственно перед созданием заявки:
        # пользователь мог отписаться после открытия формы вывода.
        if not await _withdrawal_subscriptions_ok(message.from_user.id):
            await _notify_withdrawal_subscription_required(message)
            return

        user = await get_user(message.from_user.id)
        min_amount = float(await get_setting('min_withdrawal') or 0.1)

        if amount < 0:
            await message.answer("❌ Сумма не может быть отрицательной. Введите сумму больше нуля или 0 для отмены.")
            return

        if amount < min_amount:
            await message.answer(f"❌ Минимальная сумма для вывода: {min_amount}$")
            return

        if amount > user['balance']:
            await message.answer("❌ Недостаточно средств на балансе")
            return

        # 💰 Списываем баланс сразу!
        await update_user_balance(message.from_user.id, amount=-amount)

        # Создаем заявку
        withdrawal_id = await create_withdrawal_request(
            user_id=message.from_user.id,
            amount=amount
        )

        # Уведомляем юзера
        await message.answer(
            "<tg-emoji emoji-id='5413526433718904205'>📨</tg-emoji> "
            "<b>Заявка на вывод отправлена!</b>\n"
            f"<tg-emoji emoji-id='5456348576214983692'>💰</tg-emoji> "
            f"Сумма: <b>{amount:.3f}$</b>\n"
            "<tg-emoji emoji-id='5411637614476305607'>⏳</tg-emoji> "
            "Ожидайте решения администрации.",
            parse_mode="HTML"
        )

        # Отправляем админам
        await send_withdraw_request_to_admins(withdrawal_id)

        await state.clear()

    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.callback_query(F.data.startswith("wd_confirm:"))
async def wd_confirm_handler(call: CallbackQuery):
    withdrawal_id = int(call.data.split(":")[1])
    wd = await get_withdrawal(withdrawal_id)
    user = await get_user(wd["user_id"])

    amount = float(wd["amount"] or 0)
    payout_method = wd.get("payout_method", "usdt") or "usdt"

    if payout_method == "stars":
        stars_amount = int(wd.get("stars_amount") or 0)
        await update_withdrawal_status(
            withdrawal_id,
            status="approved",
            admin_id=call.from_user.id,
        )
        await bot.send_message(
            user['user_id'],
            "<tg-emoji emoji-id='5411126895620167819'>✅</tg-emoji> "
            "<b>Заявка на вывод Stars подтверждена!</b>\n\n"
            f"Количество: <b>{stars_amount} <tg-emoji emoji-id='5967512159033234930'>⭐</tg-emoji></b>\n\n"
            "Выплата будет выполнена администрацией.",
            parse_mode="HTML",
        )
        await call.message.edit_text(
            "<tg-emoji emoji-id='5411126895620167819'>✅</tg-emoji> "
            "<b>Заявка на вывод Stars подтверждена!</b>\n\n"
            f"ID: <code>{withdrawal_id}</code>\n"
            f"Количество: <b>{stars_amount} <tg-emoji emoji-id='5967512159033234930'>⭐</tg-emoji></b>\n"
            f"Сумма списания: <b>{amount:.3f} $</b>",
            parse_mode="HTML",
        )
        await call.answer("Заявка подтверждена")
        return

    # Обычный вывод USDT: прежняя автоматическая обработка без изменений.
    success, receipt_url = await generate_crypto_receipt(amount, user['user_id'])

    await update_withdrawal_status(
        withdrawal_id,
        status="approved",
        admin_id=call.from_user.id,
        admin_receipt_url=receipt_url
    )


    await send_payment_notification(user['user_id'], amount)
    chat_link = await get_setting("chat_link")

    # Уведомляем пользователя
    kb = InlineKeyboardBuilder()
    if success and receipt_url:
        kb.button(text="📎 Открыть чек", url=receipt_url)
    else:
        kb.button(text="❌ НЕ СОЗДАН", callback_data="noop")

    if chat_link and chat_link.startswith("http"):
        kb.button(text="💬 Оставить отзыв", url=chat_link)
    else:
    # Фолбек, если админ не установил чат
        kb.button(text="💬 Оставить отзыв", url="https://t.me/UAMONEYCHAT")
    kb.adjust(1)

    asyncio.create_task(show_ad_at_opportunity(user['user_id'], "after_withdrawal"))
    await bot.send_message(
        user['user_id'],
        "<b>Ваш вывод одобрен!</b>\n"
        f"<tg-emoji emoji-id='5456348576214983692'>💰</tg-emoji> "
        f"Сумма: <b>{amount:.2f}$</b>\n\n"
        "<tg-emoji emoji-id='5413526433718904205'>📨</tg-emoji> "
        "Оставьте пожалуйста отзыв о выплате в нашем чате.\n"
        "<blockquote><i>P.s: если оставите отзыв — выплата будет без очереди.</i></blockquote>",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )
    await call.message.edit_text(
        f"✅ <b>Вывод подтвержден!</b>\nID: <code>{withdrawal_id}</code>\nСумма: <b>{amount}$</b>",
        parse_mode="HTML"
    )
    await call.answer("Готово")

@dp.callback_query(F.data.startswith("wd_norefund:"))
async def wd_norefund_handler(call: CallbackQuery):
    withdrawal_id = int(call.data.split(":")[1])
    wd = await get_withdrawal(withdrawal_id)
    user = await get_user(wd["user_id"])

    await update_withdrawal_status(withdrawal_id, status="declined", admin_id=call.from_user.id)

    await bot.send_message(
        user['user_id'],
        "<tg-emoji emoji-id='5411532233158726966'>❌</tg-emoji> <b>Ваш вывод отклонён из за нарушения правил проекта.</b>",
        parse_mode="HTML"
    )

    await call.message.edit_text("❌ Вывод отклонён (без возврата).", parse_mode="HTML")
    await call.answer("Готово")

@dp.callback_query(F.data.startswith("wd_userinfo:"))
async def wd_userinfo_handler(call: CallbackQuery):
    user_id = int(call.data.split(":")[1])
    user = await get_user(user_id)

    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    # Рефералы
    ref_data = await get_referral_data(user_id)

    # История выводов
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute(
            'SELECT COUNT(*), SUM(amount) FROM withdrawals WHERE user_id = ? AND status = "approved"',
            (user_id,)
        ) as cursor:
            result = await cursor.fetchone()
            withdrawals_count = result[0] or 0
            total_withdrawn = result[1] or 0
    referrer_text = "⭕"
    if user.get("referrer_id"):
        ref_user = await get_user(user["referrer_id"])
        if ref_user:
            referrer_text = f"@{ref_user['username']}" if ref_user['username'] else f"{ref_user['user_id']}"

    text = (
        f"👤 <b>Информация о пользователе</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📛 Имя: @{user['username']}\n"
        f"👥 Пригласил: <b>{referrer_text}</b>\n"
        f"📅 Зарегистрирован: {user.get('registered_at', 'Неизвестно')}\n\n"
        f"💰 <b>Финансы:</b>\n"
        f"• Баланс: <b>{user['balance']:.3f}$</b>\n"
        f"• Заморожено: <b>{user['frozen_balance']:.3f}$</b>\n"
        f"• Всего выведено: <b>{total_withdrawn:.3f}$</b> ({withdrawals_count} выводов)\n\n"
        f"📊 <b>Активность:</b>\n"
        f"• Выполнено заданий: <b>{user['completed_tasks']}</b>\n"
        f"• Рефералы 1 ур: <b>{ref_data['level1_count']}</b>\n"
        f"• Рефералы 2 ур: <b>{ref_data['level2_count']}</b>\n"
        f"• Статус: {'❌ Заблокирован' if user['is_blocked'] else '✅ Активен'}\n"
    )

    # === ИНЛАЙН КНОПКИ ===
    kb = InlineKeyboardBuilder()

    # Блокировка / разблокировка
    if user['is_blocked']:
        kb.button(text="🔓 Разблокировать", callback_data=f"unban_{user_id}")
    else:
        kb.button(text="🔒 Заблокировать", callback_data=f"ban_{user_id}")

    # Рефералы
    kb.button(text="👥 Рефералы", callback_data=f"show_refs_{user_id}")

    # Статистика
    kb.button(text="📊 Статистика", callback_data=f"user_stats_{user_id}")
    kb.button(text="🗑 Очистить БД", callback_data=f"clean_userdb:{user_id}")
    kb.adjust(1, 2)  # по одному в ряд, як у адмінці

    await call.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
    await call.answer()

@dp.callback_query(F.data.startswith("cleanrun:"))
async def clean_userdb_execute(call: CallbackQuery):
    _, action, user_id = call.data.split(":")
    user_id = int(user_id)

    if action == "cancel":
        await call.answer("Отменено")
        await call.message.delete()
        return

    async with aiosqlite.connect("bot_database.db") as db:

        if action == "balance":
            await db.execute("UPDATE users SET balance = 0 WHERE user_id = ?", (user_id,))

        elif action == "frozen":
            await db.execute("UPDATE users SET frozen_balance = 0 WHERE user_id = ?", (user_id,))

        elif action == "referrer":
            await db.execute("UPDATE users SET referrer_id = NULL WHERE user_id = ?", (user_id,))

        elif action == "refs":
            try:
                await db.execute("DELETE FROM referrals WHERE user_id = ? OR referrer_id = ?", (user_id, user_id,))
            except Exception as e:
                print("⚠ Таблицы referrals нет, пропускаем")

        elif action == "stats":
            await db.execute("""
                UPDATE users SET completed_tasks = 0
                WHERE user_id = ?
            """, (user_id,))

        elif action == "delete":
            await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            try:
                await db.execute("DELETE FROM referrals WHERE user_id = ? OR referrer_id = ?", (user_id, user_id,))
            except:
                print("⚠ Таблицы referrals нет, пропускаем")
            await db.execute("DELETE FROM withdrawals WHERE user_id = ?", (user_id,))

        await db.commit()

    await call.message.edit_text(f"✅ Действие <b>{action}</b> успешно выполнено!")
    await call.answer("Готово")











@dp.callback_query(F.data == "change_min_withdrawal")
async def change_min_withdrawal(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.message.answer("Введите новую минимальную сумму для вывода:")
    await state.set_state(AdminStates.waiting_min_withdrawal)
    await call.answer()

@dp.message(AdminStates.waiting_min_withdrawal)
async def process_min_withdrawal(message: types.Message, state: FSMContext):
    try:
        min_amount = float(message.text)
        await set_setting('min_withdrawal', str(min_amount))
        await message.answer(f"✅ Минимальная сумма для вывода обновлена: {min_amount}$")
    except ValueError:
        await message.answer("❌ Введите корректное число")
    await state.clear()

@dp.callback_query(F.data == "admin_settings_back")
async def admin_settings_back(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔑 Настройки Flyer", callback_data="flyer_settings")
    keyboard.button(text="✏️ Сменить канал выплат", callback_data="change_payments_channel")
    keyboard.button(text="💬 Сменить чат", callback_data="change_chat")
    keyboard.button(text="💰 Мин. вывод", callback_data="change_min_withdrawal")
    keyboard.button(text="💵 Валюта", callback_data="change_currency")
    keyboard.button(text="📝 Управление заданиями", callback_data="manage_tasks")
    keyboard.button(text=f"💰 CryptoPay", callback_data="set_crypto_token")
    keyboard.button(text="💸 Настройки рефералов", callback_data="change_referral_settings")
    keyboard.adjust(2, 2, 2, 2)

    await call.message.edit_text("⚙️ <b>Настройки бота:</b>", reply_markup=keyboard.as_markup(), parse_mode='HTML')
    await call.answer()

@dp.callback_query(F.data == "inactive_stats")
async def inactive_users_stats(call: types.CallbackQuery):
    """Статистика по неактивным пользователям"""
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.answer()
    async with aiosqlite.connect('bot_database.db') as db:
        # Общее количество пользователей
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total_users = (await cursor.fetchone())[0]

        # Заблокированные пользователи
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_blocked = TRUE") as cursor:
            blocked_users = (await cursor.fetchone())[0]

        # Пользователи с балансом > 0
        async with db.execute("SELECT COUNT(*) FROM users WHERE balance > 0 OR frozen_balance > 0") as cursor:
            users_with_balance = (await cursor.fetchone())[0]

        # Активные за последние 3 дня
        async with db.execute('''
            SELECT COUNT(DISTINCT u.user_id) 
            FROM users u
            LEFT JOIN completed_tasks ct ON u.user_id = ct.user_id AND ct.completed_at >= datetime('now', '-3 days')
            LEFT JOIN withdrawals w ON u.user_id = w.user_id AND w.created_at >= datetime('now', '-3 days')
            WHERE ct.user_id IS NOT NULL OR w.user_id IS NOT NULL
        ''') as cursor:
            active_recently = (await cursor.fetchone())[0]

        # Неактивные (по нашей логике)
        inactive_users = await get_inactive_users()
        inactive_count = len(inactive_users)

    text = (
        "📊 <b>Статистика активности пользователей</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"✅ С балансом: <b>{users_with_balance}</b>\n"
        f"🔥 Активных (3 дня): <b>{active_recently}</b>\n"
        f"💀 Неактивных: <b>{inactive_count}</b>\n"
        f"🚫 Заблокированных: <b>{blocked_users}</b>\n\n"
        f"<i>Неактивные - баланс 0$ и нет активности 3+ дней</i>"
    )

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔍 Показать неактивных", callback_data="find_inactive_users")
    keyboard.button(text="🗑️ Массовое удаление", callback_data="confirm_mass_delete")
    keyboard.button(text="🔙 Назад", callback_data="inactive_back")
    keyboard.adjust(1)

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')


stats_photo_cache = {"file_id": None, "ts": 0}
STATS_TTL = 10  # секунд, можно менять

async def send_stats_photo(
    message: types.Message,
    keyboard: InlineKeyboardBuilder,
    total_users: int,
    today_users: int,
    total_tasks: int,
    total_withdrawn: float,
    today_withdrawn: float,
    total_ad_topups: float,
):
    """Send the cached statistics image, rendering it when the cache expires."""
    if stats_photo_cache["file_id"] and time.time() - stats_photo_cache["ts"] < STATS_TTL:
        return await message.answer_photo(
            stats_photo_cache["file_id"],
            caption="📈 Статистика обновляется в реальном времени.",
            reply_markup=keyboard.as_markup()
        )

    photo = FSInputFile(core.render_stats_image(
        total_users, today_users, total_tasks,
        total_withdrawn, today_withdrawn, total_ad_topups
    ))
    msg = await message.answer_photo(
        photo,
        caption="📈 Статистика обновляется в реальном времени.",
        reply_markup=keyboard.as_markup()
    )
    stats_photo_cache.update({"file_id": msg.photo[-1].file_id, "ts": time.time()})
    return msg


# ===== handlers_custom_tasks.py =====
# -*- coding: utf-8 -*-
"""Кастомные задания: создание, редактирование, выполнение."""



# Задания - используем flyer_get_tasks
@dp.message(F.text == "Задания")
async def show_tasks(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    if await check_user_blocked(user_id):
        await message.answer("❌ Ваш аккаунт заблокирован. Обратитесь к администратору.")
        return

    flyer_tasks = await flyer_get_tasks(user_id, message.from_user.language_code or 'ru', limit=10)

    custom_tasks = await get_active_custom_tasks()

    all_tasks = []
    completed_count = 0

    for task in custom_tasks:
        try:
            async with aiosqlite.connect('bot_database.db') as db:
                async with db.execute('SELECT 1 FROM custom_task_completions WHERE task_id = ? AND user_id = ?', (task['id'], user_id)) as cursor:
                    has_completed = await cursor.fetchone()

                    if not has_completed:
                        all_tasks.append({
                            'type': 'custom',
                            'id': task['id'],
                            'name': task['name'],
                            'description': task.get('description', ''),
                            'url': task['url'],
                            'button_text': task.get('button_text', '💫 Выполнить'),
                            'price': task['price'],
                            'signature': f"custom_{task['id']}"
                        })
                    else:
                        completed_count += 1
        except Exception as e:
            print(f"❌ Ошибка при проверке кастомного задания: {e}")

    if flyer_tasks:
        for task in flyer_tasks:
            signature = task.get('signature', '')
            if signature:
                saved_status = await get_task_status(user_id, signature)
                if not saved_status:
                    current_status = await flyer_check_task(signature, user_id)
                    await set_task_status(user_id, signature, current_status)
                    saved_status = current_status

                if saved_status in ['completed', 'done', 'complete', True, 'waiting']:
                    completed_count += 1
                    continue
                else:
                    task['type'] = 'core.flyer'
                    all_tasks.append(task)
            else:
                task['type'] = 'core.flyer'
                all_tasks.append(task)

    print(f"🔍 Для пользователя {user_id}: {len(all_tasks)} активных, {completed_count} выполненных заданий")

    if not all_tasks:
        no_tasks_text = (
            '<tg-emoji emoji-id="5411637614476305607">🎉</tg-emoji> '
            '<b>Вы выполнили все доступные задания!</b>\n\n'
            'Пока нет новых, но они появляются каждый час. Заходите позже!\n\n'
            '<blockquote>•<a href="https://telegra.ph/U-menya-net-zadaniya-11-08">Что делать если нет задания?</a></blockquote>'
        )
        await message.answer(
            no_tasks_text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        return

    main_keyboard = InlineKeyboardBuilder()

    for task in all_tasks:
        if task['type'] == 'custom':
            main_keyboard.button(text=task['button_text'], url=task['url'])
        else:
            task_type = task.get('task', '')
            task_link = task.get('link', '')
            resource_id = task.get('resource_id', '')
            username = task.get('username')
            links_list = task.get('links')  # 👈 нове поле від Flyer

            url = None

            # 1️⃣ Якщо Flyer повернув поле "links" (масив) — беремо перший елемент
            if links_list and isinstance(links_list, list) and len(links_list) > 0:
                url = links_list[0]

            # 2️⃣ Інакше використовуємо link або username
            elif task_link and task_link.startswith("https://t.me/"):
                url = task_link
            elif username:
                username = username.replace("@", "")
                url = f"https://t.me/{username}"

            # 3️⃣ Якщо все одно нічого — fallback
            elif resource_id:
                resource_str = str(resource_id)
                if resource_str.startswith("-100"):
                    channel_id = resource_str.replace("-100", "")
                    url = f"https://t.me/c/{channel_id}"
                else:
                    clean_resource = resource_str.replace("@", "")
                    url = f"https://t.me/{clean_resource}"

            # 4️⃣ Формуємо кнопки
            if url:
                if task_type == "subscribe channel":
                    main_keyboard.button(text="Подписаться", url=url)
                elif task_type == "start bot":
                    main_keyboard.button(text="Запустить", url=url)
                elif task_type == "visit website":
                    main_keyboard.button(text="Перейти", url=url)
                elif task_type == "boost channel":
                    main_keyboard.button(text="Голосовать", url=url)
                else:
                    main_keyboard.button(text="Выполнить", url=url)
            else:
                # Невалидная ссылка "#" в web/desktop выглядит как неработающая кнопка.
                # Лучше не добавлять кнопку вообще.
                pass



    # Располагаем кнопки выполнения по 3 в ряд
    main_keyboard.adjust(2)

    main_keyboard.row(InlineKeyboardButton(text="Проверить все задания", callback_data="check_all_tasks", icon_custom_emoji_id="5411444087544913716"))
    DEFAULT_FLYER_REWARD = FLZAD
    potential_earnings = 0.0
    actual_task_count = 0

    debug_lines = []

    for task in all_tasks:
        try:
            task_type = task.get('type', 'core.flyer')
            if task_type == 'custom':
                raw = task.get('price', 0)
                reward = float(raw or 0)
                # Рахуємо тільки позитивні винагороди
                if reward > 0:
                    potential_earnings += reward
                    actual_task_count += 1
                    debug_lines.append(f"custom: {reward}")
            elif task_type == 'piarflow':
                # У PiarFlow награда лежит в поле price задачи
                raw = task.get('price', 0)
                reward = float(raw or 0)
                if reward > 0:
                    potential_earnings += reward
                    actual_task_count += 1
                    debug_lines.append(f"piarflow: {reward}")
            else:
                reward = DEFAULT_FLYER_REWARD
                if reward > 0:
                    potential_earnings += reward
                    actual_task_count += 1
                    debug_lines.append(f"core.flyer: {reward}")

        except Exception as e:
            print(f"⚠️ Ошибка при подсчете награды: {e}")


    # Лог для діагностики — можна закоментувати, якщо все працює
    if debug_lines:
        print("🔍 POTENTIAL EARNINGS DEBUG:", ", ".join(debug_lines))

    # Формуємо заголовок
    text = (
    f'<tg-emoji emoji-id="5312161417372142817">📝</tg-emoji> '
    f'<b>Доступных заданий: {actual_task_count}</b>\n'
    f'<tg-emoji emoji-id="5309795500277403547">💫</tg-emoji> '
    f'<b>Можно заработать: {potential_earnings:.3f}$</b>\n\n'
    f'<tg-emoji emoji-id="5411418403640484720">❗️</tg-emoji> '
    f'Подпишитесь на каналы и нажмите «Проверить задания»\n\n'
    f'<tg-emoji emoji-id="5411101937565214389">⚠️</tg-emoji> '
    f'Важно: если отписаться раньше чем через 24 часа, выплата будет списана'
)

    for i, task in enumerate(all_tasks, 1):
        task_name = task.get('name', 'Задание')

        if isinstance(task_name, str) and ' - ' in task_name:
            task_name = task_name.split(' - ')[0].strip()

        if len(task_name) > 25:
            display_name = task_name[:25] + '...'
        else:
            display_name = task_name

    await message.answer(text, reply_markup=main_keyboard.as_markup(), parse_mode='HTML')

@dp.message(F.text == "Задания №3")
async def show_piarflow_tasks_menu(message: types.Message):
    user_id = message.from_user.id

    if await check_user_blocked(user_id):
        await message.answer("❌ Ваш аккаунт заблокирован. Обратитесь к администратору.")
        return

    try:
        piarflow_tasks = await get_piarflow_tasks(user_id, message.chat.id, max_sponsors=5)
    except Exception as e:
        piarflow_tasks = []
        print(f"⚠️ PiarFlow error: {e}")

    last_error = piarflow_last_error()
    if last_error:
        unavailable_text = (
            "<tg-emoji emoji-id='5411637614476305607'>⏳</tg-emoji> "
            "<b>Задания временно недоступны</b>\n\n"
            "Сервис заданий сейчас не отвечает. Попробуй зайти позже.\n\n"
            "<blockquote>"
            "•<a href='https://telegra.ph/U-menya-net-zadaniya-11-08'>Что делать если нет задания?</a>"
            "</blockquote>"
        )
        await message.answer(
            unavailable_text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    keyboard = InlineKeyboardBuilder()
    available = 0
    potential = 0.0

    for task in piarflow_tasks:
        saved_status = await get_task_status(user_id, task['signature'])
        if saved_status in ('completed', 'done', 'complete', True, 'waiting', 'unsubscribed'):
            continue
        keyboard.button(text="Подписаться", url=task['url'])
        reward = float(task.get('price', 0) or 0)
        if reward > 0:
            potential += reward
            available += 1

    keyboard.row(InlineKeyboardButton(text="Проверить все задания", callback_data="pf_check", icon_custom_emoji_id="5411444087544913716"))
    keyboard.adjust(1)

    if not available:
        no_tasks_text = (
            "<tg-emoji emoji-id='5411637614476305607'>⏳</tg-emoji> "
            "<b>Вы выполнили все доступные задания!</b>\n\n"
            "Пока нет новых, но они появляются каждый час. Заходите позже!\n\n"
            "<blockquote>"
            "•<a href='https://telegra.ph/U-menya-net-zadaniya-11-08'>Что делать если нет задания?</a>"
            "</blockquote>"
        )
        await message.answer(
            no_tasks_text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        return

    text = (
    f'<tg-emoji emoji-id="5312161417372142817">📝</tg-emoji> '
    f'Доступных заданий: <b>{available}</b>\n'
    f'<tg-emoji emoji-id="5309795500277403547">💫</tg-emoji> '
    f'Можно заработать: <b>{potential:.3f}$</b>\n\n'
    f'<tg-emoji emoji-id="5411418403640484720">❗️</tg-emoji> '
    f'Подпишитесь на каналы и нажмите «Проверить задания»\n\n'
    f'<tg-emoji emoji-id="5411101937565214389">⚠️</tg-emoji> '
    f'Важно: если отписаться раньше чем через 24 часа, выплата будет списана'
)

    await message.answer(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')

@dp.callback_query(F.data == "pf_check")
async def check_piarflow_tasks(call: types.CallbackQuery):
    """Проверяет PiarFlow и заменяет исходное сообщение результатом."""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    # Закрываем индикатор сразу: PiarFlow может отвечать несколько секунд.
    try:
        await call.answer()
    except Exception as answer_error:
        print(f"⚠️ Не удалось подтвердить callback PiarFlow: {answer_error}")

    try:
        result = await piarflow_check_and_complete(user_id)
        if isinstance(result, dict):
            raw_completed = result.get("completed", result.get("count", 0))
            raw_reward = result.get("total_reward", result.get("reward", 0))
        elif isinstance(result, (tuple, list)) and len(result) >= 2:
            raw_completed, raw_reward = result[0], result[1]
        else:
            raw_completed, raw_reward = result or 0, 0

        if isinstance(raw_completed, (list, set, dict)):
            completed = len(raw_completed)
        else:
            completed = int(raw_completed or 0)
        total_reward = float(raw_reward or 0.0)

        print(
            f"🔎 PiarFlow check result: user_id={user_id}, "
            f"completed={completed}, total_reward={total_reward:.6f}"
        )

        last_error = piarflow_last_error()
        if last_error:
            result_text = (
                "<tg-emoji emoji-id='5411637614476305607'>⏳</tg-emoji> "
                "<b>Проверка временно недоступна</b>\n\n"
                "Сервис заданий сейчас не отвечает. Попробуй позже."
            )
            print(f"ℹ️ PiarFlow / Задания №3: проверка недоступна ({last_error})")
        elif completed <= 0:
            result_text = _task_check_empty_text()
            print(f"ℹ️ PiarFlow / Задания №3: выполненных подписок не найдено; редактируем сообщение")
        else:
            asyncio.create_task(show_ad_at_opportunity(user_id, "after_task"))
            result_text = _task_check_success_text(completed, total_reward)

        # Как и в заданиях №1/№2: заменяем исходное сообщение со списком
        # заданий результатом проверки. _edit_task_check_result спокойно
        # обрабатывает повторное нажатие, когда текст уже не меняется.
        await _edit_task_check_result(call.message, result_text)

    except Exception as e:
        print(f"❌ Ошибка проверки заданий PiarFlow: {e}")
        try:
            await bot.send_message(
                chat_id,
                "⚠️ Не удалось проверить задания. Попробуй ещё раз позже.",
                parse_mode="HTML",
            )
        except Exception as send_error:
            print(f"⚠️ Не удалось показать ошибку проверки PiarFlow: {send_error}")


@dp.message(F.text == "Задания №4")
async def show_pulsar_tasks_menu(message: types.Message):
    """Задания №4 — обычный список спонсоров Pulsar, как в заданиях №1–№3."""
    user_id = message.from_user.id
    language = (getattr(message.from_user, "language_code", None) or "ru")[:16]

    if await check_user_blocked(user_id):
        await message.answer("❌ Ваш аккаунт заблокирован. Обратитесь к администратору.")
        return

    try:
        sponsors = await pulsar_get_sponsors(user_id, language)
    except Exception as exc:
        sponsors = []
        print(f"⚠️ Pulsar error: {exc}")

    if pulsar_last_error():
        unavailable_text = (
            "<tg-emoji emoji-id='5411637614476305607'>⏳</tg-emoji> "
            "<b>Задания временно недоступны</b>\n\n"
            "Сервис заданий сейчас не отвечает. Попробуй зайти позже.\n\n"
            "<blockquote>"
            "•<a href='https://telegra.ph/U-menya-net-zadaniya-11-08'>Что делать если нет задания?</a>"
            "</blockquote>"
        )
        await message.answer(
            unavailable_text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    available = 0
    potential = 0.0
    reward = pulsar_reward()

    keyboard = InlineKeyboardBuilder()
    # Как в заданиях №2/№3: кнопка «Подписаться» на каждого спонсора
    # и одна общая кнопка проверки внизу. Никакого Mini App.
    for sponsor in sponsors:
        signature = pulsar_signature(sponsor)
        saved_status = await get_task_status(user_id, signature)
        if saved_status in ('completed', 'done', 'complete', True, 'waiting', 'unsubscribed'):
            continue
        url = pulsar_sponsor_link(sponsor)
        if not url:
            continue
        sponsor_id = str(sponsor.get("id") or sponsor.get("sponsor_id") or "") if isinstance(sponsor, dict) else ""
        await save_pulsar_offer(user_id, signature, url, sponsor_id)
        keyboard.button(text="Подписаться", url=url)
        available += 1
        potential += reward

    if available:
        keyboard.row(InlineKeyboardButton(
            text="Проверить все задания",
            callback_data="pls_check",
            icon_custom_emoji_id="5411444087544913716",
        ))
    keyboard.adjust(1)

    if not available:
        no_tasks_text = (
            "<tg-emoji emoji-id='5411637614476305607'>⏳</tg-emoji> "
            "<b>Вы выполнили все доступные задания!</b>\n\n"
            "Пока нет новых, но они появляются каждый час. Заходите позже!\n\n"
        )
        reason = pulsar_empty_reason()
        if reason:
            no_tasks_text += f"{reason}\n\n"
        no_tasks_text += (
            "<blockquote>"
            "•<a href='https://telegra.ph/U-menya-net-zadaniya-11-08'>Что делать если нет задания?</a>"
            "</blockquote>"
        )
        await message.answer(
            no_tasks_text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    text = (
        f'<tg-emoji emoji-id="5312161417372142817">📝</tg-emoji> '
        f'Доступных заданий: <b>{available}</b>\n'
        f'<tg-emoji emoji-id="5309795500277403547">💫</tg-emoji> '
        f'Можно заработать: <b>{potential:.3f}$</b>\n\n'
        f'<tg-emoji emoji-id="5411418403640484720">❗️</tg-emoji> '
        f'Подпишитесь на спонсоров и нажмите «Проверить задания»\n\n'
        f'<tg-emoji emoji-id="5411101937565214389">⚠️</tg-emoji> '
        f'Важно: если отписаться раньше чем через 24 часа, выплата будет списана'
    )
    await message.answer(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data == "pls_check")
async def check_pulsar_tasks(call: types.CallbackQuery):
    """Проверяет подписки Pulsar и заменяет исходное сообщение результатом."""
    user_id = call.from_user.id
    language = (getattr(call.from_user, "language_code", None) or "ru")[:16]

    try:
        await call.answer()
    except Exception as answer_error:
        print(f"⚠️ Не удалось подтвердить callback Pulsar: {answer_error}")

    try:
        completed, total_reward = await pulsar_check_and_complete(user_id, language)
        if pulsar_last_error():
            result_text = (
                "<tg-emoji emoji-id='5411637614476305607'>⏳</tg-emoji> "
                "<b>Проверка временно недоступна</b>\n\n"
                "Сервис заданий сейчас не отвечает. Попробуй позже."
            )
        elif completed <= 0:
            result_text = _task_check_empty_text()
        else:
            asyncio.create_task(show_ad_at_opportunity(user_id, "after_task"))
            result_text = _task_check_success_text(completed, total_reward)
        await _edit_task_check_result(call.message, result_text)
    except Exception as exc:
        print(f"❌ Ошибка проверки заданий Pulsar: {exc}")
        await _edit_task_check_result(
            call.message,
            "⚠️ Не удалось проверить задания. Попробуй ещё раз позже.",
        )


@dp.callback_query(F.data == "manage_tasks")
async def manage_tasks(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    # Получаем список заданий
    tasks = await get_active_custom_tasks()

    text = "📝 <b>Управление заданиями</b>\n\n"
    text += f"📊 Активных заданий: {len(tasks)}\n\n"

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="➕ Добавить задание", callback_data="add_custom_task")

    if tasks:
        for task in tasks[:10]:  # Показываем первые 10
            completions_info = f"{task['current_completions']}/{task['max_completions']}" if task['max_completions'] > 0 else f"{task['current_completions']}"
            keyboard.button(text=f"📝 {task['name'][:15]}... ({completions_info})", callback_data=f"view_task_{task['id']}")

    keyboard.button(text="📊 Общая статистика", callback_data="tasks_stats")
    keyboard.button(text="🔙 Назад", callback_data="admin_settings_back")
    keyboard.adjust(1, 2, 1, 1)

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')
    await call.answer()

async def create_custom_task(name, description, url, button_text, price, max_completions, created_by):
    async with aiosqlite.connect('bot_database.db') as db:
        cursor = await db.execute('''
            INSERT INTO custom_tasks (name, description, url, button_text, price, max_completions, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (name, description, url, button_text, price, max_completions, created_by))
        await db.commit()
        return cursor.lastrowid

async def get_active_custom_tasks():
    """Получает активные кастомные задания"""
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('''
            SELECT * FROM custom_tasks 
            WHERE is_active = TRUE 
            AND (max_completions = 0 OR current_completions < max_completions)
            ORDER BY created_at DESC
        ''') as cursor:
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in await cursor.fetchall()]

async def complete_custom_task(task_id, user_id):
    """Отмечает задание как выполненное и возвращает True при успехе"""
    async with aiosqlite.connect('bot_database.db') as db:
        # Проверяем не выполнял ли уже пользователь это задание
        async with db.execute('SELECT 1 FROM custom_task_completions WHERE task_id = ? AND user_id = ?', (task_id, user_id)) as cursor:
            if await cursor.fetchone():
                print(f"⚠️ Пользователь {user_id} уже выполнял задание {task_id}")
                return False  # Уже выполнял

        # Добавляем запись о выполнении
        await db.execute('INSERT INTO custom_task_completions (task_id, user_id) VALUES (?, ?)', (task_id, user_id))

        # Увеличиваем счетчик выполнений
        await db.execute('UPDATE custom_tasks SET current_completions = current_completions + 1 WHERE id = ?', (task_id,))

        await db.commit()
        print(f"✅ Задание {task_id} отмечено как выполненное для пользователя {user_id}")
        return True  # Успешно выполнено

async def get_custom_task_completions_count(task_id):
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('SELECT COUNT(*) FROM custom_task_completions WHERE task_id = ?', (task_id,)) as cursor:
            return (await cursor.fetchone())[0]

@dp.callback_query(F.data == "add_custom_task")
async def add_custom_task_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    print("🔧 Начало создания задания")
    await call.message.answer("📝 Введите название задания:")
    await state.set_state(AdminStates.waiting_task_name)
    await call.answer()

@dp.message(AdminStates.waiting_task_url)
async def process_task_url(message: types.Message, state: FSMContext):
    print(f"🔧 Получен URL: {message.text}")
    data = await state.get_data()
    url = message.text

    # Формируем URL для канала
    if url.startswith('@'):
        channel_username = url[1:]
    else:
        channel_username = url.replace('@', '')

    url = f"https://t.me/{channel_username}"
    await state.update_data(url=url, channel_username=channel_username, button_text="Подписаться")

    await message.answer("💰 Введите цену за это задание (например: 0.015 или 1.0):")
    await state.set_state(AdminStates.waiting_custom_task_price)

@dp.message(AdminStates.waiting_custom_task_price)
async def process_custom_task_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        if price <= 0:
            await message.answer("❌ Цена должна быть больше 0")
            return

        await state.update_data(price=price)
        await message.answer("👥 Введите максимальное количество выполнений (0 - без ограничений):")
        await state.set_state(AdminStates.waiting_task_max_completions)

    except ValueError:
        await message.answer("❌ Введите корректное число (например: 0.015 или 1.0)")

@dp.message(AdminStates.waiting_task_max_completions)
async def process_task_max_completions(message: types.Message, state: FSMContext):
    try:
        max_completions = int(message.text)
        data = await state.get_data()

        # Создаем задание
        task_id = await create_custom_task(
            name=data['name'],
            description=data['description'],
            url=data['url'],
            button_text=data['button_text'],
            price=data['price'],
            max_completions=max_completions,
            task_type=data.get('task_type', 'subscribe_channel'),
            channel_username=data.get('channel_username'),
            created_by=message.from_user.id
        )

        await message.answer(
            f"✅ <b>Задание создано!</b>\n\n"
            f"📝 Название: {data['name']}\n"
            f"💰 Цена: {data['price']}$\n"
            f"👥 Макс. выполнений: {max_completions if max_completions > 0 else 'без ограничений'}\n"
            f"🆔 ID задания: {task_id}\n\n"
            f"⚠️ <b>ВАЖНО!</b> Добавьте бота @{(await bot.get_me()).username} в администраторы канала {data.get('channel_username')}",
            parse_mode='HTML'
        )

        await state.clear()

        # Массовая рассылка о новом задании отключена по запросу.
        # Само задание создаётся и остаётся доступным пользователям.

    except ValueError:
        await message.answer("❌ Введите корректное число для максимальных выполнений.")

@dp.callback_query(F.data.startswith("check_custom_"))
async def check_custom_task(call: types.CallbackQuery):
    task_id = int(call.data.replace("check_custom_", ""))
    user_id = call.from_user.id

    try:
        # Получаем информацию о задании
        task = await get_custom_task(task_id)

        if not task:
            await call.answer("❌ Задание не найдено", show_alert=True)
            return

        # Проверяем не выполнял ли уже
        async with aiosqlite.connect('bot_database.db') as db:
            async with db.execute('SELECT 1 FROM custom_task_completions WHERE task_id = ? AND user_id = ?', (task_id, user_id)) as cursor:
                already_completed = await cursor.fetchone()

        if already_completed:
            await call.answer("❌ Вы уже выполняли это задание", show_alert=True)
            return

        # ПРОВЕРКА ВЫПОЛНЕНИЯ В ЗАВИСИМОСТИ ОТ ТИПА ЗАДАНИЯ
        if task['task_type'] == 'subscribe_channel' and task['channel_username']:
            # Проверяем подписку на канал
            await check_channel_subscription(task_id, user_id, task, call)
        else:
            # Для обычных ссылок - сразу начисляем награду
            await complete_and_reward(task_id, user_id, task, call)

    except Exception as e:
            print(f"❌ Ошибка проверки кастомного задания: {e}")
            await call.answer("❌ Ошибка при проверке", show_alert=True)

    await call.answer()

async def get_custom_task(task_id):
    """Получает информацию о кастомном задании"""
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('SELECT * FROM custom_tasks WHERE id = ?', (task_id,)) as cursor:
            columns = [column[0] for column in cursor.description]
            row = await cursor.fetchone()
            return dict(zip(columns, row)) if row else None

@dp.callback_query(F.data.startswith("view_task_"))
async def view_custom_task(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("view_task_", ""))

    # Получаем информацию о задании
    task = await get_custom_task(task_id)  # ← ДОБАВЬТЕ ЭТУ СТРОЧКУ

    if not task:
        await call.answer("❌ Задание не найдено")
        return

    # Получаем количество выполнений
    completions_count = await get_custom_task_completions_count(task_id)
    max_completions = task['max_completions'] if task['max_completions'] > 0 else '∞'

    text = (
        f"📝 <b>Информация о задании</b>\n\n"
        f"🆔 ID: <code>{task_id}</code>\n"
        f"📛 Название: {task['name']}\n"
        f"📄 Описание: {task.get('description', 'Нет описания')}\n"
        f"🔗 URL: {task['url']}\n"
        f"🔄 Текст кнопки: {task.get('button_text', 'Выполнить')}\n"
        f"💰 Цена: <b>{task['price']}$</b>\n"
        f"👥 Выполнений: <b>{completions_count}/{max_completions}</b>\n"
        f"📊 Статус: {'✅ Активно' if task['is_active'] else '❌ Неактивно'}\n"
        f"📅 Создано: {task['created_at'][:16]}"
    )

    keyboard = InlineKeyboardBuilder()

    # Кнопки управления
    if task['is_active']:
        keyboard.button(text="⏸️ Приостановить", callback_data=f"deactivate_task_{task_id}")
    else:
        keyboard.button(text="▶️ Активировать", callback_data=f"activate_task_{task_id}")

    keyboard.button(text="✏️ Редактировать", callback_data=f"edit_task_{task_id}")
    keyboard.button(text="🗑️ Удалить", callback_data=f"delete_task_{task_id}")
    keyboard.button(text="📊 Статистика", callback_data=f"task_stats_{task_id}")
    keyboard.button(text="🔙 Назад", callback_data="manage_tasks")

    keyboard.adjust(2, 2, 1, 1)

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')
    await call.answer()

async def get_custom_task_completions_count(task_id):
    """Получает количество выполнений задания"""
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('SELECT COUNT(*) FROM custom_task_completions WHERE task_id = ?', (task_id,)) as cursor:
            return (await cursor.fetchone())[0]

@dp.callback_query(F.data.startswith("deactivate_task_"))
async def deactivate_task(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("deactivate_task_", ""))

    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('UPDATE custom_tasks SET is_active = FALSE WHERE id = ?', (task_id,))
        await db.commit()

    await call.answer("✅ Задание приостановлено")

    # Вместо изменения call.data, просто вызываем view_custom_task с новым callback
    await view_custom_task_handler(call, task_id)

@dp.callback_query(F.data.startswith("activate_task_"))
async def activate_task(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("activate_task_", ""))

    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('UPDATE custom_tasks SET is_active = TRUE WHERE id = ?', (task_id,))
        await db.commit()

    await call.answer("✅ Задание активировано")

    # Вместо изменения call.data, просто вызываем view_custom_task с новым callback
    await view_custom_task_handler(call, task_id)

async def view_custom_task_handler(call: types.CallbackQuery, task_id: int):
    """Вспомогательная функция для отображения задания"""
    task = await get_custom_task(task_id)

    if not task:
        await call.answer("❌ Задание не найдено")
        return

    # Получаем количество выполнений
    completions_count = await get_custom_task_completions_count(task_id)
    max_completions = task['max_completions'] if task['max_completions'] > 0 else '∞'

    text = (
        f"📝 <b>Информация о задании</b>\n\n"
        f"🆔 ID: <code>{task_id}</code>\n"
        f"📛 Название: {task['name']}\n"
        f"📄 Описание: {task.get('description', 'Нет описания')}\n"
        f"🔗 URL: {task['url']}\n"
        f"🔄 Текст кнопки: {task.get('button_text', 'Выполнить')}\n"
        f"💰 Цена: <b>{task['price']}$</b>\n"
        f"👥 Выполнений: <b>{completions_count}/{max_completions}</b>\n"
        f"📊 Статус: {'✅ Активно' if task['is_active'] else '❌ Неактивно'}\n"
        f"📅 Создано: {task['created_at'][:16]}"
    )

    keyboard = InlineKeyboardBuilder()

    # Кнопки управления
    if task['is_active']:
        keyboard.button(text="⏸️ Приостановить", callback_data=f"deactivate_task_{task_id}")
    else:
        keyboard.button(text="▶️ Активировать", callback_data=f"activate_task_{task_id}")

    keyboard.button(text="✏️ Редактировать", callback_data=f"edit_task_{task_id}")
    keyboard.button(text="🗑️ Удалить", callback_data=f"delete_task_{task_id}")
    keyboard.button(text="📊 Статистика", callback_data=f"task_stats_{task_id}")
    keyboard.button(text="🔙 Назад", callback_data="manage_tasks")

    keyboard.adjust(2, 2, 1, 1)

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')

@dp.callback_query(F.data.startswith("delete_task_"))
async def delete_task(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("delete_task_", ""))
    task = await get_custom_task(task_id)

    if not task:
        await call.answer("❌ Задание не найдено")
        return

    # Создаем клавиатуру для подтверждения удаления
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Да, удалить", callback_data=f"confirm_delete_{task_id}")
    keyboard.button(text="❌ Отмена", callback_data=f"view_task_{task_id}")
    keyboard.adjust(2)

    await call.message.edit_text(
        f"🗑️ <b>Подтверждение удаления</b>\n\n"
        f"Вы уверены, что хотите удалить задание?\n\n"
        f"📝 <b>{task['name']}</b>\n"
        f"💰 Цена: {task['price']}$\n"
        f"👥 Выполнений: {task['current_completions']}\n\n"
        f"<i>Это действие нельзя отменить!</i>",
        reply_markup=keyboard.as_markup(),
        parse_mode='HTML'
    )
    await call.answer()

@dp.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_task(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("confirm_delete_", ""))
    task = await get_custom_task(task_id)

    if not task:
        await call.answer("❌ Задание не найдено")
        return

    try:
        async with aiosqlite.connect('bot_database.db') as db:
            # Удаляем задание и связанные записи о выполнениях
            await db.execute('DELETE FROM custom_tasks WHERE id = ?', (task_id,))
            await db.execute('DELETE FROM custom_task_completions WHERE task_id = ?', (task_id,))
            await db.commit()

        await call.answer("✅ Задание успешно удалено")

        # Возвращаемся к управлению заданиями через прямой вызов
        await manage_tasks(call)

    except Exception as e:
        print(f"❌ Ошибка при удалении задания: {e}")
        await call.answer("❌ Ошибка при удалении задания", show_alert=True)

@dp.callback_query(F.data.startswith("cancel_delete_"))
async def cancel_delete_task(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("cancel_delete_", ""))

    # Возвращаемся к просмотру задания
    new_call = CallbackQuery(
        id=call.id,
        from_user=call.from_user,
        chat_instance=call.chat_instance,
        message=call.message,
        data=f"view_task_{task_id}"
    )
    await view_custom_task(new_call)
    await call.answer("❌ Удаление отменено")

async def create_custom_task(name, description, url, button_text, price, max_completions, task_type='subscribe_channel', channel_username=None, created_by=None):
    async with aiosqlite.connect('bot_database.db') as db:
        cursor = await db.execute('''
            INSERT INTO custom_tasks (name, description, url, button_text, price, max_completions, task_type, channel_username, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, description, url, button_text, price, max_completions, task_type, channel_username, created_by))
        await db.commit()
        return cursor.lastrowid

@dp.callback_query(F.data.startswith("task_stats_"))
async def task_stats_detail(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("task_stats_", ""))
    task = await get_custom_task(task_id)

    if not task:
        await call.answer("❌ Задание не найдено")
        return

    # Получаем детальную статистику выполнения
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('''
            SELECT u.user_id, u.username, ctc.completed_at 
            FROM custom_task_completions ctc 
            JOIN users u ON ctc.user_id = u.user_id 
            WHERE ctc.task_id = ? 
            ORDER BY ctc.completed_at DESC
        ''', (task_id,)) as cursor:
            completions = await cursor.fetchall()

    # Получаем общее количество выполнений
    total_completions = await get_custom_task_completions_count(task_id)

    text = (
        f"📊 <b>Статистика задания:</b> {task['name']}\n\n"
        f"🆔 ID задания: <code>{task_id}</code>\n"
        f"💰 Цена: {task['price']}$\n"
        f"👥 Всего выполнений: <b>{total_completions}</b>\n"
        f"🎯 Макс. выполнений: {task['max_completions'] if task['max_completions'] > 0 else '∞'}\n\n"
    )

    if completions:
        text += "<b>Последние выполнения:</b>\n"
        for i, completion in enumerate(completions[:15], 1):  # Показываем последние 15
            user_id, username, completed_at = completion
            text += f"{i}. ID: <code>{user_id}</code> | @{username} | {completed_at[:16]}\n"

        if len(completions) > 15:
            text += f"\n... и еще {len(completions) - 15} выполнений"
    else:
        text += "❌ Выполнений еще нет"

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад к заданию", callback_data=f"view_task_{task_id}")

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')
    await call.answer()

@dp.callback_query(F.data == "tasks_stats")
async def tasks_overall_stats(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    # Получаем общую статистику по всем заданиям
    async with aiosqlite.connect('bot_database.db') as db:
        # Общее количество заданий
        async with db.execute('SELECT COUNT(*) FROM custom_tasks') as cursor:
            total_tasks = (await cursor.fetchone())[0]

        # Активные задания
        async with db.execute('SELECT COUNT(*) FROM custom_tasks WHERE is_active = TRUE') as cursor:
            active_tasks = (await cursor.fetchone())[0]

        # Общее количество выполнений
        async with db.execute('SELECT COUNT(*) FROM custom_task_completions') as cursor:
            total_completions = (await cursor.fetchone())[0]

        # Общая сумма выплат за задания
        async with db.execute('''
            SELECT SUM(ct.price) 
            FROM custom_task_completions ctc 
            JOIN custom_tasks ct ON ctc.task_id = ct.id
        ''') as cursor:
            total_payout = (await cursor.fetchone())[0] or 0

        # Самые популярные задания
        async with db.execute('''
            SELECT ct.id, ct.name, COUNT(ctc.task_id) as completions
            FROM custom_tasks ct 
            LEFT JOIN custom_task_completions ctc ON ct.id = ctc.task_id
            GROUP BY ct.id 
            ORDER BY completions DESC 
            LIMIT 5
        ''') as cursor:
            popular_tasks = await cursor.fetchall()

    text = (
        "📊 <b>Общая статистика заданий</b>\n\n"
        f"📝 Всего заданий: <b>{total_tasks}</b>\n"
        f"✅ Активных заданий: <b>{active_tasks}</b>\n"
        f"👥 Всего выполнений: <b>{total_completions}</b>\n"
        f"💰 Общая сумма выплат: <b>{total_payout:.3f}$</b>\n\n"
    )

    if popular_tasks:
        text += "<b>🏆 Самые популярные задания:</b>\n"
        for i, (task_id, task_name, completions) in enumerate(popular_tasks, 1):
            text += f"{i}. {task_name[:20]}... - <b>{completions}</b> выполнений\n"

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data="manage_tasks")

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')
    await call.answer()

# Обработчики для каждого типа редактирования
@dp.callback_query(F.data.startswith("edit_name_"))
async def edit_name_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("edit_name_", ""))
    task = await get_custom_task(task_id)

    await state.update_data(edit_task_id=task_id, edit_field='name')
    await call.message.answer(f"✏️ Введите новое название задания:\n\nТекущее: {task['name']}")
    await state.set_state(AdminStates.waiting_edit_value)
    await call.answer()

@dp.callback_query(F.data.startswith("edit_desc_"))
async def edit_desc_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("edit_desc_", ""))
    task = await get_custom_task(task_id)

    await state.update_data(edit_task_id=task_id, edit_field='description')
    await call.message.answer(f"📝 Введите новое описание задания:\n\nТекущее: {task.get('description', 'Нет описания')}")
    await state.set_state(AdminStates.waiting_edit_value)
    await call.answer()

@dp.callback_query(F.data.startswith("edit_price_"))
async def edit_price_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("edit_price_", ""))
    task = await get_custom_task(task_id)

    await state.update_data(edit_task_id=task_id, edit_field='price')
    await call.message.answer(f"💰 Введите новую цену задания:\n\nТекущая: {task['price']}$")
    await state.set_state(AdminStates.waiting_edit_value)
    await call.answer()

@dp.callback_query(F.data.startswith("edit_limit_"))
async def edit_limit_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("edit_limit_", ""))
    task = await get_custom_task(task_id)

    await state.update_data(edit_task_id=task_id, edit_field='max_completions')
    max_completions = task['max_completions'] if task['max_completions'] > 0 else 'без ограничений'
    await call.message.answer(f"👥 Введите новое максимальное количество выполнений (0 - без ограничений):\n\nТекущее: {max_completions}")
    await state.set_state(AdminStates.waiting_edit_value)
    await call.answer()

@dp.callback_query(F.data.startswith("edit_url_"))
async def edit_url_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("edit_url_", ""))
    task = await get_custom_task(task_id)

    await state.update_data(edit_task_id=task_id, edit_field='url')
    await call.message.answer(f"🔗 Введите новый URL задания:\n\nТекущий: {task['url']}")
    await state.set_state(AdminStates.waiting_edit_value)
    await call.answer()

@dp.callback_query(F.data.startswith("edit_button_"))
async def edit_button_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("edit_button_", ""))
    task = await get_custom_task(task_id)

    await state.update_data(edit_task_id=task_id, edit_field='button_text')
    await call.message.answer(f"🔄 Введите новый текст кнопки:\n\nТекущий: {task.get('button_text', 'Выполнить')}")
    await state.set_state(AdminStates.waiting_edit_value)
    await call.answer()

# Общий обработчик для всех изменений
@dp.message(AdminStates.waiting_edit_value)
async def process_edit_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data['edit_task_id']
    field = data['edit_field']
    new_value = message.text.strip()

    try:
        async with aiosqlite.connect('bot_database.db') as db:
            if field == 'name':
                await db.execute('UPDATE custom_tasks SET name = ? WHERE id = ?', (new_value, task_id))
                field_name = "название"

            elif field == 'description':
                await db.execute('UPDATE custom_tasks SET description = ? WHERE id = ?', (new_value, task_id))
                field_name = "описание"

            elif field == 'price':
                new_price = float(new_value)
                if new_price <= 0:
                    await message.answer("❌ Цена должна быть больше 0")
                    return
                await db.execute('UPDATE custom_tasks SET price = ? WHERE id = ?', (new_price, task_id))
                field_name = "цена"

            elif field == 'max_completions':
                new_limit = int(new_value)
                if new_limit < 0:
                    await message.answer("❌ Количество выполнений не может быть отрицательным")
                    return
                await db.execute('UPDATE custom_tasks SET max_completions = ? WHERE id = ?', (new_limit, task_id))
                field_name = "лимит выполнений"

            elif field == 'url':
                await db.execute('UPDATE custom_tasks SET url = ? WHERE id = ?', (new_value, task_id))
                field_name = "URL"

            elif field == 'button_text':
                await db.execute('UPDATE custom_tasks SET button_text = ? WHERE id = ?', (new_value, task_id))
                field_name = "текст кнопки"

            await db.commit()

        await message.answer(f"✅ {field_name} успешно обновлен!")

        # Создаем кнопку для возврата к заданию
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="📝 Посмотреть задание", callback_data=f"view_task_{task_id}")
        keyboard.button(text="⚙️ Управление заданиями", callback_data="manage_tasks")
        keyboard.adjust(1)

        await message.answer("Выберите действие:", reply_markup=keyboard.as_markup())

    except ValueError as e:
        if field == 'price':
            await message.answer("❌ Введите корректное число для цены (например: 0.015 или 1.0)")
        elif field == 'max_completions':
            await message.answer("❌ Введите корректное число для лимита выполнений")
        else:
            await message.answer(f"❌ Ошибка при обновлении: {e}")
    except Exception as e:
        await message.answer(f"❌ Ошибка при обновлении: {e}")

    await state.clear()

def _task_check_success_text(completed: int, total_reward: float) -> str:
    """Единый дизайн успешной проверки для всех трёх категорий заданий."""
    return (
        '<tg-emoji emoji-id="5411126895620167819">🔍</tg-emoji> '
        '<b>Проверка завершена!</b>\n\n'
        f'<tg-emoji emoji-id="5362084755716214813">📊</tg-emoji> '
        f'Выполнено заданий: <b>{completed}</b>\n'
        f'<tg-emoji emoji-id="5309795500277403547">💫</tg-emoji> '
        f'Начислено: <b>{total_reward:.3f}$</b>\n\n'
        '<tg-emoji emoji-id="5462911651315468143">🔒</tg-emoji> '
        'Средства заморожены на 24 часов'
    )


def _task_check_empty_text() -> str:
    """Обычное сообщение без HTML-тегов и premium emoji."""
    return (
        "❌ Выполненных подписок не найдено.\n"
        "Подпишись через кнопки из списка и нажми проверку."
    )

async def _edit_task_check_result(message: types.Message, text: str) -> None:
    """Обновляет экран проверки и спокойно обрабатывает повторное нажатие."""
    try:
        await message.edit_text(text, parse_mode="HTML")
    except Exception as e:
        if "message is not modified" in str(e).lower():
            return
        raise


@dp.callback_query(F.data == "check_all_tasks")
async def check_all_tasks(call: types.CallbackQuery):
    user_id = call.from_user.id
    print("[TASK-CHECK-FIX-20261114] check_all_tasks handler active", flush=True)

    # Подтверждаем callback сразу: проверка может занимать несколько секунд.
    await call.answer()

    try:
        completed_tasks = 0
        total_reward = 0

        # Получаем ВСЕ задания заново
        flyer_tasks = await flyer_get_tasks(user_id, 'ru', limit=20)
        custom_tasks = await get_active_custom_tasks()

        print(f"🔍 Получено Flyer заданий: {len(flyer_tasks)}")

        # Проверяем Flyer задания - САМОСТОЯТЕЛЬНАЯ ПРОВЕРКА ПОДПИСКИ
        # Проверяем Flyer задания - ПРОСТАЯ ЛОГИКА
        if flyer_tasks:
            for task in flyer_tasks:
                signature = task.get('signature', '')
                task_name = task.get('name', 'Unknown')

                if signature:
                    print(f"🔍 Проверяем Flyer задание: {task_name}")

                    # ПРОВЕРЯЕМ ЧЕРЕЗ FLYER STATUS
                    status = await flyer_check_task(signature, user_id)
                    print(f"🔍 Flyer статус: {status}")

                    # В этом проекте waiting означает "принято на проверку":
                    # по исходной логике за него начисляется замороженная награда.
                    if status in [True, "completed", "done", "complete", "waiting"]:
                        print(f"✅ Flyer принял задание: {status}")

                        saved_status = await get_task_status(user_id, signature)
                        if saved_status not in ["completed", "done", "complete", "waiting"]:
                            reward = float(FLZAD)

                            balance_ok = await update_user_balance(user_id, frozen_amount=reward)
                            tasks_ok = await update_user_balance(user_id, completed_tasks=1)
                            if not balance_ok or not tasks_ok:
                                raise RuntimeError(
                                    f"Не удалось сохранить награду Flyer для {signature}"
                                )

                            await add_frozen_transaction(user_id, reward, "core.flyer", signature)
                            await set_task_status(
                                user_id,
                                signature,
                                "waiting" if status == "waiting" else "completed",
                            )

                            completed_tasks += 1
                            total_reward += reward
                            print(f"✅ Заморожено {reward:.3f}$ за задание: {task_name}")
                        else:
                            print(f"⚠️ Задание уже принято/оплачено ранее: {task_name}")
                    else:
                        print(
                            f"ℹ️ Flyer не подтвердил выполнение {task_name}; "
                            f"статус={status!r}. Награда не начислена."
                        )
                        
            # Проверяем задания PiarFlow
        try:
            pf_done, pf_reward = await piarflow_check_and_complete(user_id)
            completed_tasks += pf_done
            total_reward += pf_reward
        except Exception as e:
            print(f"⚠️ PiarFlow check error: {e}")

        # Проверка кастомных заданий
        if custom_tasks:
            for task in custom_tasks:
                if task['task_type'] == 'subscribe_channel' and task['channel_username']:
                    try:
                        print(f"🔍 Проверяем кастомное задание: {task['name']}")

                        chat_member = await bot.get_chat_member(f"@{task['channel_username']}", user_id)

                        if chat_member.status in ['member', 'administrator', 'creator']:
                            async with aiosqlite.connect('bot_database.db') as db:
                                async with db.execute('SELECT 1 FROM custom_task_completions WHERE task_id = ? AND user_id = ?', (task['id'], user_id)) as cursor:
                                    has_completed = await cursor.fetchone()

                            if not has_completed:
                                completed_tasks += 1
                                reward = task['price']
                                total_reward += reward

                                await complete_custom_task(task['id'], user_id)
                                await update_user_balance(user_id, frozen_amount=reward)
                                await update_user_balance(user_id, completed_tasks=1)
                                await add_frozen_transaction(user_id, reward, 'custom_subscribe', task['id'])

                                print(f"✅ Кастомное задание выполнено: {task['name']}")

                    except Exception as e:
                        print(f"❌ Ошибка проверки кастомного задания: {e}")

        # Результат
        if completed_tasks > 0:
            asyncio.create_task(show_ad_at_opportunity(user_id, "after_task"))
            await _edit_task_check_result(
                call.message,
                _task_check_success_text(completed_tasks, total_reward),
            )

        else:
            empty_text = _task_check_empty_text()
            print("[TASK-CHECK-FIX-20261114] sending exact empty-subscriptions text", flush=True)
            await _edit_task_check_result(call.message, empty_text)

    except Exception as e:
        print(f"❌ Ошибка проверки всех заданий: {e}")
        try:
            await call.message.edit_text(
                "⚠️ Не удалось проверить задания. Попробуй ещё раз позже.",
                parse_mode="HTML",
            )
        except Exception as edit_error:
            print(f"⚠️ Не удалось показать ошибку проверки: {edit_error}")

@dp.callback_query(F.data == "execute_mass_delete")
async def execute_mass_delete(call: types.CallbackQuery):
    """Выполняет массовое удаление неактивных пользователей"""
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.answer()
    await call.message.edit_text("🗑️ <b>Начинаю массовое удаление...</b>", parse_mode='HTML')

    inactive_users = await get_inactive_users()
    deleted_count = 0

    if not inactive_users:
        await call.message.edit_text("❌ Нет пользователей для удаления", parse_mode='HTML')
        return

    try:
        async with aiosqlite.connect('bot_database.db') as db:
            for user in inactive_users:
                user_id = user['user_id']

                # Удаляем пользователя и связанные данные
                await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
                await db.execute("DELETE FROM completed_tasks WHERE user_id = ?", (user_id,))
                await db.execute("DELETE FROM user_notifications WHERE user_id = ?", (user_id,))
                await db.execute("DELETE FROM frozen_transactions WHERE user_id = ?", (user_id,))
                await db.execute("DELETE FROM custom_task_completions WHERE user_id = ?", (user_id,))
                await db.execute("DELETE FROM user_task_completions WHERE user_id = ?", (user_id,))

                deleted_count += 1

            await db.commit()

        # Обновляем реферальные связи для оставшихся пользователей
        await cleanup_referral_links()

        await call.message.edit_text(
            f"✅ <b>Массовое удаление завершено!</b>\n\n"
            f"🗑️ Удалено пользователей: <b>{deleted_count}</b>\n"
            f"📊 Осталось неактивных: <b>0</b>\n\n"
            f"<i>Все неактивные пользователи удалены из базы данных</i>",
            parse_mode='HTML'
        )

    except Exception as e:
        await call.message.edit_text(
            f"❌ <b>Ошибка при удалении:</b>\n\n{str(e)}",
            parse_mode='HTML'
        )


# ===== handlers_admin.py =====
# -*- coding: utf-8 -*-
"""Админ-панель: настройки, рассылки, управление юзерами."""




@dp.message(F.text == "💰 Баланс резерва")
async def admin_balance_btn(message: Message):
    await asyncio.sleep(0.1)

    total_balance = await get_crypto_bot_balance()

    # Определяем статус резерва
    if total_balance < 1:
        reserve_status = "🚨 КРИТИЧЕСКИ НИЗКИЙ"
        reserve_emoji = "🔴"
        recommendation = "❌ СРОЧНО ПОПОЛНИТЕ РЕЗЕРВ!"
    elif total_balance < 1:
        reserve_status = "⚠️ НИЗКИЙ"
        reserve_emoji = "🟡"
        recommendation = "💡 Рекомендуется пополнить резерв"
    elif total_balance < 5:
        reserve_status = "ℹ️ СРЕДНИЙ"
        reserve_emoji = "🟢"
        recommendation = "✅ Резерв в норме"
    else:
        reserve_status = "✅ ДОСТАТОЧНЫЙ"
        reserve_emoji = "💚"
        recommendation = "🎉 Отличный запас!"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="<tg-emoji emoji-id='5456348576214983692'>💵</tg-emoji> Пополнить резерв", callback_data="reserve_topup")]
    ])

    await message.answer(
        f"💰 <b>БАЛАНС СИСТЕМЫ</b>\n\n"
        f"💵 Текущий баланс: <b>{total_balance:.2f} USDT</b>\n"
        f"📊 Статус: {reserve_emoji} <b>{reserve_status}</b>\n\n"
        f"<i>{recommendation}</i>",
        parse_mode='HTML',
        reply_markup=kb
    )


@dp.callback_query(F.data == "reserve_topup")
async def admin_topup_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    total_balance = await get_crypto_bot_balance()

    await call.message.edit_text(
        f"💵 <b>Пополнение резерва системы</b>\n\n"
        f"💰 Текущий баланс кошелька: <b>{total_balance:.2f} USDT</b>\n\n"
        f"Введите сумму в USDT для пополнения:",
        parse_mode='HTML'
    )
    await state.set_state(ReserveTopupStates.waiting_for_amount)

@dp.message(ReserveTopupStates.waiting_for_amount)
async def process_topup_amount(message: Message, state: FSMContext):
    """Обрабатывает введенную сумму для пополнения"""
    try:
        amount = float(message.text.strip())

        if amount <= 0:
            await message.answer("❌ Сумма должна быть больше 0")
            return

        if amount < 1:
            await message.answer("❌ Минимальная сумма пополнения: 1 USDT")
            return

        # Создаем инвойс для пополнения
        invoice_url = await create_reserve_topup_invoice(amount, message.from_user.id)

        if invoice_url and invoice_url != "https://t.me/CryptoBot":
            await message.answer(
                f"💳 <b>ИНВОЙС ДЛЯ ПОПОЛНЕНИЯ РЕЗЕРВА</b>\n\n"
                f"💰 Сумма: <b>{amount:.2f} USDT</b>\n"
                f"📎 Ссылка для оплаты: {invoice_url}\n\n"
                f"⚠️ <i>После оплаты средства поступят в резерв системы.</i>\n"
                f"🔧 <i>Используется тот же кошелек, что и для выплат.</i>",
                reply_markup=get_admin_keyboard(),
                parse_mode='HTML'
            )

            # Уведомляем других админов
            for admin_id in ADMIN_IDS:
                if admin_id != message.from_user.id:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"👤 Админ @{message.from_user.username or 'N/A'} создал инвойс\n"
                            f"💰 Сумма: {amount:.2f} USDT\n"
                            f"💳 Для пополнения резерва",
                            parse_mode='HTML'
                        )
                    except:
                        pass

        else:
            await message.answer("❌ Не удалось создать инвойс. Попробуйте позже.")

    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректную сумму (например: 10.5)")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

    await state.clear()

async def _send_promotion_menu(message: types.Message, user_id: int):
    """Формирует меню продвижения по свежим данным; учитывает только каналы и чаты."""
    user = await get_user(user_id)
    promo_price = float(await get_setting("promotion_price") or 0.015)
    ad_balance = float(user.get("ad_balance", 0) or 0)
    possible = int(ad_balance // promo_price) if promo_price > 0 else 0
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute(
            "SELECT COUNT(*) FROM user_tasks WHERE created_by=? AND is_active=1 "
            "AND task_type IN ('channel', 'chat')",
            (user_id,)
        ) as cursor:
            active_count = int((await cursor.fetchone())[0] or 0)
        async with db.execute(
            "SELECT COUNT(*) FROM user_tasks WHERE created_by=? AND is_active=0 "
            "AND task_type IN ('channel', 'chat')",
            (user_id,)
        ) as cursor:
            done_count = int((await cursor.fetchone())[0] or 0)

    text = (
        '<tg-emoji emoji-id="5413822103562524971">📢</tg-emoji> <b>Создать задания</b>\n\n'
        f'<tg-emoji emoji-id="5309795500277403547">💰</tg-emoji> 1 задание — <b>{promo_price:.3f} $</b>\n'
        f'<tg-emoji emoji-id="5312057711091813718">💳</tg-emoji> Рекламный баланс — <b>{ad_balance:.3f} $</b>\n'
        f'<tg-emoji emoji-id="5362084755716214813">📊</tg-emoji> Его хватит на <b>{possible}</b> выполнений\n\n'
        f'<tg-emoji emoji-id="5258258882022612173">📈</tg-emoji> Активных: <b>{active_count}</b>  |  '
        f'<tg-emoji emoji-id="5258419835922030550">✅</tg-emoji> Завершённых: <b>{done_count}</b>\n\n'
        '<blockquote>Наш бот <a href="https://t.me/UAMONEY_BOT">@UAMONEY_BOT</a> должен быть администратором продвигаемого проекта!</blockquote>'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Создать задание",
            callback_data="promo_create",
            icon_custom_emoji_id="5318863168376709909",
            style="primary",
        )],
        [InlineKeyboardButton(
            text="Активные заказы",
            callback_data="promo_active_page_1",
            icon_custom_emoji_id="5318810984524060823",
        )],
        [InlineKeyboardButton(
            text="Завершённые заказы",
            callback_data="promo_done_page_1",
            icon_custom_emoji_id="5312043357311111246",
        )]
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


@dp.message(F.text == "Продвижение")
async def promotion_menu(message: types.Message, state: FSMContext):
    # Повторный вход в «Продвижение» начинает раздел с чистого состояния.
    await state.clear()
    _ensure_promo_pause_monitor_started()
    await _refresh_promo_task_pauses()
    await _send_promotion_menu(message, message.from_user.id)

@dp.message(Command("cancel"), PromotionStates.waiting_channel_link)
async def promo_cancel_waiting_link(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "❌ Создание задания отменено. Теперь можно пользоваться другими разделами бота."
    )


@dp.message(
    PromotionStates.waiting_channel_link,
    F.text.regexp(r"^(?:@[A-Za-z0-9_]{5,32}|https?://t\.me/[A-Za-z0-9_]{5,32}/?)$")
)
async def promo_receive_channel_link(message: types.Message, state: FSMContext):
    link = (message.text or "").strip()
    data = await state.get_data()
    task_type = data.get("task_type", "channel")

    if not link:
        await message.answer("❌ Отправьте ссылку на публичный канал или чат.")
        return

    # Для автоматической проверки членства нужен публичный username.
    if link.startswith("@"):
        target_username = link[1:]
    elif link.startswith(("https://t.me/", "http://t.me/")):
        target_username = link.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
    else:
        target_username = ""

    if (not target_username or "/" in target_username
            or target_username.startswith("+")
            or target_username.startswith("joinchat")
            or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", target_username)):
        if task_type == "chat":
            await message.answer(
                "❌ Нужна публичная ссылка на чат вида https://t.me/имя_чата или @имя_чата.\n"
                "Ссылки-приглашения t.me/+... не подходят, потому что бот не сможет проверить вступление."
            )
        else:
            await message.answer(
                "❌ Нужна публичная ссылка вида https://t.me/имя_канала или @имя_канала."
            )
        return

    # Приводим короткую запись @username к обычной ссылке Telegram,
    # чтобы все последующие кнопки и экраны работали одинаково.
    if link.startswith("@"):
        link = f"https://t.me/{target_username}"

    api_username = f"@{target_username}"
    try:
        chat = await bot.get_chat(api_username)
        expected_types = ("group", "supergroup") if task_type == "chat" else ("channel",)
        if chat.type not in expected_types:
            if task_type == "chat":
                await message.answer("❌ Эта ссылка ведёт не на группу/чат. Отправьте ссылку на публичный групповой чат.")
            else:
                await message.answer("❌ Эта ссылка ведёт не на канал. Выберите тип «Канал» или отправьте ссылку на канал.")
            return

        admin_status = await _promo_bot_admin_status(target_username)
        if admin_status is not True:
            if admin_status is False:
                await message.answer(
                    _promo_admin_missing_text(task_type),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            else:
                object_name = "чате" if task_type == "chat" else "канале"
                await message.answer(
                    f"⚠️ Не удалось проверить права бота в {object_name}. "
                    "Попробуйте ещё раз чуть позже."
                )
            return

    except Exception as exc:
        print(f"⚠️ Ошибка проверки объекта продвижения {link}: {exc}")
        object_name = "чата" if task_type == "chat" else "канала"
        await message.answer(
            f"❌ Не удалось проверить {object_name}. Убедитесь, что ссылка публичная, объект доступен боту, "
            "а бот добавлен в него администратором.\n"
            "Приватные ссылки-приглашения не поддерживаются."
        )
        return

    await state.update_data(link=link, username=target_username, task_type=task_type)
    if task_type == "chat":
        await message.answer("✏️ Введите количество вступлений в чат (минимум 10):")
    else:
        await message.answer("✏️ Введите количество подписок (минимум 10):")
    await state.set_state(PromotionStates.waiting_count)

@dp.message(PromotionStates.waiting_count)
async def promo_count(message: types.Message, state: FSMContext):
    try:
        count = int(message.text.strip())
        if count < 10:
            await message.answer("❌ Минимум 10 выполнений.")
            return

        data = await state.get_data()
        link = data["link"]
        username = data["username"]
        task_type = data.get("task_type", "channel")
        if task_type not in ("channel", "chat"):
            await state.clear()
            await message.answer("❌ Создание заданий для ботов отключено. Доступны только каналы и чаты.")
            return

        promo_price = float(await get_setting("promotion_price") or 0.015)
        total = count * promo_price
        user = await get_user(message.from_user.id)

        if user["ad_balance"] < total:
            await message.answer("❌ Недостаточно средств на рекламном балансе.")
            await state.clear()
            return

        # Определяем текст в зависимости от типа
        if task_type == "channel":
            task_info = f"📢 <b>Канал:</b> {link}\n"
        elif task_type == "chat":
            task_info = f"💬 <b>Чат:</b> {link}\n"
        else:
            await state.clear()
            await message.answer("❌ Этот тип задания больше не поддерживается.")
            return

        task_info += f"👥 <b>Количество:</b> {count}\n"

        # Для каналов и чатов бот должен иметь права администратора для проверки вступления.
        if task_type in ("channel", "chat"):
            me = await bot.get_me()
            task_info += f"👨‍💼 <b>Админ:</b> @{me.username} должен быть администратором\n"

        preview = (
            f"📋 <b>Подтверждение задания</b>\n\n"
            f"{task_info}"
            f"💵 <b>Стоимость:</b> {total:.3f} $\n\n"
            f"<i>Средства будут списаны с рекламного баланса</i>"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Подтвердить",
                callback_data="promo_confirm",
                icon_custom_emoji_id="5411126895620167819",
            )],
            [InlineKeyboardButton(
                text="Назад",
                callback_data="promo_cancel",
                icon_custom_emoji_id="5411532233158726966",
            )]
        ])
        await state.update_data(count=count, total=total, unit_price=promo_price)
        await message.answer(preview, parse_mode="HTML", reply_markup=kb)
        await state.set_state(PromotionStates.waiting_confirm)
    except:
        await message.answer("❌ Введите корректное число.")



@dp.callback_query(F.data == "check_user_tasks")
async def check_user_tasks(call: types.CallbackQuery):
    """Проверяет только вступление в продвигаемые каналы и чаты."""
    user_id = call.from_user.id
    _ensure_promo_pause_monitor_started()
    await _refresh_promo_task_pauses()
    await call.answer()
    completed = 0
    total_reward = 0.0
    reward = float(await get_setting("user_task_reward") or 0.015)

    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_tasks "
            "WHERE is_active = TRUE AND task_type IN ('channel', 'chat') "
            "AND COALESCE(is_paused, 0) = 0 "
            "AND current_completions < max_completions "
            "AND id NOT IN (SELECT task_id FROM user_task_completions WHERE user_id = ?)",
            (user_id,),
        ) as cursor:
            tasks = await cursor.fetchall()

    for task in tasks:
        try:
            task_type = task["task_type"] if "task_type" in task.keys() else "channel"
            if task_type not in ("channel", "chat"):
                continue
            admin_status = await _promo_bot_admin_status(task["channel_username"])
            if admin_status is not True:
                if admin_status is False:
                    async with aiosqlite.connect("bot_database.db") as db:
                        await db.execute("UPDATE user_tasks SET is_paused = 1 WHERE id = ?", (task["id"],))
                        await db.commit()
                continue

            should_reward = await _promo_target_is_member(user_id, task["channel_username"])
            if not should_reward:
                continue

            newly_completed = False
            async with aiosqlite.connect("bot_database.db") as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO user_task_completions (task_id, user_id) "
                    "SELECT ?, ? WHERE EXISTS (SELECT 1 FROM user_tasks "
                    "WHERE id = ? AND is_active = TRUE "
                    "AND task_type IN ('channel', 'chat') "
                    "AND current_completions < max_completions "
                    "AND COALESCE(is_paused, 0) = 0)",
                    (task["id"], user_id, task["id"]),
                )
                if cursor.rowcount == 1:
                    update_cursor = await db.execute(
                        "UPDATE user_tasks SET current_completions = current_completions + 1 "
                        "WHERE id = ? AND is_active = TRUE "
                        "AND task_type IN ('channel', 'chat') "
                        "AND current_completions < max_completions "
                        "AND COALESCE(is_paused, 0) = 0",
                        (task["id"],),
                    )
                    newly_completed = update_cursor.rowcount == 1
                    if newly_completed:
                        await db.execute(
                            "UPDATE user_tasks SET is_active = FALSE "
                            "WHERE id = ? AND current_completions >= max_completions",
                            (task["id"],),
                        )
                    else:
                        await db.execute(
                            "DELETE FROM user_task_completions WHERE task_id = ? AND user_id = ?",
                            (task["id"], user_id),
                        )
                await db.commit()

            if newly_completed:
                await update_user_balance(user_id, frozen_amount=reward, completed_tasks=1)
                await add_frozen_transaction(user_id, reward, 'user_task', task["id"])
                completed += 1
                total_reward += reward
        except Exception as exc:
            print(f"⚠️ Ошибка при проверке задания: {exc}")
            continue

    if completed > 0:
        asyncio.create_task(show_ad_at_opportunity(user_id, "after_task"))
        result_text = _task_check_success_text(completed, total_reward)
        await _edit_task_check_result(call.message, result_text)
    else:
        await _edit_task_check_result(call.message, _task_check_empty_text())

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject = None):
    _ensure_promo_pause_monitor_started()
    user_id = message.from_user.id
    # Гарантированно создаём пользователя в users: core.get_user() только читает,
    # и без этого начисления Pulsar/PiarFlow для нового пользователя теряются.
    await get_user(user_id)

    # Реферальная система
    if command and command.args:
        try:
            referrer_id = int(command.args)
            if referrer_id != user_id:
                user = await get_user(user_id)
                if not user['referrer_id']:
                    async with aiosqlite.connect('bot_database.db') as db:
                        await db.execute(
                            'UPDATE users SET referrer_id = ? WHERE user_id = ?',
                            (referrer_id, user_id)
                        )
                        await db.commit()
        except ValueError:
            pass

    # Проверка блокировки
    if await check_user_blocked(user_id):
        await message.answer("❌ Ваш аккаунт заблокирован. Обратитесь к администратору.")
        return

    # Атрибуция Pulsar: фиксируем /start для заданий №4.
    asyncio.create_task(
        pulsar_record_start(user_id, (getattr(message.from_user, "language_code", None) or "ru")[:16])
    )

    kb = get_main_keyboard(user_id)
    
    photo = FSInputFile("welcome.png")

    await message.answer_photo(
        photo,
        caption=(
        "<tg-emoji emoji-id='5456348576214983692'>💲</tg-emoji> UA. - Бот для заработка на выполнении заданий и приглашении друзей\n\n"
        "Наш чат - <a href='https://t.me/UAMONEYCHAT'>кликабельно</a>\n\n"
        "Следите за обновлениями и новостями UA. в официальном <a href='https://t.me/UAMONY'>канале ›</a>\n\n"
        "<tg-emoji emoji-id='5411418403640484720'>⚙️</tg-emoji> Руководство пользователя (Частые вопросы)\n"
        "<blockquote>"
        "•<a href='https://telegra.ph/Skolko-ya-mogu-zarabotat-11-08'>Сколько я могу заработать?</a>\n"
        "•<a href='https://telegra.ph/Kak-vyvesti-sredstva-11-08'>Как вывести средства?</a>\n"
        "•<a href='https://telegra.ph/U-menya-net-zadaniya-11-08'>У меня нет задания</a>\n"
        "</blockquote>"
        ),
        reply_markup=kb,
        parse_mode="HTML",
    )

@dp.message(F.text == "Админка")
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав администратора.")
        return

    await message.answer("👨‍💻 <b>Админ панель</b>", reply_markup=get_admin_keyboard(), parse_mode='HTML')

@dp.message(F.text == "👥 Пользователи")
async def admin_users(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="📋 Все пользователи", callback_data="all_users:0:tasks:desc")
    keyboard.button(text="🔍 Найти пользователя", callback_data="find_user")
    keyboard.button(text="📊 Статистика", callback_data="users_stats")
    keyboard.adjust(1, 2)

    users = await get_all_users()
    total_users = len(users)

    text = f"👥 <b>Всего пользователей: {total_users}</b>\n\n" \
           f"Нажми <b>📋 Все пользователи</b> чтобы открыть список."

    await message.answer(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "find_user")
async def find_user_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.message.answer("🔍 Введите ID пользователя для поиска:")
    await state.set_state(AdminStates.waiting_user_id)
    await call.answer()


@dp.callback_query(F.data.startswith("add_balance_"))
async def add_balance_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    user_id = int(call.data.split("_")[2])
    await state.update_data(user_id=user_id, action="add")
    await call.message.answer(f"💵 Введите сумму для начисления пользователю {user_id}:")
    await state.set_state(AdminStates.waiting_balance_amount)
    await call.answer()

@dp.callback_query(F.data.startswith("remove_balance_"))
async def remove_balance_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    user_id = int(call.data.split("_")[2])
    await state.update_data(user_id=user_id, action="remove")
    await call.message.answer(f"💵 Введите сумму для снятия с пользователя {user_id}:")
    await state.set_state(AdminStates.waiting_balance_amount)
    await call.answer()

@dp.message(AdminStates.waiting_balance_amount)
async def process_balance_change(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data['user_id']
    action = data['action']

    try:
        amount = float(message.text)
        if amount <= 0:
            await message.answer("❌ Сумма должна быть больше 0")
            await state.clear()
            return

        user = await get_user(user_id)

        if action == "remove" and amount > user['balance']:
            await message.answer("❌ Недостаточно средств на балансе пользователя")
            await state.clear()
            return

        # Применяем изменение баланса
        change_amount = amount if action == "add" else -amount
        await update_user_balance(user_id, amount=change_amount)

        # Записываем в историю
        async with aiosqlite.connect('bot_database.db') as db:
            await db.execute(
                'INSERT INTO balance_history (user_id, amount, type, admin_id, reason) VALUES (?, ?, ?, ?, ?)',
                (user_id, change_amount, 'admin_adjustment', message.from_user.id, 
                 f"{'Начисление' if action == 'add' else 'Списание'} администратором")
            )
            await db.commit()

        action_text = "начислено" if action == "add" else "списано"
        await message.answer(f"✅ Пользователю {user_id} {action_text} {amount:.3f}$")

        # Уведомляем пользователя
        try:
            await bot.send_message(
                user_id,
                f"📢 <b>Изменение баланса</b>\n\n"
                f"💵 Сумма: <b>{amount:.3f}$</b>\n"
                f"📝 Тип: {'Начисление' if action == 'add' else 'Списание'}\n"
                f"💰 Новый баланс: <b>{(user['balance'] + change_amount):.3f}$</b>",
                parse_mode='HTML'
            )
        except:
            pass

        await state.clear()

    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.callback_query(F.data.startswith("ban_"))
async def ban_user(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    user_id = int(call.data.split("_")[1])
    await state.update_data(user_id=user_id)
    await call.message.answer(f"📝 Укажите причину блокировки пользователя {user_id}:")
    await state.set_state(AdminStates.waiting_ban_reason)
    await call.answer()

@dp.callback_query(F.data.startswith("unban_"))
async def unban_user(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    user_id = int(call.data.split("_")[1])

    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('UPDATE users SET is_blocked = FALSE WHERE user_id = ?', (user_id,))
        await db.commit()

    await call.message.answer(f"✅ Пользователь {user_id} разблокирован")

    # Уведомляем пользователя
    try:
        await bot.send_message(user_id, "✅ Ваш аккаунт разблокирован администратором")
    except:
        pass

    await call.answer()

@dp.message(Command("debug_penalties"))
async def debug_penalties(message: types.Message):
    """Диагностика системы штрафов"""
    if message.from_user.id not in ADMIN_IDS:
        return

    try:
        # Проверяем есть ли пользователи с заморозкой
        async with aiosqlite.connect('bot_database.db') as db:
            async with db.execute('SELECT COUNT(*) FROM users WHERE frozen_balance > 0') as cursor:
                users_with_frozen = (await cursor.fetchone())[0]

            async with db.execute('SELECT COUNT(*) FROM frozen_transactions WHERE is_unfrozen = FALSE') as cursor:
                active_transactions = (await cursor.fetchone())[0]

        text = (
            f"🔍 <b>Диагностика штрафов</b>\n\n"
            f"👥 Пользователей с заморозкой: <b>{users_with_frozen}</b>\n"
            f"💸 Активных транзакций: <b>{active_transactions}</b>\n"
            f"🔧 Flyer статус: <b>{'✅ Включен' if core.flyer_enabled else '❌ Выключен'}</b>\n"
            f"🔄 Проверка отписок: <b>{'✅ Запущена' if 'check_completed_tasks' in globals() else '❌ Не найдена'}</b>"
        )

        await message.answer(text, parse_mode='HTML')

    except Exception as e:
        await message.answer(f"❌ Ошибка диагностики: {e}")

@dp.message(AdminStates.waiting_ban_reason)
async def process_ban_reason(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data['user_id']
    reason = message.text

    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('UPDATE users SET is_blocked = TRUE WHERE user_id = ?', (user_id,))
        await db.commit()

    await message.answer(f"✅ Пользователь {user_id} заблокирован\nПричина: {reason}")

    # Уведомляем пользователя
    try:
        await bot.send_message(
            user_id,
            f"❌ <b>Ваш аккаунт заблокирован</b>\n\n"
            f"📝 Причина: {reason}\n"
            f"🔄 Для разблокировки обратитесь к администратору",
            parse_mode='HTML'
        )
    except:
        pass

    await state.clear()

@dp.callback_query(F.data.startswith("show_refs_"))
async def show_user_referrals(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    user_id = int(call.data.split("_")[2])

    # Получаем рефералов 1 уровня
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('SELECT user_id, username, registered_at FROM users WHERE referrer_id = ?', (user_id,)) as cursor:
            level1_refs = await cursor.fetchall()

    text = f"👥 <b>Рефералы пользователя {user_id}</b>\n\n"
    text += f"📊 Рефералы 1 уровня: {len(level1_refs)}\n\n"

    if level1_refs:
        text += "<b>Список рефералов:</b>\n"
        for ref in level1_refs[:20]:  # Ограничиваем показ
            ref_user = await get_user(ref[0])
            text += f"👤 <code>{ref[0]}</code> | @{ref[1]} |📝 {ref_user['completed_tasks']}\n"
    else:
        text += "❌ Рефералов нет"

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔙 Назад", callback_data=f"back_to_user_{user_id}")

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')
    await call.answer()

# Обработчик кнопки "Назад" в меню пользователей
@dp.callback_query(F.data == "users_back")
async def users_back(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    # Возвращаемся к списку пользователей
    users = await get_all_users()
    total_users = len(users)

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔍 Найти пользователя", callback_data="find_user")
    keyboard.button(text="📊 Статистика", callback_data="users_stats")
    keyboard.adjust(1)

    text = f"👥 <b>Всего пользователей: {total_users}</b>\n\n"
    text += "<b>Последние 10 пользователей:</b>\n"

    for user in users[:10]:
        user_data = await get_user(user[0])
        text += f"👤 ID: <code>{user[0]}</code> | 💰 {user_data['balance']:.2f}$ | 📝 {user_data['completed_tasks']} заданий\n"

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')
    await call.answer()

# Обработчик кнопки "Назад к пользователю"
@dp.callback_query(F.data.startswith("back_to_user_"))
async def back_to_user(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    user_id = int(call.data.split("_")[3])
    await show_user_details(call, user_id)
    await call.answer()

@dp.callback_query(F.data == "flyer_settings")
async def flyer_settings(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    # Получаем информацию о текущем ключе
    current_key = core.FLYER_API_KEY
    key_display = f"{current_key[:8]}...{current_key[-4:]}" if current_key and len(current_key) > 12 else "Не настроен"

    # Проверяем статус API
    status = "✅ Активен" if core.flyer_enabled else "❌ Неактивен"

    text = (
        "🔑 <b>НАСТРОЙКИ FLYER API</b>\n\n"
        f"📊 Текущий ключ: <code>{key_display}</code>\n"
        f"🔄 Статус: {status}\n\n"
        "💡 <b>Функции Flyer API:</b>\n"
        "• Проверка подписки на каналы\n"
        "• Проверка выполнения заданий\n"
        "• Защита от ботов\n\n"
        "<b>Выберите действие:</b>"
    )

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🧪 Тест API", callback_data="test_flyer_api")
    keyboard.button(text="🔙 Назад", callback_data="admin_settings_back")
    keyboard.adjust(1)

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')
    await call.answer()

@dp.callback_query(F.data == "test_flyer_api")
async def test_flyer_api(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.message.edit_text("🧪 <b>Тестируем Flyer API...</b>", parse_mode='HTML')

    test_results = []

    try:
        # Тест 1: Получение заданий
        test_results.append("🔍 Получение заданий...")
        tasks = await flyer_get_tasks(call.from_user.id, 'ru', limit=1)
        if tasks is not None:
            if tasks:
                test_results.append(f"✅ Получено {len(tasks)} заданий")
                # Показываем первое задание
                if tasks[0]:
                    task = tasks[0]
                    test_results.append(f"📝 Задание: {task.get('name', 'Название')}")
                    test_results.append(f"💰 Цена: {task.get('price', '0')}$")
                    test_results.append(f"📌 Тип: {task.get('task', 'Неизвестно')}")
            else:
                test_results.append("⚠️ Заданий нет (это нормально)")
        else:
            test_results.append("❌ Ошибка получения заданий")

        # Тест 2: Проверка метода check_task (если есть задания)
        if tasks and len(tasks) > 0 and tasks[0].get('signature'):
            signature = tasks[0]['signature']
            test_results.append(f"🔍 Проверка задания {signature[:8]}...")
            status = await flyer_check_task(signature, call.from_user.id)
            test_results.append(f"📊 Статус: {status}")

    except Exception as e:
        test_results.append(f"❌ Ошибка тестирования: {str(e)}")

    text = "🧪 <b>РЕЗУЛЬТАТЫ ТЕСТА FLYER API</b>\n\n" + "\n".join(test_results)

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔄 Повторить тест", callback_data="test_flyer_api")
    keyboard.button(text="🔙 Назад", callback_data="flyer_settings")
    keyboard.adjust(1)

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')
    await call.answer()

@dp.callback_query(F.data == "gramads_settings")
async def gramads_settings_menu(call: types.CallbackQuery):
    """Меню настроек Gramads"""
    if call.from_user.id not in ADMIN_IDS:
        return

    # Получаем текущий токен
    current_token = await get_setting("gramads_token") or "Не установлен"

    # Обрезаем для отображения
    if len(current_token) > 30:
        token_display = f"{current_token[:15]}...{current_token[-15:]}"
    else:
        token_display = current_token

    # Статус
    status = "🟢 Включен" if core.gramads_enabled else "🔴 Выключен"

    # Статистика
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_blocked = FALSE") as cursor:
            total_users = (await cursor.fetchone())[0]

    text = (
        f"📺 <b>НАСТРОЙКИ GRAMADS</b>\n\n"
        f"📊 Статус: {status}\n"
        f"👥 Пользователей: {total_users}\n"
        f"🔑 Токен: <code>{token_display}</code>\n\n"
        f"<i>Реклама — дополнительный доход для бота</i>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить токен", callback_data="set_gramads_token")
    kb.button(text="🧪 Тест рекламы", callback_data="test_gramads")
    kb.button(text="📊 Статистика показов", callback_data="gramads_stats")
    kb.button(text="🔄 Перезагрузить настройки", callback_data="reload_gramads")
    kb.button(text="🔙 Назад", callback_data="admin_settings_back")
    kb.adjust(1)

    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode='HTML')
    await call.answer()

@dp.callback_query(F.data == "set_gramads_token")
async def set_gramads_token_start(call: types.CallbackQuery, state: FSMContext):
    """Запрос нового токена Gramads"""
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.message.answer(
        "🔑 <b>Введите новый токен Gramads:</b>\n\n"
        "Токен должен начинаться с <code>eyJ</code> (JWT формат)\n"
        "Пример: <code>eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...</code>",
        parse_mode='HTML'
    )
    await state.set_state(AdminStates.waiting_gramads_token)
    await call.answer()

@dp.message(AdminStates.waiting_gramads_token)
async def save_gramads_token(message: types.Message, state: FSMContext):
    """Сохранение токена Gramads"""

    new_token = message.text.strip()

    # Проверяем формат токена (JWT обычно начинается с eyJ)
    if not new_token.startswith('eyJ'):
        await message.answer(
            "❌ <b>Неверный формат токена!</b>\n\n"
            "Токен Gramads должен быть в JWT формате и начинаться с <code>eyJ</code>",
            parse_mode='HTML'
        )
        await state.clear()
        return

    # Сохраняем в БД
    await set_setting("gramads_token", new_token)

    # Обновляем глобальные переменные
    core.GRAMADS_TOKEN = new_token
    core.gramads_enabled = True

    await message.answer(
        f"✅ <b>Токен Gramads успешно сохранен!</b>\n\n"
        f"📊 Токен: <code>{new_token[:20]}...{new_token[-20:]}</code>\n"
        f"🔄 Система рекламы активирована",
        parse_mode='HTML'
    )
    await state.clear()

@dp.callback_query(F.data == "test_gramads")
async def test_gramads_ad(call: types.CallbackQuery):
    """Тест рекламы Gramads"""
    if call.from_user.id not in ADMIN_IDS:
        return

    user_id = call.from_user.id

    await call.message.edit_text("🧪 <b>Тестируем показ рекламы...</b>", parse_mode='HTML')

    success = await show_gramads_ad(user_id)

    if success:
        text = "✅ <b>Реклама успешно показана!</b>\n\nПроверьте свой Telegram для подтверждения."
    else:
        text = "❌ <b>Не удалось показать рекламу</b>\n\nПроверьте:\n1. Корректность токена\n2. Наличие активных рекламных кампаний\n3. Доступность API Gramads"

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Повторить тест", callback_data="test_gramads")
    kb.button(text="🔙 Назад", callback_data="gramads_settings")
    kb.adjust(1)

    await call.message.edit_text(text, parse_mode='HTML', reply_markup=kb.as_markup())
    await call.answer()

@dp.callback_query(F.data == "gramads_stats")
async def show_gramads_stats(call: types.CallbackQuery):
    """Статистика показов рекламы"""
    if call.from_user.id not in ADMIN_IDS:
        return

    # Считаем пользователей, которым показывали рекламу
    ad_users_count = len(last_ad_times)

    # Можно добавить больше статистики из БД
    text = (
        f"📊 <b>СТАТИСТИКА GRAMADS</b>\n\n"
        f"🔄 Статус: {'🟢 Активен' if core.gramads_enabled else '🔴 Отключен'}\n"
        f"👥 Показано рекламы: {ad_users_count} пользователям\n"
        f"⏰ Кеш последних показов: {len(last_ad_times)} записей\n\n"
        f"<i>Статистика обновляется в реальном времени</i>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data="gramads_stats")
    kb.button(text="🔙 Назад", callback_data="gramads_settings")
    kb.adjust(1)

    await call.message.edit_text(text, parse_mode='HTML', reply_markup=kb.as_markup())
    await call.answer()

@dp.callback_query(F.data == "reload_gramads")
async def reload_gramads_settings(call: types.CallbackQuery):
    """Перезагрузка настроек Gramads"""
    if call.from_user.id not in ADMIN_IDS:
        return

    await load_gramads_token()
    await call.answer("✅ Настройки Gramads перезагружены", show_alert=True)
    await gramads_settings_menu(call)





@dp.callback_query(F.data.regexp(r"^edit_allowed_langs_page_\d+$"))
async def edit_allowed_langs(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return
    page = int(call.data.split("_")[-1])
    allowed = (await get_setting("allowed_languages") or "uk,ru").split(",")
    await call.message.edit_text(
        "🌐 <b>Выбор разрешённых языков</b>\n\n"
        "Нажмите на язык чтобы включить/выключить его.\n\n" 
        "<i>Кнопка показывает состояние и название языка.</i>",
        parse_mode="HTML",
        reply_markup=build_lang_page_markup(page, allowed)
    )
    await call.answer()

# --- Тогл конкретної мови (з відкатом на ту ж сторінку) ---
@dp.callback_query(F.data.regexp(r"^toggle_lang_[a-z]{2,3}_page_\d+$"))
async def toggle_lang(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return
    parts = call.data.split("_")
    lang_code = parts[2]
    page = int(parts[-1])

    allowed = (await get_setting("allowed_languages") or "uk,ru").split(",")
    allowed = [x for x in allowed if x]

    if lang_code in allowed:
        allowed.remove(lang_code)
    else:
        allowed.append(lang_code)

    await set_setting("allowed_languages", ",".join(allowed))

    allowed = (await get_setting("allowed_languages") or "uk,ru").split(",")
    await call.message.edit_text(
        "🌐 <b>Выбор разрешённых языков</b>\n\n"
        "Нажмите на язык чтобы включить/выключить его.\n\n" 
        "<i>Кнопка показывает состояние и название языка.</i>",
        parse_mode="HTML",
        reply_markup=build_lang_page_markup(page, allowed)
    )
    await call.answer()

# --- Повернення назад до головного меню налаштувань ---
@dp.callback_query(F.data == "settings_back")
async def settings_back(call: types.CallbackQuery):
    # Викликаємо туфункцію, що показує admin settings
    try:
        await admin_settings(call.message)
    except Exception:
        # якщо admin_settings очікує Message — спробуємо просто відповісти
        await call.message.edit_text("⚙️ Перенаправляю в настройки...", reply_markup=get_admin_keyboard())
    await call.answer()



@dp.message(AdminStates.waiting_payments_channell)
async def save_payment_channel(message: Message, state: FSMContext):
    channel = message.text.strip()
    await set_setting("payments_channell", channel)

    await message.answer(
        f"✅ Канал выплат сохранён: <code>{channel}</code>",
        parse_mode="HTML",
        reply_markup=get_admin_keyboard()
    )
    await state.clear()


@dp.callback_query(F.data == "edit_promotion_price")
async def edit_promotion_price(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("❌ У вас нет доступа", show_alert=True)
        return

    await call.message.answer("💰 Введите новую цену за одно выполнение (в $):\nНапример: <b>0.02</b>", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_promotion_price)
    await call.answer()

@dp.message(AdminStates.waiting_promotion_price)
async def set_new_promotion_price(message: types.Message, state: FSMContext):
    try:
        new_price = float(message.text.strip().replace(",", "."))
        if new_price <= 0:
            await message.answer("❌ Цена должна быть больше 0.")
            return

        await set_setting("promotion_price", str(new_price))
        await message.answer(f"✅ Цена задания успешно изменена на <b>{new_price:.3f} $</b>!", parse_mode="HTML")
        await state.clear()

    except ValueError:
        await message.answer("❌ Введите корректное число, например: 0.015")

@dp.callback_query(F.data == "change_referral_settings")
async def change_referral_settings(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    # Отримуємо поточні налаштування
    bonus_lvl1 = await get_setting('ref_bonus_level1') or '0.05'
    bonus_lvl2 = await get_setting('ref_bonus_level2') or '0.03'
    tasks_lvl1 = await get_setting('ref_tasks_required_level1') or '3'
    tasks_lvl2 = await get_setting('ref_tasks_required_level2') or '5'

    kb = InlineKeyboardBuilder()
    kb.button(text=f"💰 1 ур. - {bonus_lvl1}$", callback_data="change_ref_bonus_level1")
    kb.button(text=f"📝 Заданий: {tasks_lvl1}", callback_data="change_ref_tasks_level1")
    kb.button(text=f"💰 2 ур. - {bonus_lvl2}$", callback_data="change_ref_bonus_level2")
    kb.button(text=f"📝 Заданий: {tasks_lvl2}", callback_data="change_ref_tasks_level2")
    kb.button(text="🔙 Назад", callback_data="admin_settings_back")
    kb.adjust(2, 2, 1)

    await call.message.edit_text(
        f"⚙️ <b>Настройки реферальной системы</b>\n\n"
        f"💰 <b>1 уровень</b>\n"
        f"├─ Бонус: <b>{bonus_lvl1}$</b>\n"
        f"└─ Нужно заданий: <b>{tasks_lvl1}</b>\n\n"
        f"💰 <b>2 уровень</b>\n"
        f"├─ Бонус: <b>{bonus_lvl2}$</b>\n"
        f"└─ Нужно заданий: <b>{tasks_lvl2}</b>\n\n"
        f"💡 Рефер получает выплату когда реферал выполнит нужное количество заданий.",
        parse_mode='HTML',
        reply_markup=kb.as_markup()
    )
    await call.answer()

@dp.callback_query(F.data == "change_ref_bonus_level1")
async def change_ref_bonus_level1(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("💰 Введите выплату за реферала 1 уровня (в $, например: 0.05):")
    await state.set_state(AdminStates.waiting_ref_bonus_level1)
    await call.answer()

@dp.callback_query(F.data == "change_ref_bonus_level2")
async def change_ref_bonus_level2(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("💰 Введите выплату за реферала 2 уровня (в $, например: 0.03):")
    await state.set_state(AdminStates.waiting_ref_bonus_level2)
    await call.answer()

@dp.callback_query(F.data == "change_ref_tasks_level1")
async def change_ref_tasks_level1(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("📝 Введите количество заданий для получения бонуса 1 уровня (например: 3):")
    await state.set_state(AdminStates.waiting_ref_tasks_level1)
    await call.answer()

@dp.callback_query(F.data == "change_ref_tasks_level2")
async def change_ref_tasks_level2(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("📝 Введите количество заданий для получения бонуса 2 уровня (например: 5):")
    await state.set_state(AdminStates.waiting_ref_tasks_level2)
    await call.answer()

@dp.message(AdminStates.waiting_ref_bonus_level1)
async def process_ref_bonus_level1(message: types.Message, state: FSMContext):
    try:
        value = float(message.text)
        if value <= 0:
            await message.answer("❌ Сумма должна быть больше 0")
            return
        await set_setting('ref_bonus_level1', str(value))
        await message.answer(f"✅ Бонус 1 уровня установлен: {value}$")
    except ValueError:
        await message.answer("❌ Введите корректное число")
    await state.clear()

@dp.message(AdminStates.waiting_ref_bonus_level2)
async def process_ref_bonus_level2(message: types.Message, state: FSMContext):
    try:
        value = float(message.text)
        if value <= 0:
            await message.answer("❌ Сумма должна быть больше 0")
            return
        await set_setting('ref_bonus_level2', str(value))
        await message.answer(f"✅ Бонус 2 уровня установлен: {value}$")
    except ValueError:
        await message.answer("❌ Введите корректное число")
    await state.clear()

@dp.message(AdminStates.waiting_ref_tasks_level1)
async def process_ref_tasks_level1(message: types.Message, state: FSMContext):
    try:
        value = int(message.text)
        if value < 1:
            await message.answer("❌ Количество должно быть не менее 1")
            return
        await set_setting('ref_tasks_required_level1', str(value))
        await message.answer(f"✅ Заданий для 1 уровня: {value}")
    except ValueError:
        await message.answer("❌ Введите корректное число")
    await state.clear()

@dp.message(AdminStates.waiting_ref_tasks_level2)
async def process_ref_tasks_level2(message: types.Message, state: FSMContext):
    try:
        value = int(message.text)
        if value < 1:
            await message.answer("❌ Количество должно быть не менее 1")
            return
        await set_setting('ref_tasks_required_level2', str(value))
        await message.answer(f"✅ Заданий для 2 уровня: {value}")
    except ValueError:
        await message.answer("❌ Введите корректное число")
    await state.clear()

@dp.callback_query(F.data == "set_crypto_token")
async def admin_set_crypto_token(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("🪙 Отправьте новый токен криптобота:")
    await state.set_state(AdminStates.waiting_crypto_token)

@dp.message(AdminStates.waiting_crypto_token)
async def process_crypto_token(message: types.Message, state: FSMContext):

    new_token = message.text.strip()

    if not new_token or len(new_token) < 10:
        await message.answer("❌ Неверный формат токена. Попробуйте еще раз.")
        await state.clear()
        return

    # ✅ Сохраняем токен в базе данных
    await set_setting("core.CRYPTO_BOT_TOKEN", new_token)

    # ✅ Обновляем глобальные переменные
    core.CRYPTO_BOT_TOKEN = new_token
    core.crypto_enabled = True

    await message.answer(
        f"✅ Токен криптобота успешно обновлен!\n\n"
        f"🔐 Новый токен: <code>{core.CRYPTO_BOT_TOKEN[:8]}...{core.CRYPTO_BOT_TOKEN[-4:]}</code>",
        parse_mode="HTML"
    )

    await state.clear()

@dp.message(F.text == "📢 Рассылка")
async def admin_broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    await message.answer(
        "📢 <b>Создание рассылки</b>\n\n"
        "Отправьте сообщение для рассылки (можно с медиа).\n"
        "После этого вы сможете добавить инлайн кнопки.",
        parse_mode='HTML'
    )
    await state.set_state(AdminStates.waiting_broadcast)

# Админ настройки callback
@dp.callback_query(F.data == "change_payments_channel")
async def change_payments_channel(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.message.answer("Введите новую ссылку на канал новостей:")
    await state.set_state(AdminStates.waiting_payments_channel)
    await call.answer()

@dp.message(AdminStates.waiting_payments_channel)
async def process_payments_channel(message: types.Message, state: FSMContext):
    await set_setting('payments_channel', message.text)
    await message.answer("✅ Ссылка на канал новостей обновлена")
    await state.clear()

@dp.callback_query(F.data == "change_chat")
async def change_chat(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.message.answer("Введите новую ссылку на чат:")
    await state.set_state(AdminStates.waiting_chat_link)
    await call.answer()

@dp.message(AdminStates.waiting_chat_link)
async def process_chat_link(message: types.Message, state: FSMContext):
    await set_setting('chat_link', message.text)
    await message.answer("✅ Ссылка на чат обновлена")
    await state.clear()

@dp.callback_query(F.data == "change_currency")
async def change_currency(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.message.answer("Введите новую валюту (3-4 символа, например USD или TEST):")
    await state.set_state(AdminStates.waiting_currency)
    await call.answer()

@dp.message(AdminStates.waiting_currency)
async def process_currency(message: types.Message, state: FSMContext):
    currency = message.text.strip()
    if 3 <= len(currency) <= 4:
        await set_setting('currency', currency)
        await message.answer(f"✅ Валюта обновлена: {currency}")
    else:
        await message.answer("❌ Введите 3-4 символа для валюты")
    await state.clear()

@dp.callback_query(F.data == "change_flyer_key")
async def change_flyer_key(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return

    current_key = core.FLYER_API_KEY
    if current_key:
        key_info = f"\n\n📊 Текущий ключ: <code>{current_key[:8]}...{current_key[-4:]}</code>"
    else:
        key_info = "\n\n⚠️ Ключ не настроен"

    await call.message.answer(
        f"🔑 <b>Смена ключа Flyer API</b>{key_info}\n\n"
        "Введите новый ключ Flyer API:",
        parse_mode='HTML'
    )
    await state.set_state(AdminStates.waiting_flyer_key)
    await call.answer()

# ← ДОБАВЬ ЭТОТ КОД ЗДЕСЬ
@dp.message(AdminStates.waiting_flyer_key)
async def process_flyer_key(message: types.Message, state: FSMContext):

    new_key = message.text.strip()

    # Проверяем формат ключа
    if not new_key.startswith('FL-') or len(new_key) < 10:
        await message.answer(
            "❌ Неверный формат ключа Flyer API. Ключ должен начинаться с 'FL-' и быть длиннее 10 символов."
        )
        await state.clear()
        return

    core.FLYER_API_KEY = new_key

    try:
        core.flyer = Flyer(core.FLYER_API_KEY)
        core.flyer_enabled = True
        print(f"✅ Flyer переинициализирован с новым ключом: {core.FLYER_API_KEY[:8]}...")
    except Exception as e:
        core.flyer_enabled = False
        print(f"❌ Ошибка инициализации Flyer: {e}")
        await message.answer(f"❌ Ошибка инициализации Flyer: {e}")
        await state.clear()
        return

    # Тестируем новый ключ
    await message.answer("🔄 Тестируем новый ключ...")

    try:
        test_tasks = await flyer_get_tasks(message.from_user.id, 'ru', limit=1)

        if test_tasks is not None:
            if test_tasks:
                status_text = f"✅ Ключ работает! Получено {len(test_tasks)} заданий"
            else:
                status_text = "⚠️ Ключ работает, но заданий нет"
        else:
            status_text = "❌ Ключ не работает - ошибка получения заданий"

        await message.answer(
            f"🔑 <b>Ключ Flyer API успешно обновлен!</b>\n\n"
            f"{status_text}\n\n"
            f"📊 Новый ключ: <code>{core.FLYER_API_KEY[:8]}...{core.FLYER_API_KEY[-4:]}</code>",
            parse_mode='HTML'
        )

    except Exception as e:
        await message.answer(
            f"❌ <b>Ошибка тестирования ключа:</b>\n\n"
            f"{str(e)}\n\n"
            f"⚠️ Ключ сохранен, но требует проверки",
            parse_mode='HTML'
        )

    await state.clear()


@dp.message(AdminStates.waiting_broadcast)
async def process_broadcast_message(message: types.Message, state: FSMContext):
    # Сохраняем сообщение для рассылки
    media_data = None
    text_data = None
    media_type = None

    if message.photo:
        # Фото с текстом
        media_data = message.photo[-1].file_id
        media_type = 'photo'
        text_data = message.caption if message.caption else None
    elif message.video:
        # Видео с текстом
        media_data = message.video.file_id
        media_type = 'video'
        text_data = message.caption if message.caption else None
    elif message.animation:
        # GIF с текстом
        media_data = message.animation.file_id
        media_type = 'animation'
        text_data = message.caption if message.caption else None
    elif message.text:
        # Только текст
        text_data = message.html_text
        media_type = 'text'
    elif message.document:
        # Документ с текстом
        media_data = message.document.file_id
        media_type = 'document'
        text_data = message.caption if message.caption else None
    else:
        await message.answer("❌ Неподдерживаемый тип сообщения для рассылки")
        return

    # Сохраняем в состояние
    await state.update_data(
        broadcast_text=text_data,
        broadcast_media=media_data,
        broadcast_media_type=media_type,
        broadcast_buttons=[]  # пустой список для кнопок
    )

    # Формируем предпросмотр
    preview_text = "✅ <b>Сообщение для рассылки получено!</b>\n\n"

    if media_type == 'photo':
        preview_text += "📷 <b>Тип:</b> Фото с текстом\n"
    elif media_type == 'video':
        preview_text += "🎥 <b>Тип:</b> Видео с текстом\n"
    elif media_type == 'animation':
        preview_text += "🎬 <b>Тип:</b> GIF с текстом\n"
    elif media_type == 'document':
        preview_text += "📎 <b>Тип:</b> Документ с текстом\n"
    else:
        preview_text += "📝 <b>Тип:</b> Только текст\n"

    if text_data:
        preview_text += f"<b>Текст:</b>\n{text_data[:200]}{'...' if len(text_data) > 200 else ''}\n\n"

    preview_text += "<b>Доступные действия:</b>\n"
    preview_text += "• Добавить инлайн кнопки\n"
    preview_text += "• Начать рассылку сразу\n\n"
    preview_text += "<i>Кнопки добавляются по одной. Максимум 5 кнопок.</i>"

    # Предлагаем добавить кнопки
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="➕ Добавить кнопку", callback_data="broadcast_add_button")
    keyboard.button(text="🚀 Начать рассылку", callback_data="broadcast_start")
    keyboard.button(text="❌ Отмена", callback_data="broadcast_cancel")
    keyboard.adjust(1)

    # Сохраняем превью-сообщение отдельно, чтобы потом его редактировать
    preview_message = None

    if media_type == 'photo' and media_data:
        # Показываем предпросмотр фото с текстом
        preview_message = await message.answer_photo(
            media_data,
            caption=preview_text,
            reply_markup=keyboard.as_markup(),
            parse_mode='HTML'
        )
    elif media_type == 'video' and media_data:
        # Показываем предпросмотр видео с текстом
        preview_message = await message.answer_video(
            media_data,
            caption=preview_text,
            reply_markup=keyboard.as_markup(),
            parse_mode='HTML'
        )
    elif media_type == 'animation' and media_data:
        # Показываем предпросмотр GIF с текстом
        preview_message = await message.answer_animation(
            media_data,
            caption=preview_text,
            reply_markup=keyboard.as_markup(),
            parse_mode='HTML'
        )
    elif media_type == 'document' and media_data:
        # Показываем предпросмотр документа с текстом
        preview_message = await message.answer_document(
            media_data,
            caption=preview_text,
            reply_markup=keyboard.as_markup(),
            parse_mode='HTML'
        )
    else:
        # Только текст
        preview_message = await message.answer(
            preview_text,
            reply_markup=keyboard.as_markup(),
            parse_mode='HTML'
        )

    # Сохраняем ID превью-сообщения для дальнейшего редактирования
    await state.update_data(preview_message_id=preview_message.message_id)


@dp.message(AdminStates.waiting_task_name)
async def process_task_name(message: types.Message, state: FSMContext):
    print(f"🔧 Получено название: {message.text}")
    await state.update_data(name=message.text, task_type='subscribe_channel')
    await message.answer("📄 Введите описание задания:")
    await state.set_state(AdminStates.waiting_task_description)

@dp.message(AdminStates.waiting_task_description)
async def process_task_description(message: types.Message, state: FSMContext):
    print(f"🔧 Получено описание: {message.text}")
    await state.update_data(description=message.text)
    await message.answer("🔗 Введите username канала (например: @channel_name):")
    await state.set_state(AdminStates.waiting_task_url)

@dp.callback_query(F.data.startswith("edit_task_"))
async def edit_task_start(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return

    task_id = int(call.data.replace("edit_task_", ""))

    # Показываем меню редактирования вместо ошибки
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✏️ Изменить название", callback_data=f"edit_name_{task_id}")
    keyboard.button(text="📝 Изменить описание", callback_data=f"edit_desc_{task_id}")
    keyboard.button(text="💰 Изменить цену", callback_data=f"edit_price_{task_id}")
    keyboard.button(text="👥 Изменить лимит выполнений", callback_data=f"edit_limit_{task_id}")
    keyboard.button(text="🔗 Изменить URL", callback_data=f"edit_url_{task_id}")
    keyboard.button(text="🔄 Изменить текст кнопки", callback_data=f"edit_button_{task_id}")
    keyboard.button(text="🔙 Назад", callback_data=f"view_task_{task_id}")
    keyboard.adjust(2, 2, 1, 1)

    await call.message.edit_text(
        f"✏️ <b>Редактирование задания #{task_id}</b>\n\n"
        f"Выберите что хотите изменить:",
        reply_markup=keyboard.as_markup(),
        parse_mode='HTML'
    )
    await call.answer()

@dp.callback_query(F.data == "broadcast_add_button")
async def broadcast_add_button(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    buttons = data.get('broadcast_buttons', [])

    if len(buttons) >= 5:
        await call.answer("❌ Максимум 5 кнопок в сообщении", show_alert=True)
        return

    await call.message.answer(
        "🔘 <b>Добавление кнопки</b>\n\n"
        "Отправьте текст кнопки и URL через знак |\n"
        "<b>Формат:</b> <code>Текст кнопки | https://example.com</code>\n\n"
        "<i>Пример:</i> <code>Наш канал | https://t.me/example</code>",
        parse_mode='HTML'
    )
    await state.set_state(AdminStates.waiting_broadcast_buttons)
    await call.answer()

@dp.message(AdminStates.waiting_broadcast_buttons)
async def process_broadcast_button(message: types.Message, state: FSMContext):
    try:
        text, url = message.text.split('|', 1)
        text = text.strip()
        url = url.strip()

        if not text or not url:
            await message.answer("❌ Неверный формат. Используйте: Текст | URL")
            return

        if not url.startswith('http'):
            await message.answer("❌ URL должен начинаться с http:// или https://")
            return

        # Добавляем кнопку в список
        data = await state.get_data()
        buttons = data.get('broadcast_buttons', [])
        buttons.append({'text': text, 'url': url})

        await state.update_data(broadcast_buttons=buttons)

        # Показываем текущие кнопки
        buttons_text = "\n".join([f"• {btn['text']} -> {btn['url']}" for btn in buttons])

        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="➕ Добавить еще кнопку", callback_data="broadcast_add_button")
        keyboard.button(text="🚀 Начать рассылку", callback_data="broadcast_start")
        keyboard.button(text="🗑️ Очистить все кнопки", callback_data="broadcast_clear_buttons")
        keyboard.button(text="❌ Отмена", callback_data="broadcast_cancel")
        keyboard.adjust(1)

        await message.answer(
            f"✅ <b>Кнопка добавлена!</b>\n\n"
            f"<b>Текущие кнопки:</b>\n{buttons_text}\n\n"
            f"Вы можете добавить еще кнопки или начать рассылку.",
            reply_markup=keyboard.as_markup(),
            parse_mode='HTML'
        )

    except ValueError:
        await message.answer(
            "❌ Неверный формат. Используйте:\n"
            "<code>Текст кнопки | https://example.com</code>",
            parse_mode='HTML'
        )

@dp.message(F.text == "🧹 Проверка неактивных")
async def check_inactive_users(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    # Показываем меню проверки
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔍 Найти неактивных", callback_data="find_inactive_users")
    keyboard.button(text="📊 Статистика неактивных", callback_data="inactive_stats")
    keyboard.button(text="🚫 Массовое удаление", callback_data="mass_cleanup")
    keyboard.adjust(1)

    await message.answer(
        "🧹 <b>Проверка неактивных пользователей</b>\n\n"
        "Здесь вы можете найти пользователей, которые:\n"
        "• Баланс: 0$\n" 
        "• Не выполняли задания более 3 дней\n"
        "• Можно массово удалить из базы\n\n"
        "<i>Выберите действие:</i>",
        reply_markup=keyboard.as_markup(),
        parse_mode='HTML'
    )

@dp.callback_query(F.data == "find_inactive_users")
async def find_inactive_users(call: types.CallbackQuery):
    """Поиск неактивных пользователей"""
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.answer()
    await call.message.edit_text("🔍 <b>Ищу неактивных пользователей...</b>", parse_mode='HTML')

    inactive_users = await get_inactive_users()

    if not inactive_users:
        await call.message.edit_text(
            "✅ <b>Неактивных пользователей не найдено!</b>\n\n"
            "Все пользователи активны или имеют баланс/выполняли задания.",
            parse_mode='HTML'
        )
        return

    # Разбиваем на страницы по 10 пользователей
    users_per_page = 10
    total_pages = (len(inactive_users) + users_per_page - 1) // users_per_page

    await show_inactive_users_page(call, inactive_users, 1, users_per_page, total_pages)

@dp.callback_query(F.data.startswith("inactive_page_"))
async def inactive_users_pagination(call: types.CallbackQuery):
    """Пагинация по неактивным пользователям"""
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.answer()
    page = int(call.data.split("_")[2])
    inactive_users = await get_inactive_users()

    users_per_page = 10
    total_pages = (len(inactive_users) + users_per_page - 1) // users_per_page

    await show_inactive_users_page(call, inactive_users, page, users_per_page, total_pages)

@dp.callback_query(F.data == "confirm_mass_delete")
async def confirm_mass_delete(call: types.CallbackQuery):
    """Подтверждение массового удаления"""
    if call.from_user.id not in ADMIN_IDS:
        return

    inactive_users = await get_inactive_users()

    if not inactive_users:
        await call.answer("❌ Нет пользователей для удаления", show_alert=True)
        return

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Да, удалить всех", callback_data="execute_mass_delete")
    keyboard.button(text="❌ Отмена", callback_data="find_inactive_users")
    keyboard.adjust(2)

    await call.message.edit_text(
        f"🚫 <b>Подтверждение массового удаления</b>\n\n"
        f"Вы уверены, что хотите удалить <b>{len(inactive_users)}</b> неактивных пользователей?\n\n"
        f"<i>Это действие нельзя отменить! Будут удалены все пользователи с:\n"
        f"• Баланс: 0$\n"
        f"• Нет активности 3+ дней</i>",
        reply_markup=keyboard.as_markup(),
        parse_mode='HTML'
    )

@dp.callback_query(F.data == "inactive_back")
async def inactive_back(call: types.CallbackQuery):
    """Возврат в меню проверки неактивных"""
    if call.from_user.id not in ADMIN_IDS:
        return

    await call.answer()
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔍 Найти неактивных", callback_data="find_inactive_users")
    keyboard.button(text="📊 Статистика неактивных", callback_data="inactive_stats")
    keyboard.button(text="🚫 Массовое удаление", callback_data="mass_cleanup")
    keyboard.adjust(1)

    await call.message.edit_text(
        "🧹 <b>Проверка неактивных пользователей</b>\n\n"
        "Выберите действие:",
        reply_markup=keyboard.as_markup(),
        parse_mode='HTML'
    )


# ===== handlers_inactive.py =====
# -*- coding: utf-8 -*-
"""Работа с неактивными пользователями."""



# Визначаємо рефера
@dp.callback_query(F.data.startswith("clean_userdb:"))
async def clean_userdb_menu(call: CallbackQuery):
    await call.answer()
    user_id = int(call.data.split(":")[1])

    kb = InlineKeyboardBuilder()

    kb.button(text="💰 Баланс", callback_data=f"cleanopt:balance:{user_id}")
    kb.button(text="🧊 Заморож. баланс", callback_data=f"cleanopt:frozen:{user_id}")
    kb.button(text="👤 Рефер", callback_data=f"cleanopt:referrer:{user_id}")
    kb.button(text="👥 Рефералы", callback_data=f"cleanopt:refs:{user_id}")
    kb.button(text="📊 Статистика", callback_data=f"cleanopt:stats:{user_id}")
    kb.button(text="🔥 Удалить все", callback_data=f"cleanopt:delete:{user_id}")
    kb.button(text="❌ Отмена", callback_data=f"cleanopt:cancel:{user_id}")

    kb.adjust(1, 1, 2, 2, 1)

    await call.message.edit_text(
        f"🗑 <b>Очистка данных пользователя {user_id}</b>\n"
        f"Выберите, что хотите очистить:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

async def show_inactive_users_page(call, inactive_users, page, users_per_page, total_pages):
    """Показывает страницу с неактивными пользователями"""
    start_idx = (page - 1) * users_per_page
    end_idx = start_idx + users_per_page
    users_page = inactive_users[start_idx:end_idx]

    text = f"🧹 <b>Неактивные пользователи</b>\n\n"
    text += f"📊 Найдено: <b>{len(inactive_users)}</b> пользователей\n"
    text += f"📄 Страница {page}/{total_pages}\n\n"

    for i, user in enumerate(users_page, start_idx + 1):
        last_active = user['last_activity'] or 'Никогда'
        text += f"{i}. ID: <code>{user['user_id']}</code> | @{user['username']}\n"
        text += f"   💰 Баланс: {user['balance']:.3f}$ | 📝 Заданий: {user['completed_tasks']}\n"
        text += f"   📅 Последняя активность: {last_active}\n\n"

    keyboard = InlineKeyboardBuilder()

    # Кнопки пагинации
    if page > 1:
        keyboard.button(text="⬅️ Назад", callback_data=f"inactive_page_{page-1}")
    if page < total_pages:
        keyboard.button(text="➡️ Вперед", callback_data=f"inactive_page_{page+1}")

    # Кнопки действий
    keyboard.button(text="🗑️ Удалить всех", callback_data="confirm_mass_delete")
    keyboard.button(text="📊 Статистика", callback_data="inactive_stats")
    keyboard.button(text="🔙 Назад", callback_data="inactive_back")

    keyboard.adjust(2, 1, 2)

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')

async def get_inactive_users():
    """Находит пользователей с балансом 0 и без заданий за последние 3 дня"""
    async with aiosqlite.connect('bot_database.db') as db:
        # Получаем пользователей с балансом 0 и без активности 3 дня
        query = '''
            SELECT 
                u.user_id, 
                u.username, 
                u.balance, 
                u.frozen_balance,
                u.completed_tasks,
                u.registered_at,
                MAX(COALESCE(ct.completed_at, w.created_at, un.last_notification, u.registered_at)) as last_activity
            FROM users u
            LEFT JOIN completed_tasks ct ON u.user_id = ct.user_id AND ct.completed_at >= datetime('now', '-3 days')
            LEFT JOIN withdrawals w ON u.user_id = w.user_id AND w.created_at >= datetime('now', '-3 days')
            LEFT JOIN user_notifications un ON u.user_id = un.user_id AND un.last_notification >= datetime('now', '-3 days')
            WHERE u.balance = 0 
                AND u.frozen_balance = 0
                AND u.is_blocked = FALSE
            GROUP BY u.user_id
            HAVING last_activity < datetime('now', '-3 days') OR last_activity IS NULL
            ORDER BY last_activity ASC
        '''

        async with db.execute(query) as cursor:
            columns = [column[0] for column in cursor.description]
            users = [dict(zip(columns, row)) for row in await cursor.fetchall()]

            # Форматируем даты для красивого отображения
            for user in users:
                if user['last_activity']:
                    try:
                        dt = datetime.fromisoformat(user['last_activity'])
                        user['last_activity'] = dt.strftime("%d.%m.%Y %H:%M")
                    except:
                        pass
                else:
                    user['last_activity'] = "Никогда"

            return users

# Импорт клавиатуры главного меню
from utils import get_main_keyboard

# ===== Ручное начисление рекламного баланса администратором =====
@dp.message(F.text.startswith("/add_ad_balance"))
async def add_ad_balance_command(message: types.Message):
    """Использование: /add_ad_balance USER_ID AMOUNT"""
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer(
            "Формат команды:\n"
            "<code>/add_ad_balance ID_ПОЛЬЗОВАТЕЛЯ СУММА</code>\n\n"
            "Пример: <code>/add_ad_balance 123456789 5</code>",
            parse_mode="HTML"
        )
        return

    try:
        target_user_id = int(parts[1])
        amount = float(parts[2])
        if target_user_id <= 0 or not (0 < amount < float("inf")):
            raise ValueError
    except ValueError:
        await message.answer(
            "❌ Проверьте ID и сумму. Сумма должна быть положительным числом."
        )
        return

    try:
        async with aiosqlite.connect("bot_database.db") as db:
            cursor = await db.execute(
                """
                UPDATE users
                SET ad_balance = COALESCE(ad_balance, 0) + ?
                WHERE user_id = ?
                """,
                (amount, target_user_id)
            )
            await db.commit()

            if cursor.rowcount == 0:
                await message.answer(
                    f"❌ Пользователь <code>{target_user_id}</code> не найден в базе.",
                    parse_mode="HTML"
                )
                return

            async with db.execute(
                "SELECT ad_balance FROM users WHERE user_id = ?",
                (target_user_id,)
            ) as result:
                row = await result.fetchone()

        new_balance = float(row[0] or 0) if row else 0.0

        await message.answer(
            f"✅ Рекламный баланс пользователя <code>{target_user_id}</code> "
            f"увеличен на <b>{amount:.3f} $</b>.\n"
            f"💳 Новый рекламный баланс: <b>{new_balance:.3f} $</b>",
            parse_mode="HTML"
        )

        try:
            await bot.send_message(
                target_user_id,
                f"🎁 Вам начислен рекламный баланс!\n\n"
                f"➕ Начислено: <b>{amount:.3f} $</b>\n"
                f"💳 Текущий рекламный баланс: <b>{new_balance:.3f} $</b>",
                parse_mode="HTML"
            )
        except Exception as notify_error:
            print(
                f"⚠️ Не удалось уведомить пользователя "
                f"{target_user_id}: {notify_error}"
            )

    except Exception as e:
        print(f"❌ Ошибка начисления рекламного баланса: {e}")
        await message.answer("❌ Не удалось начислить рекламный баланс. Проверьте логи.")


# Последний обработчик: отвечает на некорректный ввод ссылки,
# но не перехватывает сообщения, которые уже обработали другие разделы меню.
@dp.message(PromotionStates.waiting_channel_link, F.text)
async def promo_invalid_link_input(message: types.Message):
    await message.answer(
        "❌ Отправьте публичную ссылку вида https://t.me/имя_канала "
        "или @username.\n\n"
        "Чтобы выйти из создания задания, отправьте /cancel "
        "или выберите другой раздел меню."
    )


# Отмена незавершённого вывода USDT без ввода суммы.
@dp.message(Command("cancel"), WithdrawalStates.waiting_for_amount)
async def cancel_withdrawal_amount(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Вывод отменён. Теперь можно пользоваться другими разделами бота.")


# Последний обработчик: меню и другие зарегистрированные команды имеют приоритет.
@dp.message(WithdrawalStates.waiting_for_amount, F.text)
async def invalid_withdrawal_amount_input(message: types.Message):
    await message.answer(
        "❌ Введите сумму числом (например, 5 или 2.5).\n\n"
        "Чтобы отменить вывод и перейти в другой раздел, выберите пункт меню "
        "или отправьте /cancel."
    )


@dp.message(Command("miniapp"))
async def cmd_miniapp(message: types.Message):
    """Открывает Mini App отдельной кнопкой из любого места чата."""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Mini App пока доступен только администратору.")
        return
    if WebAppInfo is None:
        await message.answer("Mini App недоступен на этой версии aiogram.")
        return
    url = getattr(core, "MINIAPP_URL", "https://uamoneybots.ru/miniapp/?v=20261125k")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🌐 Открыть Mini App", web_app=WebAppInfo(url=url))
        ]]
    )
    await message.answer(
        "Нажмите кнопку ниже, чтобы открыть Mini App UA.",
        reply_markup=keyboard,
    )


def _miniapp_normalize_check_result(result):
    """Приводит разные форматы ответов проверок к (completed, reward)."""
    if isinstance(result, dict):
        raw_completed = result.get("completed", result.get("count", 0))
        raw_reward = result.get("total_reward", result.get("reward", 0))
    elif isinstance(result, (tuple, list)) and len(result) >= 2:
        raw_completed, raw_reward = result[0], result[1]
    else:
        raw_completed, raw_reward = result or 0, 0
    if isinstance(raw_completed, (list, set, dict)):
        completed = len(raw_completed)
    else:
        completed = int(raw_completed or 0)
    return completed, float(raw_reward or 0.0)


async def _miniapp_check_flyer_and_custom(user_id: int, language: str):
    """Проверка заданий №1: Flyer + кастомные подписки."""
    completed = 0
    total_reward = 0.0

    flyer_tasks = await flyer_get_tasks(user_id, language, limit=20)
    for task in flyer_tasks:
        signature = task.get("signature", "")
        if not signature:
            continue
        status = await flyer_check_task(signature, user_id)
        if status not in (True, "completed", "done", "complete", "waiting"):
            continue
        saved_status = await get_task_status(user_id, signature)
        if saved_status in ("completed", "done", "complete", "waiting"):
            continue
        reward = float(FLZAD)
        balance_ok = await update_user_balance(user_id, frozen_amount=reward)
        tasks_ok = await update_user_balance(user_id, completed_tasks=1)
        if not balance_ok or not tasks_ok:
            raise RuntimeError(f"Не удалось сохранить награду Flyer: {signature}")
        await add_frozen_transaction(user_id, reward, "core.flyer", signature)
        await set_task_status(user_id, signature, "waiting" if status == "waiting" else "completed")
        completed += 1
        total_reward += reward

    for task in await get_active_custom_tasks():
        if task.get("task_type") != "subscribe_channel" or not task.get("channel_username"):
            continue
        try:
            member = await bot.get_chat_member(f"@{task['channel_username']}", user_id)
            if str(getattr(member, "status", "")).lower() not in ("member", "administrator", "creator"):
                continue
            async with aiosqlite.connect("bot_database.db") as db:
                async with db.execute(
                    "SELECT 1 FROM custom_task_completions WHERE task_id=? AND user_id=?",
                    (task["id"], user_id),
                ) as cursor:
                    if await cursor.fetchone():
                        continue
            await complete_custom_task(task["id"], user_id)
            reward = float(task.get("price") or 0)
            await update_user_balance(user_id, frozen_amount=reward, completed_tasks=1)
            await add_frozen_transaction(user_id, reward, "custom_subscribe", task["id"])
            completed += 1
            total_reward += reward
        except Exception as exc:
            print(f"⚠️ Mini App custom check: {exc}")

    return completed, total_reward


async def _miniapp_check_user_tasks(user_id: int):
    """Проверка заданий №2: продвигаемые каналы и чаты."""
    _ensure_promo_pause_monitor_started()
    await _refresh_promo_task_pauses()
    completed = 0
    total_reward = 0.0
    reward = float(await get_setting("user_task_reward") or 0.015)

    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_tasks "
            "WHERE is_active=TRUE AND task_type IN ('channel','chat') "
            "AND COALESCE(is_paused,0)=0 AND current_completions < max_completions "
            "AND id NOT IN (SELECT task_id FROM user_task_completions WHERE user_id=?)",
            (user_id,),
        ) as cursor:
            tasks = await cursor.fetchall()

    for task in tasks:
        try:
            admin_status = await _promo_bot_admin_status(task["channel_username"])
            if admin_status is not True:
                if admin_status is False:
                    async with aiosqlite.connect("bot_database.db") as db:
                        await db.execute("UPDATE user_tasks SET is_paused=1 WHERE id=?", (task["id"],))
                        await db.commit()
                continue
            if not await _promo_target_is_member(user_id, task["channel_username"]):
                continue

            newly_completed = False
            async with aiosqlite.connect("bot_database.db") as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO user_task_completions (task_id, user_id) "
                    "SELECT ?, ? WHERE EXISTS (SELECT 1 FROM user_tasks "
                    "WHERE id=? AND is_active=TRUE AND task_type IN ('channel','chat') "
                    "AND current_completions < max_completions AND COALESCE(is_paused,0)=0)",
                    (task["id"], user_id, task["id"]),
                )
                if cursor.rowcount == 1:
                    update_cursor = await db.execute(
                        "UPDATE user_tasks SET current_completions=current_completions+1 "
                        "WHERE id=? AND is_active=TRUE AND task_type IN ('channel','chat') "
                        "AND current_completions < max_completions AND COALESCE(is_paused,0)=0",
                        (task["id"],),
                    )
                    newly_completed = update_cursor.rowcount == 1
                    if newly_completed:
                        await db.execute(
                            "UPDATE user_tasks SET is_active=FALSE "
                            "WHERE id=? AND current_completions >= max_completions",
                            (task["id"],),
                        )
                    else:
                        await db.execute(
                            "DELETE FROM user_task_completions WHERE task_id=? AND user_id=?",
                            (task["id"], user_id),
                        )
                await db.commit()

            if newly_completed:
                await update_user_balance(user_id, frozen_amount=reward, completed_tasks=1)
                await add_frozen_transaction(user_id, reward, "user_task", task["id"])
                completed += 1
                total_reward += reward
        except Exception as exc:
            print(f"⚠️ Mini App user task check: {exc}")

    return completed, total_reward


async def _miniapp_check_all_tasks(message: types.Message):
    """Проверяет задания №1–№4 по действию из Mini App."""
    user_id = message.from_user.id
    language = (getattr(message.from_user, "language_code", None) or "ru")[:16]
    completed = 0
    total_reward = 0.0

    checks = (
        ("№1", lambda: _miniapp_check_flyer_and_custom(user_id, language)),
        ("№2", lambda: _miniapp_check_user_tasks(user_id)),
        ("№3", lambda: piarflow_check_and_complete(user_id)),
        ("№4", lambda: pulsar_check_and_complete(user_id, language)),
    )
    for label, check in checks:
        try:
            done, reward = _miniapp_normalize_check_result(await check())
            completed += done
            total_reward += reward
        except Exception as exc:
            print(f"⚠️ Mini App check {label}: {exc}")

    if completed > 0:
        asyncio.create_task(show_ad_at_opportunity(user_id, "after_task"))
        result_text = _task_check_success_text(completed, total_reward)
    else:
        result_text = _task_check_empty_text()

    await message.answer(
        result_text,
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


# ============================================================
# Mini App: данные из веб-приложения приходят обычным сообщением.
# ============================================================
@dp.message(F.web_app_data)
async def miniapp_web_app_data(message: types.Message, state: FSMContext):
    """Обрабатывает действия Mini App, не дублируя бизнес-логику вывода."""
    user_id = message.from_user.id
    try:
        payload = json.loads(message.web_app_data.data)
    except Exception:
        await message.answer("❌ Не удалось прочитать данные Mini App. Откройте его заново из бота.")
        return

    action = payload.get("action")
    if action == "check_tasks":
        await _miniapp_check_all_tasks(message)
        return

    if action == "withdraw_usdt":
        user = await get_user(user_id)
        if not user:
            await message.answer("❌ Не удалось загрузить профиль. Попробуйте ещё раз.")
            return
        min_amount = float(await get_setting("min_withdrawal") or 0.2)
        if float(user.get("balance", 0) or 0) < min_amount:
            await message.answer(f"❌ Минимальная сумма для вывода: {min_amount:.3f} $")
            return
        await message.answer(
            "<tg-emoji emoji-id='5456348576214983692'>💰</tg-emoji> "
            "Введите сумму для вывода USDT "
            f"(доступно: {float(user.get('balance', 0) or 0):.3f} $):",
            parse_mode="HTML",
        )
        await state.set_state(WithdrawalStates.waiting_for_amount)
        return

    if action == "withdraw_stars":
        try:
            stars_amount = int(payload.get("stars") or 0)
        except (TypeError, ValueError):
            stars_amount = 0
        if not 15 <= stars_amount <= 1000:
            await message.answer("❌ Выберите от 15 до 1000 звёзд.")
            return

        usd_amount = round(stars_amount * 0.017, 6)
        if not await _withdrawal_subscriptions_ok(user_id):
            await _notify_withdrawal_subscription_required(message)
            await message.answer("Для вывода подпишитесь на канал и вступите в чат.")
            return

        user = await get_user(user_id)
        if not user:
            await message.answer("❌ Не удалось загрузить профиль. Попробуйте ещё раз.")
            return
        if float(user.get("balance", 0) or 0) + 1e-9 < usd_amount:
            await message.answer(
                f"❌ Недостаточно средств: для {stars_amount} ⭐ нужно {usd_amount:.3f} $."
            )
            return

        changed = await update_user_balance(user_id, amount=-usd_amount)
        if changed is False:
            await message.answer("❌ Не удалось списать средства. Попробуйте ещё раз.")
            return

        withdrawal_id = await create_withdrawal_request(
            user_id=user_id,
            amount=usd_amount,
            payout_method="stars",
            stars_amount=stars_amount,
        )
        await message.answer(
            "<tg-emoji emoji-id='5458870142924526130'>📨</tg-emoji> "
            "<b>Заявка на вывод Stars отправлена!</b>\n\n"
            f"Количество: <b>{stars_amount} ⭐</b>\n"
            f"Списано с баланса: <b>{usd_amount:.3f} $</b>\n\n"
            "Ожидайте решения администрации.",
            parse_mode="HTML",
        )
        await send_withdraw_request_to_admins(withdrawal_id)
        return

    await message.answer("❌ Неизвестное действие Mini App.")


# ============================================================
# Гарантированный ответ на любые необработанные callback-запросы.
# Без answerCallbackQuery Telegram показывает "зависшую" кнопку.
# ============================================================
@dp.callback_query()
async def unhandled_callback_query(call: types.CallbackQuery):
    try:
        await call.answer()
    except Exception:
        pass
