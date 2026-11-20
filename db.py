# -*- coding: utf-8 -*-
"""Работа с базой данных: init_db, настройки, пользователи, задания."""
import asyncio
import sqlite3
import json
import time
import random
import os
import re
import aiohttp
import aiogram
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import core
from core import *
from states import *


async def get_setting(key):
    try:
        async with aiosqlite.connect('bot_database.db') as db:
            # Проверяем существование таблицы
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'") as cursor:
                if not await cursor.fetchone():
                    return None

            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None
    except Exception as e:
        print(f"❌ Ошибка получения настройки {key}: {e}")
        return None

async def set_setting(key, value):
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()

# Инициализация базы данных
async def init_db():
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0,
                frozen_balance REAL DEFAULT 0,
                completed_tasks INTEGER DEFAULT 0,
                referrer_id INTEGER,
                is_blocked BOOLEAN DEFAULT FALSE,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ad_balance REAL DEFAULT 0,
                ref_paid_level1 INTEGER DEFAULT 0,
                ref_paid_level2 INTEGER DEFAULT 0
            )
        ''')

        # Пожизненный заработок пользователя для рейтинга «Топ богачей».
        try:
            await db.execute("ALTER TABLE users ADD COLUMN lifetime_earned REAL DEFAULT 0")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"⚠️ lifetime_earned уже существует или ошибка: {e}")

        try:
            await db.execute("ALTER TABLE users ADD COLUMN ref_paid_level1 INTEGER DEFAULT 0")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"⚠️ ref_paid_level1 уже существует или ошибка: {e}")

        try:
            await db.execute("ALTER TABLE users ADD COLUMN ref_paid_level2 INTEGER DEFAULT 0")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"⚠️ ref_paid_level2 уже существует или ошибка: {e}")

        try:
            await db.execute("ALTER TABLE users ADD COLUMN ref_paid_in_progress_lvl1 INTEGER DEFAULT 0")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"⚠️ ref_paid_in_progress_lvl1 уже существует или ошибка: {e}")

        try:
            await db.execute("ALTER TABLE users ADD COLUMN ref_paid_in_progress_lvl2 INTEGER DEFAULT 0")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"⚠️ ref_paid_in_progress_lvl2 уже существует или ошибка: {e}")

        await db.execute('''
            CREATE TABLE IF NOT EXISTS custom_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                url TEXT NOT NULL,
                button_text TEXT DEFAULT 'Выполнить',
                price REAL DEFAULT 0.015,
                max_completions INTEGER DEFAULT 0,
                current_completions INTEGER DEFAULT 0,
                task_type TEXT DEFAULT 'url',  -- 'url', 'subscribe_channel'
                channel_username TEXT,  -- для заданий типа подписка
                is_active BOOLEAN DEFAULT TRUE,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS frozen_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                task_type TEXT,
                task_id INTEGER,
                frozen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                unfreeze_at TIMESTAMP,
                is_unfrozen BOOLEAN DEFAULT FALSE
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                receipt_url TEXT,
                admin_receipt_url TEXT,
                status TEXT DEFAULT 'pending',
                reject_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                admin_id INTEGER
            )
        ''')
        # Метод выплаты и число Telegram Stars для новых заявок.
        for column_name, statement in (
            ("payout_method", "ALTER TABLE withdrawals ADD COLUMN payout_method TEXT DEFAULT 'usdt'"),
            ("stars_amount", "ALTER TABLE withdrawals ADD COLUMN stars_amount INTEGER"),
        ):
            try:
                await db.execute(statement)
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    raise


        # Однократно/повторно безопасно восстанавливаем минимум исторического заработка
        # по текущему и замороженному балансу плюс уже одобренным выводам.
        await db.execute("""
            UPDATE users
            SET lifetime_earned = MAX(
                COALESCE(lifetime_earned, 0),
                COALESCE(balance, 0) + COALESCE(frozen_balance, 0) + COALESCE((
                    SELECT SUM(w.amount)
                    FROM withdrawals w
                    WHERE w.user_id = users.user_id AND w.status = 'approved'
                ), 0)
            )
        """)

        await db.execute('''
            CREATE TABLE IF NOT EXISTS balance_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                admin_id INTEGER,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS daily_bonus_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_daily_bonus_claims_user_time
            ON daily_bonus_claims (user_id, claimed_at)
        ''')

        # Pulsar Ads — задания №4 (список спонсоров). Связка спонсор -> награда,
        # чтобы можно было списать выплату при отписке.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS pulsar_links (
                user_id INTEGER NOT NULL,
                signature TEXT NOT NULL,
                link TEXT NOT NULL,
                sponsor_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, signature)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS pulsar_users (
                user_id INTEGER PRIMARY KEY,
                language_code TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS custom_task_completions (
                task_id INTEGER,
                user_id INTEGER,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (task_id, user_id)
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_notifications (
                user_id INTEGER PRIMARY KEY,
                last_notification TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS completed_tasks (
                user_id INTEGER,
                signature TEXT,
                status TEXT,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, signature)
            )
        ''')

        # Начальные настройки
        await db.execute('''
            INSERT OR IGNORE INTO settings (key, value) VALUES 
            ('min_withdrawal', '0.2'),
            ('currency', 'USD'),
            ('payments_channel', ''),
            ('chat_link', ''),
            ('pulsar_platform_id', ''),
            ('pulsar_api_key', ''),
            ('pulsar_base_url', 'https://pulsarx.sale/api/v1'),
            ('pulsar_mini_app_url', 'https://pulsarx.sale/bot-tasks/index.html?platform_id=991')
        ''')
        # === Нові таблиці для продвижения ===
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_link TEXT NOT NULL,
                channel_username TEXT,
                price REAL DEFAULT 0.015,
                max_completions INTEGER NOT NULL,
                current_completions INTEGER DEFAULT 0,
                created_by INTEGER,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                task_type TEXT DEFAULT 'channel'
            )
        ''')
        try:
            await db.execute("ALTER TABLE user_tasks ADD COLUMN task_type TEXT DEFAULT 'channel'")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"⚠️ task_type уже существует или ошибка: {e}")


        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_task_completions (
                task_id INTEGER,
                user_id INTEGER,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (task_id, user_id)
            )
        ''')

        await db.execute('''
            INSERT OR IGNORE INTO settings (key, value) VALUES ('promotion_price', '0.015')
        ''')
        await db.execute('''
            INSERT OR IGNORE INTO settings (key, value) VALUES 
            ('ref_bonus_level1', '0.05'),
            ('ref_bonus_level2', '0.03'),
            ('ref_tasks_required_level1', '3'),
            ('ref_tasks_required_level2', '5')
        ''')

        try:
            await db.execute("ALTER TABLE users ADD COLUMN ref_paid_level1 INTEGER DEFAULT 0")
        except:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN ref_paid_level2 INTEGER DEFAULT 0")
        except:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN ref_tasks_completed INTEGER DEFAULT 0")
        except:
            pass
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # якщо нема запису — додаємо дефолтне значення
        await db.execute('INSERT OR IGNORE INTO settings (key, value) VALUES ("user_task_reward", "0.015")')

    # 🔹 Добавляем поле ad_balance, если его нет
        try:
            await db.execute("ALTER TABLE users ADD COLUMN ad_balance REAL DEFAULT 0")
        except:
            pass
        await db.execute('''
            CREATE TABLE IF NOT EXISTS ad_topups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                invoice_id TEXT UNIQUE,
                amount REAL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP
            )
        ''')

        try:
            await db.execute("ALTER TABLE frozen_transactions ADD COLUMN penalty_applied INTEGER DEFAULT 0;")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"⚠️ penalty_applied already exists or error: {e}")

        try:
            await db.execute("ALTER TABLE frozen_transactions ADD COLUMN penalty_amount REAL DEFAULT 0;")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"⚠️ penalty_amount already exists or error: {e}")

        await db.commit()

async def get_task_status(user_id, signature):
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute(
            'SELECT status FROM completed_tasks WHERE user_id = ? AND signature = ?', 
            (user_id, signature)
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None

async def set_task_status(user_id, signature, status):
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('''
            INSERT OR REPLACE INTO completed_tasks (user_id, signature, status) 
            VALUES (?, ?, ?)
        ''', (user_id, signature, status))
        await db.commit()

# -------------------------
# Подсчёт заявок по статусу
# -------------------------
async def count_withdrawals_by_status(status: str) -> int:
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute("SELECT COUNT(*) FROM withdrawals WHERE status = ?", (status,)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0


async def get_user(user_id):
    async with aiosqlite.connect('bot_database.db') as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)) as cursor:
            user = await cursor.fetchone()

            if user:
                def safe_float(value):
                    try:
                        return float(value)
                    except (ValueError, TypeError):
                        return 0.0

                def safe_int(value):
                    try:
                        return int(value)
                    except (ValueError, TypeError):
                        return 0

                # ✅ КОРЕКТНИЙ СПОСІБ - перевіряємо чи є поле, потім отримуємо значення
                def get_field(field_name, default_value=None):
                    """Безпечно отримує значення поля з Row"""
                    if field_name in user.keys():
                        return user[field_name]
                    return default_value

                # Отримуємо всі поля безпечно
                balance = safe_float(get_field('balance', 0))
                frozen_balance = safe_float(get_field('frozen_balance', 0))
                ad_balance = safe_float(get_field('ad_balance', 0))
                completed_tasks = get_field('completed_tasks', 0)
                referrer_id = get_field('referrer_id')
                is_blocked = get_field('is_blocked', False)
                ref_paid_level1 = safe_int(get_field('ref_paid_level1', 0))
                ref_paid_level2 = safe_int(get_field('ref_paid_level2', 0))
                ref_paid_in_progress_lvl1 = safe_int(get_field('ref_paid_in_progress_lvl1', 0))
                ref_paid_in_progress_lvl2 = safe_int(get_field('ref_paid_in_progress_lvl2', 0))
                username = get_field('username', f"user_{user_id}")

                return {
                    'user_id': user_id,
                    'username': username,
                    'balance': balance,
                    'frozen_balance': frozen_balance,
                    'completed_tasks': completed_tasks,
                    'referrer_id': referrer_id,
                    'is_blocked': bool(is_blocked),
                    'ad_balance': ad_balance,
                    'ref_paid_level1': ref_paid_level1,
                    'ref_paid_level2': ref_paid_level2,
                    'ref_paid_in_progress_lvl1': ref_paid_in_progress_lvl1,
                    'ref_paid_in_progress_lvl2': ref_paid_in_progress_lvl2
                }

            # Якщо користувача немає — створюємо нового
            username = f"user_{user_id}"
            try:
                chat = await bot.get_chat(user_id)
                username = chat.username or chat.first_name or username
            except:
                pass

            # Створюємо нового користувача з усіма полями
            async with aiosqlite.connect('bot_database.db') as db:
                await db.execute('''
                    INSERT OR IGNORE INTO users 
                    (user_id, username, balance, frozen_balance, ad_balance, 
                     completed_tasks, referrer_id, is_blocked, 
                     ref_paid_level1, ref_paid_level2, 
                     ref_paid_in_progress_lvl1, ref_paid_in_progress_lvl2)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (user_id, username, 0, 0, 0, 0, None, False, 0, 0, 0, 0))
                await db.commit()

            return {
                'user_id': user_id,
                'username': username,
                'balance': 0.0,
                'frozen_balance': 0.0,
                'completed_tasks': 0,
                'referrer_id': None,
                'is_blocked': False,
                'ad_balance': 0.0,
                'ref_paid_level1': 0,
                'ref_paid_level2': 0,
                'ref_paid_in_progress_lvl1': 0,
                'ref_paid_in_progress_lvl2': 0
            }


async def update_user_balance(user_id, amount=0, frozen_amount=0, completed_tasks=0):
    print(f"🔧 update_user_balance вызвана: user_id={user_id}, amount={amount}, frozen_amount={frozen_amount}, completed_tasks={completed_tasks}")

    max_retries = 3
    retry_delay = 0.5

    for attempt in range(max_retries):
        try:
            async with aiosqlite.connect('bot_database.db') as db:
                if amount != 0:
                    await db.execute(
                        'UPDATE users SET balance = balance + ? WHERE user_id = ?',
                        (amount, user_id)
                    )
                    if amount > 0:
                        await db.execute(
                            'UPDATE users SET lifetime_earned = COALESCE(lifetime_earned, 0) + ? WHERE user_id = ?',
                            (amount, user_id)
                        )
                    print(f"💳 Изменен баланс: {amount}$")

                if frozen_amount != 0:
                    await db.execute(
                        'UPDATE users SET frozen_balance = frozen_balance + ? WHERE user_id = ?',
                        (frozen_amount, user_id)
                    )
                    if frozen_amount > 0:
                        await db.execute(
                            'UPDATE users SET lifetime_earned = COALESCE(lifetime_earned, 0) + ? WHERE user_id = ?',
                            (frozen_amount, user_id)
                        )
                    print(f"❄️ Изменен замороженный баланс: {frozen_amount}$")

                if completed_tasks != 0:
                    await db.execute(
                        'UPDATE users SET completed_tasks = completed_tasks + ? WHERE user_id = ?',
                        (completed_tasks, user_id)
                    )
                    print(f"📝 Изменено количество заданий: {completed_tasks}")

                await db.commit()
                print(f"✅ Баланс пользователя {user_id} обновлен")

                # ⬇️ ДОДАЄМО ВИКЛИК ПЕРЕВІРКИ РЕФЕРАЛЬНИХ БОНУСІВ
                if completed_tasks > 0:
                    await check_and_pay_referral_bonus(user_id)

                return True

        except Exception as e:
            print(f"❌ Попытка {attempt + 1}/{max_retries} не удалась: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay * (attempt + 1))
            else:
                print(f"❌ Не удалось обновить баланс пользователя {user_id} после {max_retries} попыток")
                return False

async def safe_db_operation(operation, *args, **kwargs):
    """Безопасное выполнение операций с БД с повторными попытками"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return await operation(*args, **kwargs)
        except Exception as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            else:
                raise e




async def create_withdrawal_request(user_id, amount, payout_method="usdt", stars_amount=None):
    async with aiosqlite.connect('bot_database.db') as db:
        cursor = await db.execute(
            'INSERT INTO withdrawals (user_id, amount, payout_method, stars_amount) VALUES (?, ?, ?, ?)',
            (user_id, amount, payout_method, stars_amount)
        )
        await db.commit()
        return cursor.lastrowid

async def get_pending_withdrawals():
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('''
            SELECT w.*, u.username 
            FROM withdrawals w 
            LEFT JOIN users u ON w.user_id = u.user_id 
            WHERE w.status = 'pending'
            ORDER BY w.created_at
        ''') as cursor:
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in await cursor.fetchall()]

async def get_withdrawal(withdrawal_id):
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('''
            SELECT w.*, u.username 
            FROM withdrawals w 
            LEFT JOIN users u ON w.user_id = u.user_id 
            WHERE w.id = ?
        ''', (withdrawal_id,)) as cursor:
            columns = [column[0] for column in cursor.description]
            row = await cursor.fetchone()
            return dict(zip(columns, row)) if row else None





async def update_withdrawal_status(withdrawal_id, status, admin_id=None, admin_receipt_url=None, reject_reason=None):
    try:
        async with aiosqlite.connect('bot_database.db') as db:
            if status == 'approved':
                await db.execute('''
                    UPDATE withdrawals 
                    SET status = ?, admin_id = ?, admin_receipt_url = ?, processed_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                ''', (status, admin_id, admin_receipt_url, withdrawal_id))

            elif status in ('rejected', 'declined'):
                # declined == отклонено без возврата
                await db.execute('''
                    UPDATE withdrawals 
                    SET status = ?, admin_id = ?, reject_reason = ?, processed_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                ''', (status, admin_id, reject_reason or 'Отклонено администратором', withdrawal_id))

            elif status == 'refunded':
                await db.execute('''
                    UPDATE withdrawals 
                    SET status = ?, admin_id = ?, processed_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (status, admin_id, withdrawal_id))

            await db.commit()

            # Возвращаем средства если статус подразумевает возврат
            if status in ('rejected', 'refunded') or status == 'rejected':
                # Если логіка твоя — only for 'refunded' возврат, то убери 'rejected'
                if status == 'refunded':
                    withdrawal = await get_withdrawal(withdrawal_id)
                    if withdrawal:
                        await update_user_balance(withdrawal['user_id'], amount=withdrawal['amount'])

    except Exception as e:
        print(f"❌ Ошибка при обновлении статуса вывода: {e}")

async def get_referral_data(user_id):
    async with aiosqlite.connect('bot_database.db') as db:
        # Рефералы 1 уровня
        async with db.execute('SELECT COUNT(*) FROM users WHERE referrer_id = ?', (user_id,)) as cursor:
            level1_count = (await cursor.fetchone())[0]

        # Рефералы 2 уровня
        async with db.execute('''
            SELECT COUNT(*) FROM users 
            WHERE referrer_id IN (SELECT user_id FROM users WHERE referrer_id = ?)
        ''', (user_id,)) as cursor:
            level2_count = (await cursor.fetchone())[0]

        return {'level1_count': level1_count, 'level2_count': level2_count}

async def get_all_users():
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('SELECT user_id, username FROM users WHERE is_blocked = FALSE') as cursor:
            return await cursor.fetchall()

async def update_user_notification_time(user_id):
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('''
            INSERT OR REPLACE INTO user_notifications (user_id, last_notification) 
            VALUES (?, CURRENT_TIMESTAMP)
        ''', (user_id,))
        await db.commit()

async def get_user_last_notification(user_id):
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('SELECT last_notification FROM user_notifications WHERE user_id = ?', (user_id,)) as cursor:
            result = await cursor.fetchone()
            if result:
                print(f"📅 В базе: пользователь {user_id} - последнее уведомление {result[0]}")
            else:
                print(f"📅 В базе: пользователь {user_id} - записей об уведомлениях нет")
            return result[0] if result else None

async def get_user_promo_counts(user_id: int):
    """Возвращает количество активных и завершённых заказов"""
    async with aiosqlite.connect("bot_database.db") as db:
        # Активные
        async with db.execute(
            "SELECT COUNT(*) FROM user_tasks WHERE created_by=? AND is_active=TRUE",
            (user_id,)
        ) as cursor:
            active = (await cursor.fetchone())[0]

        # Завершённые
        async with db.execute(
            "SELECT COUNT(*) FROM user_tasks WHERE created_by=? AND is_active=FALSE",
            (user_id,)
        ) as cursor:
            done = (await cursor.fetchone())[0]

        return active, done


async def get_expected_withdrawal(user_id: int) -> float:
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute("""
            SELECT SUM(amount)
            FROM withdrawals
            WHERE user_id = ? AND status = 'pending'
        """, (user_id,)) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row and row[0] else 0.0

async def get_withdrawn_amount(user_id: int) -> float:
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute("""
            SELECT SUM(amount)
            FROM withdrawals
            WHERE user_id = ? AND status = 'approved'
        """, (user_id,)) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row and row[0] else 0.0


# Вспомогательная функция для отображения деталей пользователя
async def show_user_details(call: types.CallbackQuery, user_id: int):
    user = await get_user(user_id)

    if not user:
        await call.message.edit_text("❌ Пользователь не найден")
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

    keyboard = InlineKeyboardBuilder()

    keyboard.button(text="💰 Начислить", callback_data=f"add_balance_{user_id}")
    keyboard.button(text="➖ Снять", callback_data=f"remove_balance_{user_id}")

    if user['is_blocked']:
        keyboard.button(text="🔓 Разблокировать", callback_data=f"unban_{user_id}")
    else:
        keyboard.button(text="🔒 Заблокировать", callback_data=f"ban_{user_id}")

    keyboard.button(text="👥 Рефералы", callback_data=f"show_refs_{user_id}")
    keyboard.button(text="📊 Статистика", callback_data=f"user_stats_{user_id}")

    keyboard.button(text="🗑 Очистить БД", callback_data=f"clean_userdb:{user_id}")
    from handlers_admin import users_back  # отложенный импорт (защита от цикла)
    keyboard.button(text="🔙 Назад", callback_data="users_back")
    keyboard.adjust(2, 2, 1, 1)

    await call.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode='HTML')

async def check_completed_tasks():
    'Резервная проверка Flyer-отписок по точной подписи каждой выплаты.'
    from services import flyer_check_task

    while True:
        try:
            if not core.flyer_enabled:
                print("❌ Flyer отключен — пропускаем резервную проверку отписок")
                await asyncio.sleep(3600)
                continue

            async with aiosqlite.connect('bot_database.db') as db:
                db.row_factory = aiosqlite.Row
                async with db.execute('''
                    SELECT ft.id, ft.user_id, ft.amount, ft.task_type, ft.task_id,
                           u.username, ft.frozen_at, ft.unfreeze_at,
                           ft.is_unfrozen, ft.penalty_applied
                    FROM frozen_transactions ft
                    JOIN users u ON u.user_id = ft.user_id
                    WHERE ft.is_unfrozen = FALSE
                      AND ft.task_type = 'core.flyer'
                      AND ft.penalty_applied = 0
                      AND ft.task_id IS NOT NULL
                ''') as cursor:
                    transactions = await cursor.fetchall()

            print(f"🔍 Flyer: резервная проверка {len(transactions)} активных выплат")
            penalties = 0
            for tx_row in transactions:
                user_id = int(tx_row[1])
                signature = str(tx_row[4] or '').strip()
                if not signature:
                    continue

                # Проверяем только подпись, записанную в этой транзакции.
                first_status = await flyer_check_task(signature, user_id)
                if first_status is None or str(first_status).lower() != 'abort':
                    continue

                # Вебхук должен обработать событие сразу; это резерв на случай потери POST.
                await asyncio.sleep(600)
                final_status = await flyer_check_task(signature, user_id)
                if final_status is None or str(final_status).lower() != 'abort':
                    continue

                if await apply_penalty_for_transaction(tx_row):
                    penalties += 1
                    print(f"🚨 Flyer: подтверждена отписка user_id={user_id}, signature={signature}")
                await asyncio.sleep(1)

            print(f"📊 Flyer: резервная проверка завершена. Штрафов: {penalties}")
            await asyncio.sleep(3600)
        except Exception as e:
            print(f"❌ Ошибка резервной проверки Flyer-отписок: {e}")
            await asyncio.sleep(900)


async def apply_penalty_for_transaction(tx_row):
    'Атомарно списывает только сумму награды по одной транзакции, без дополнительной надбавки.'
    try:
        tx_id = int(tx_row[0])
        async with aiosqlite.connect('bot_database.db') as db:
            await db.execute('BEGIN IMMEDIATE')
            async with db.execute('''
                SELECT id, user_id, amount, task_type, task_id, is_unfrozen, penalty_applied
                FROM frozen_transactions WHERE id = ?
            ''', (tx_id,)) as cur:
                tx = await cur.fetchone()

            if not tx or bool(tx[5]) or bool(tx[6]):
                await db.rollback()
                return False

            user_id = int(tx[1])
            amount = max(0.0, float(tx[2] or 0.0))
            task_type = str(tx[3] or '')
            task_ref = tx[4]
            if amount <= 0:
                await db.rollback()
                return False

            async with db.execute(
                'SELECT frozen_balance FROM users WHERE user_id = ?', (user_id,)
            ) as cur:
                user_row = await cur.fetchone()
            if not user_row:
                await db.rollback()
                print(f"⚠️ Не найден пользователь при штрафе: {user_id}")
                return False

            frozen_balance = max(0.0, float(user_row[0] or 0.0))
            # Списываем только из заморозки. Если награда уже разморожена
            # (или заморозки недостаточно), обычный баланс не трогаем.
            if frozen_balance + 1e-9 < amount:
                await db.rollback()
                print(f"ℹ️ Отписка: награда {tx_id} уже не находится полностью в заморозке; списание пропущено")
                return False
            frozen_to_deduct = amount
            penalty_total = round(amount, 6)

            await db.execute('''
                UPDATE users
                SET frozen_balance = frozen_balance - ?
                WHERE user_id = ? AND frozen_balance >= ?
            ''', (frozen_to_deduct, user_id, frozen_to_deduct))

            cur = await db.execute('''
                UPDATE frozen_transactions
                SET is_unfrozen = TRUE, penalty_applied = 1, penalty_amount = ?
                WHERE id = ? AND is_unfrozen = FALSE AND penalty_applied = 0
            ''', (penalty_total, tx_id))
            if cur.rowcount != 1:
                await db.rollback()
                return False

            await db.execute('''
                INSERT INTO balance_history (user_id, amount, type, admin_id, reason)
                VALUES (?, ?, 'penalty', NULL, ?)
            ''', (user_id, -penalty_total, f"Штраф за отписку ({task_type}:{task_ref})"))

            if task_type == 'core.flyer' and task_ref is not None:
                cur = await db.execute('''
                    UPDATE completed_tasks SET status = 'unsubscribed'
                    WHERE user_id = ? AND signature = ?
                ''', (user_id, str(task_ref)))
                if cur.rowcount == 0:
                    await db.execute('''
                        INSERT OR REPLACE INTO completed_tasks (user_id, signature, status)
                        VALUES (?, ?, 'unsubscribed')
                    ''', (user_id, str(task_ref)))

            await db.commit()

        # Личное уведомление пользователю об отписке отключено.
        # Само применение штрафа и уведомление администраторов сохранены.

        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🔔 Flyer: у пользователя {user_id} списана награда {penalty_total:.6f}$ "
                    f"за {task_type}:{task_ref} (без дополнительной надбавки)"
                )
            except Exception:
                pass

        print(f"✅ Штраф применён: user_id={user_id}, signature={task_ref}, сумма={penalty_total:.6f}$")
        return True
    except Exception as e:
        print(f"❌ Ошибка apply_penalty_for_transaction: {e}")
        return False




async def complete_and_reward(task_id, user_id, task, call):
    """Начисляет награду за выполненное задание"""

    print(f"💰 Начинаем начисление награды за задание {task_id}")

    # ПРОВЕРЯЕМ не выполнял ли уже (правильная логика)
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('SELECT 1 FROM custom_task_completions WHERE task_id = ? AND user_id = ?', (task_id, user_id)) as cursor:
            already_completed = await cursor.fetchone()

    if already_completed:
        print(f"❌ Пользователь {user_id} уже выполнял задание {task_id}")
        await call.answer("❌ Вы уже выполняли это задание", show_alert=True)
        return

    # Отмечаем как выполненное
    from handlers_custom_tasks import complete_custom_task  # отложенный импорт (защита от цикла)
    success = await complete_custom_task(task_id, user_id)
    if not success:
        print(f"❌ Ошибка при отметке задания {task_id} как выполненного")
        await call.answer("❌ Ошибка при отметке задания как выполненного", show_alert=True)
        return

    reward = task['price']
    print(f"💵 Награда: {reward}$")

    # Замораживаем средства
    await update_user_balance(user_id, frozen_amount=reward)
    await update_user_balance(user_id, completed_tasks=1)

    # Записываем транзакцию для автоматического размороживания
    await add_frozen_transaction(user_id, reward, 'custom_subscribe', task_id)

    print(f"✅ Награда {reward}$ заморожена для пользователя {user_id}")
    await call.answer(f"✅ Задание выполнено! Награда {reward}$ заморожена на 24 часов.", show_alert=True)

    # Удаляем сообщение
    try:
        await call.message.delete()
        print("✅ Сообщение с заданием удалено")
    except Exception as e:
        print(f"⚠️ Не удалось удалить сообщение: {e}")

async def update_db_structure():
    """Обновляет структуру базы данных при необходимости"""
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS daily_bonus_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_daily_bonus_claims_user_time
            ON daily_bonus_claims (user_id, claimed_at)
        ''')

        # Pulsar Ads — задания №4 (список спонсоров). Связка спонсор -> награда,
        # чтобы можно было списать выплату при отписке.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS pulsar_links (
                user_id INTEGER NOT NULL,
                signature TEXT NOT NULL,
                link TEXT NOT NULL,
                sponsor_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, signature)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS pulsar_users (
                user_id INTEGER PRIMARY KEY,
                language_code TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ПЕРВОЕ: Проверяем таблицу users на наличие referrer_id (для реферальной системы)
        async with db.execute("PRAGMA table_info(users)") as cursor:
            user_columns = [column[1] for column in await cursor.fetchall()]

        if 'referrer_id' not in user_columns:
            await db.execute('ALTER TABLE users ADD COLUMN referrer_id INTEGER')
            print("✅ Добавлена колонка referrer_id в users (для реферальной системы)")
        else:
            print("✅ Колонка referrer_id уже существует в users")

        # ВТОРОЕ: Проверяем таблицу custom_tasks
        async with db.execute("PRAGMA table_info(custom_tasks)") as cursor:
            task_columns = [column[1] for column in await cursor.fetchall()]

        if 'task_type' not in task_columns:
            await db.execute('ALTER TABLE custom_tasks ADD COLUMN task_type TEXT DEFAULT "url"')
            print("✅ Добавлена колонка task_type в custom_tasks")

        if 'channel_username' not in task_columns:
            await db.execute('ALTER TABLE custom_tasks ADD COLUMN channel_username TEXT')
            print("✅ Добавлена колонка channel_username в custom_tasks")

        await db.commit()
        print("✅ Структура базы данных успешно обновлена")

async def add_frozen_transaction(user_id, amount, task_type, task_id=None):
    """Добавляет запись о замороженных средствах"""
    try:
        async with aiosqlite.connect('bot_database.db') as db:
            await db.execute(
                'INSERT INTO frozen_transactions (user_id, amount, task_type, task_id, unfreeze_at) VALUES (?, ?, ?, ?, datetime("now", "+24 hours"))',
                (user_id, amount, task_type, task_id)
            )
            await db.commit()
            print(f"✅ Записана замороженная транзакция: {user_id} - {amount}$ - {task_type}")
    except Exception as e:
        print(f"❌ Ошибка записи замороженной транзакции: {e}")

async def auto_unfreeze_balances():
    """Автоматически размораживает средства через 24 часов"""
    while True:
        try:
            print("🔍 Проверяем средства для разморозки...")

            async with aiosqlite.connect('bot_database.db') as db:
                # Находим транзакции, которые нужно разморозить
                async with db.execute('''
                    SELECT ft.id, ft.user_id, ft.amount, u.username 
                    FROM frozen_transactions ft
                    JOIN users u ON ft.user_id = u.user_id
                    WHERE ft.unfreeze_at <= datetime('now')
                    AND ft.is_unfrozen = FALSE
                    AND COALESCE(ft.penalty_applied, 0) = 0
                    AND ft.task_type != 'piarflow'
                ''') as cursor:
                    transactions_to_unfreeze = await cursor.fetchall()

                unfrozen_count = 0
                total_amount = 0

                for transaction in transactions_to_unfreeze:
                    transaction_id, user_id, amount, username = transaction

                    try:
                        # Перемещаем средства из frozen_balance в balance
                        await db.execute(
                            'UPDATE users SET frozen_balance = frozen_balance - ?, balance = balance + ? WHERE user_id = ?',
                            (amount, amount, user_id)
                        )

                        # Помечаем транзакцию как размороженную
                        await db.execute(
                            'UPDATE frozen_transactions SET is_unfrozen = TRUE WHERE id = ?',
                            (transaction_id,)
                        )

                        unfrozen_count += 1
                        total_amount += amount

                        print(f"✅ Разморожено {amount}$ для пользователя {username} (ID: {user_id})")

                    except Exception as e:
                        print(f"❌ Ошибка разморозки для пользователя {user_id}: {e}")

                # PiarFlow-транзакции: средства начислены на frozen_balance,
                # при разморозке переносим их на основной баланс.
                async with db.execute('''
                    SELECT ft.id, ft.user_id, ft.amount, u.username
                    FROM frozen_transactions ft
                    JOIN users u ON ft.user_id = u.user_id
                    WHERE ft.unfreeze_at <= datetime('now')
                    AND ft.is_unfrozen = FALSE
                    AND COALESCE(ft.penalty_applied, 0) = 0
                    AND ft.task_type = 'piarflow'
                ''') as cursor:
                    piarflow_to_unfreeze = await cursor.fetchall()

                piarflow_unfrozen = 0
                piarflow_amount = 0.0
                for transaction in piarflow_to_unfreeze:
                    transaction_id, user_id, amount, username = transaction
                    try:
                        # Переносим средства из frozen_balance в balance
                        await db.execute(
                            'UPDATE users SET frozen_balance = frozen_balance - ?, balance = balance + ? WHERE user_id = ?',
                            (amount, amount, user_id)
                        )
                        # Помечаем транзакцию как размороженную
                        await db.execute(
                            'UPDATE frozen_transactions SET is_unfrozen = TRUE WHERE id = ?',
                            (transaction_id,)
                        )
                        piarflow_unfrozen += 1
                        piarflow_amount += amount
                        print(f"✅ PiarFlow: разморожено {amount}$ для {username} (ID: {user_id})")
                    except Exception as e:
                        print(f"❌ Ошибка разморозки PiarFlow транзакции {transaction_id}: {e}")

                if unfrozen_count > 0:
                    await db.commit()
                    print(f"✅ Всего разморожено {unfrozen_count} транзакций на сумму {total_amount:.3f}$")
                if piarflow_unfrozen > 0:
                    await db.commit()
                    print(f"✅ PiarFlow: разморожено {piarflow_unfrozen} транзакций на сумму {piarflow_amount:.3f}$")
                if unfrozen_count == 0 and piarflow_unfrozen == 0:
                    print("ℹ️ Нет транзакций для разморозки")

            # Проверяем каждые 30 минут
            await asyncio.sleep(1800)

        except Exception as e:
            print(f"❌ Ошибка в auto_unfreeze_balances: {e}")
            await asyncio.sleep(300)

async def get_total_system_balance():
    """Получает общий баланс системы"""
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute('SELECT SUM(balance) + SUM(frozen_balance) FROM users') as cursor:
            result = await cursor.fetchone()
            return result[0] or 0

async def check_and_pay_referral_bonus(user_id: int):
    """Перевіряє та виплачує реферальні бонуси ПРИ ДОСЯГНЕННІ потрібної кількості завдань"""
    try:
        user = await get_user(user_id)
        referrer_id = user.get('referrer_id')

        if not referrer_id:
            return

        # Отримуємо налаштування
        bonus_lvl1 = float(await get_setting('ref_bonus_level1') or 0.05)
        bonus_lvl2 = float(await get_setting('ref_bonus_level2') or 0.03)
        tasks_lvl1 = int(await get_setting('ref_tasks_required_level1') or 3)
        tasks_lvl2 = int(await get_setting('ref_tasks_required_level2') or 5)

        print(f"🔍 Проверка реф. бонусов для {user_id}: {user['completed_tasks']} заданий")
        print(f"  ref_paid_level1: {user.get('ref_paid_level1', 0)}")
        print(f"  ref_paid_level2: {user.get('ref_paid_level2', 0)}")

        # ✅ 1 РІВЕНЬ - виплачуємо ТІЛЬКИ КОЛИ досягаємо tasks_lvl1 І ще не платили
        if (user['completed_tasks'] >= tasks_lvl1 and 
            not user.get('ref_paid_level1', 0) and 
            not user.get('ref_paid_in_progress_lvl1', 0)):

            print(f"🎯 Умова 1 уровня выполнена! Доступно {user['completed_tasks']}/{tasks_lvl1}")

            # Захисник від повторної виплати
            async with aiosqlite.connect('bot_database.db') as db:
                await db.execute(
                    'UPDATE users SET ref_paid_in_progress_lvl1 = 1 WHERE user_id = ?',
                    (user_id,)
                )
                await db.commit()

            try:
                # Виплачуємо бонус реферу
                await update_user_balance(referrer_id, amount=bonus_lvl1)

                # Помічаємо як виплачено
                async with aiosqlite.connect('bot_database.db') as db:
                    await db.execute(
                        'UPDATE users SET ref_paid_level1 = 1 WHERE user_id = ?',
                        (user_id,)
                    )
                    await db.execute(
                        'UPDATE users SET ref_paid_in_progress_lvl1 = 0 WHERE user_id = ?',
                        (user_id,)
                    )

                    # Запис в історію
                    await db.execute(
                        'INSERT INTO balance_history (user_id, amount, type, reason) VALUES (?, ?, ?, ?)',
                        (referrer_id, bonus_lvl1, 'referral_bonus',
                         f'Реферальный бонус 1 уровня за пользователя {user_id} ({tasks_lvl1} заданий)')
                    )
                    await db.commit()

                # Повідомляємо рефера
                try:
                    await bot.send_message(
                        referrer_id,
                        f"🎉 <b>Реферальный бонус 1 уровня!</b>\n\n"
                        f"👤 Ваш реферал выполнил {tasks_lvl1} заданий\n"
                        f"💰 Вы получили: <b>{bonus_lvl1}$</b>\n\n"
                        f"👥 Приглашайте больше друзей!",
                        parse_mode='HTML'
                    )
                except Exception as e:
                    print(f"⚠️ Не удалось уведомить реферала 1 уровня: {e}")

                print(f"✅ Выплачен бонус 1 уровня: {referrer_id} +{bonus_lvl1}$ за реферала {user_id}")

            except Exception as e:
                # При ошибке сбрасываем флаг in_progress, чтобы бонус можно было выплатить позже
                print(f"❌ Ошибка выплаты бонуса 1 уровня: {e}")
                async with aiosqlite.connect('bot_database.db') as db:
                    await db.execute(
                        'UPDATE users SET ref_paid_in_progress_lvl1 = 0 WHERE user_id = ?',
                        (user_id,)
                    )
                    await db.commit()

        # ✅ 2 РІВЕНЬ - аналогічно
        if referrer_id:
            referrer = await get_user(referrer_id)
            referrer_lvl2_id = referrer.get('referrer_id')

            if (referrer_lvl2_id and 
                user['completed_tasks'] >= tasks_lvl2 and 
                not user.get('ref_paid_level2', 0) and
                not user.get('ref_paid_in_progress_lvl2', 0)):

                print(f"🎯 Умова 2 уровня выполнена! Доступно {user['completed_tasks']}/{tasks_lvl2}")

                # Захисник від повторної виплати
                async with aiosqlite.connect('bot_database.db') as db:
                    await db.execute(
                        'UPDATE users SET ref_paid_in_progress_lvl2 = 1 WHERE user_id = ?',
                        (user_id,)
                    )
                    await db.commit()

                try:
                    # Виплачуємо бонус реферу 2 рівня
                    await update_user_balance(referrer_lvl2_id, amount=bonus_lvl2)

                    # Помічаємо як виплачено
                    async with aiosqlite.connect('bot_database.db') as db:
                        await db.execute(
                            'UPDATE users SET ref_paid_level2 = 1 WHERE user_id = ?',
                            (user_id,)
                        )
                        await db.execute(
                            'UPDATE users SET ref_paid_in_progress_lvl2 = 0 WHERE user_id = ?',
                            (user_id,)
                        )

                        # Запис в історію
                        await db.execute(
                            'INSERT INTO balance_history (user_id, amount, type, reason) VALUES (?, ?, ?, ?)',
                            (referrer_lvl2_id, bonus_lvl2, 'referral_bonus',
                             f'Реферальный бонус 2 уровня за пользователя {user_id} ({tasks_lvl2} заданий)')
                        )
                        await db.commit()

                    # Повідомляємо рефера 2 рівня
                    try:
                        await bot.send_message(
                            referrer_lvl2_id,
                            f"🎉 <b>Реферальный бонус 2 уровня!</b>\n\n"
                            f"👤 Реферал вашего реферала выполнил {tasks_lvl2} заданий\n"
                            f"💰 Вы получили: <b>{bonus_lvl2}$</b>\n\n"
                            f"👥 Приглашайте больше друзей!",
                            parse_mode='HTML'
                        )
                    except Exception as e:
                        print(f"⚠️ Не удалось уведомить реферала 2 уровня: {e}")

                    print(f"✅ Выплачен бонус 2 уровня: {referrer_lvl2_id} +{bonus_lvl2}$ за реферала {user_id}")
                except Exception as e:
                    # Как и для 1 уровня: при ошибке снимаем блокировку, чтобы бонус
                    # можно было выплатить позже (в т.ч. дозачётом при старте).
                    print(f"❌ Ошибка выплаты бонуса 2 уровня: {e}")
                    async with aiosqlite.connect('bot_database.db') as db:
                        await db.execute(
                            'UPDATE users SET ref_paid_in_progress_lvl2 = 0 WHERE user_id = ?',
                            (user_id,)
                        )
                        await db.commit()

    except Exception as e:
        print(f"❌ Ошибка проверки реферальных бонусов: {e}")


async def catch_up_referral_bonuses():
    """Доплачивает бонусы тем рефералам, у кого порог уже достигнут, но выплаты не было.

    Нужно для старых начислений PiarFlow/Pulsar: они увеличивали completed_tasks
    напрямую в SQL, мимо update_user_balance(), где вызывается проверка бонусов.
    """
    try:
        async with aiosqlite.connect("bot_database.db") as db:
            async with db.execute("""
                SELECT user_id FROM users
                WHERE referrer_id IS NOT NULL
                  AND (
                    (COALESCE(ref_paid_level1, 0) = 0 AND COALESCE(ref_paid_in_progress_lvl1, 0) = 0)
                    OR
                    (COALESCE(ref_paid_level2, 0) = 0 AND COALESCE(ref_paid_in_progress_lvl2, 0) = 0)
                  )
            """) as cursor:
                candidates = [row[0] for row in await cursor.fetchall()]
    except Exception as e:
        print(f"❌ Не удалось получить список рефералов для дозачёта: {e}")
        return

    for user_id in candidates:
        try:
            await check_and_pay_referral_bonus(int(user_id))
        except Exception as e:
            print(f"❌ Ошибка дозачёта реферального бонуса {user_id}: {e}")


async def check_unsubscribed_penalties():
    while True:
        try:
            # Текущая ставка выплаты из настроек
            current_reward = float(await get_setting("user_task_reward") or 0.015)

            async with aiosqlite.connect("bot_database.db") as db:
                db.row_factory = aiosqlite.Row
                # ДОБАВЛЯЄМО ПЕРЕВІРКУ ТИПУ ЗАВДАННЯ - лише канали
                completed_rows = await db.execute_fetchall("""
                    SELECT utc.task_id, utc.user_id, utc.completed_at, ut.channel_username, ut.task_type
                    FROM user_task_completions utc
                    JOIN user_tasks ut ON utc.task_id = ut.id
                    WHERE ut.task_type = 'channel'
                """)

            now = datetime.utcnow()

            for row in completed_rows:
                user_id = row["user_id"]
                task_id = row["task_id"]
                channel_username = row["channel_username"]
                completed_at = datetime.fromisoformat(row["completed_at"])

                # Сколько дней прошло с момента выполнения
                days_since = (now - completed_at).days

                # Прошло больше 3 дней — не штрафуем
                if days_since >= 2:
                    continue

                # Проверяем, подписан ли пользователь сейчас
                is_subscribed = await is_user_subscribed(bot, user_id, channel_username)
                if is_subscribed:
                    continue  # всё в порядке, подписка есть

                # Проверяем, был ли уже штраф
                async with aiosqlite.connect("bot_database.db") as db:
                    async with db.execute("""
                        SELECT 1 FROM frozen_transactions
                        WHERE user_id = ? AND task_id = ? AND task_type = 'unsubscribe_penalty'
                    """, (user_id, task_id)) as cursor:
                        already_penalized = await cursor.fetchone()

                if already_penalized:
                    continue  # уже штрафовали

                # Применяем штраф
                async with aiosqlite.connect("bot_database.db") as db:
                    await db.execute("""
                        UPDATE users
                        SET frozen_balance = frozen_balance - ?
                        WHERE user_id = ? AND frozen_balance >= ?
                    """, (current_reward, user_id, current_reward))

                    await db.execute("""
                        INSERT INTO frozen_transactions (user_id, amount, task_type, task_id, penalty_applied, penalty_amount)
                        VALUES (?, ?, 'unsubscribe_penalty', ?, 1, ?)
                    """, (user_id, -current_reward, task_id, current_reward))

                    await db.commit()

                # Личное уведомление пользователю об отписке отключено.
                # Проверка подписки и применение штрафа сохранены.

            # Проверка каждые 10 минут
            await asyncio.sleep(3600)

        except Exception as e:
            print(f"❌ Ошибка в check_unsubscribed_penalties: {e}")
            await asyncio.sleep(60)

async def cleanup_referral_links():
    """Очищает реферальные ссылки после удаления пользователей"""
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute('''
            UPDATE users 
            SET referrer_id = NULL 
            WHERE referrer_id NOT IN (SELECT user_id FROM users)
        ''')
        await db.commit()

async def cleanup_database():

    while True:

        async with aiosqlite.connect("bot_database.db") as db:

            await db.execute("DELETE FROM user_notifications WHERE last_notification < datetime('now','-7 days')")

            await db.execute("DELETE FROM custom_task_completions WHERE completed_at < datetime('now','-10 days')")

            await db.execute("DELETE FROM user_task_completions WHERE completed_at < datetime('now','-10 days')")

            await db.execute("DELETE FROM completed_tasks WHERE completed_at < datetime('now','-10 days')")

            await db.execute("DELETE FROM frozen_transactions WHERE is_unfrozen=1 AND frozen_at < datetime('now','-10 days')")

            await db.execute("DELETE FROM withdrawals WHERE status!='approved' AND created_at < datetime('now','-20 days')")

            await db.execute("DELETE FROM ad_topups WHERE status!='paid' AND created_at < datetime('now','-20 days')")

            await db.execute("DELETE FROM balance_history WHERE created_at < datetime('now','-20 days')")

            await db.commit()

        await asyncio.sleep(172800)
