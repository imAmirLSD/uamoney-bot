# -*- coding: utf-8 -*-
"""
core.py — общее ядро проекта.
Конфигурация, bot, dp и общие ИЗМЕНЯЕМЫЕ переменные.

⚠️ Секреты хранятся в .env или переменных окружения, не в исходном коде.
"""
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
# Пути проекта определяются относительно каталога этого файла.

import asyncio
import sqlite3
import os
import json
import time
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

import asyncio

import sqlite3

try:
    import aiosqlite  # pyright: ignore[reportMissingImports]  # type: ignore[import-not-found]
except ModuleNotFoundError:
    # Keep the bot usable when the optional aiosqlite package is not installed.
    # This small compatibility layer provides the async API used in this file.
    class _AsyncCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        @property
        def description(self):
            return self._cursor.description

        @property
        def lastrowid(self):
            return self._cursor.lastrowid

        async def fetchone(self):
            return self._cursor.fetchone()

        async def fetchall(self):
            return self._cursor.fetchall()

    class _ExecuteContext:
        def __init__(self, connection, query, parameters):
            self._connection = connection
            self._query = query
            self._parameters = parameters

        def __await__(self):
            return self._connection._execute(self._query, self._parameters).__await__()

        async def __aenter__(self):
            return await self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

    class _AsyncConnection:
        def __init__(self, path):
            self._connection = sqlite3.connect(path)

        @property
        def row_factory(self):
            return self._connection.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._connection.row_factory = value

        def execute(self, query, parameters=()):
            return _ExecuteContext(self, query, parameters)

        async def _execute(self, query, parameters=()):
            return _AsyncCursor(self._connection.execute(query, parameters))

        async def execute_fetchall(self, query, parameters=()):
            return (await self._execute(query, parameters))._cursor.fetchall()

        async def commit(self):
            self._connection.commit()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            self._connection.close()
            return False

    class _AioSQLiteFallback:
        Row = sqlite3.Row

        @staticmethod
        def connect(path):
            return _AsyncConnection(path)

    aiosqlite = _AioSQLiteFallback()

try:
    import aiogram  # type: ignore[import-not-found]
    from aiogram import Bot, Dispatcher, types, F, Router, BaseMiddleware  # type: ignore[import-not-found]
    from aiogram.filters import Command, CommandObject  # type: ignore[import-not-found]
    from aiogram.fsm.state import State, StatesGroup  # type: ignore[import-not-found]
    from aiogram.fsm.context import FSMContext  # type: ignore[import-not-found]
    from aiogram.types import (  # type: ignore[import-not-found]
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        ReplyKeyboardMarkup,
        KeyboardButton,
        Message,
        CallbackQuery,
    )
    from aiogram.utils.keyboard import (  # type: ignore[import-not-found]
        InlineKeyboardBuilder,
        ReplyKeyboardBuilder,
    )
    from aiogram.exceptions import (  # type: ignore[import-not-found]
        TelegramForbiddenError,
        TelegramBadRequest,
    )
except ImportError as exc:
    raise ImportError("aiogram is required to run this bot. Install it with `pip install aiogram`.") from exc

from datetime import datetime, timedelta

import os

try:
    from flyerapi import Flyer  # type: ignore[import-not-found]
except ImportError:
    # The optional Flyer API integration is not always installed in the runtime env.
    # Provide a lightweight fallback so the bot can still run without crashing.
    class Flyer:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

        async def get_balance(self, *args, **kwargs):
            return {"balance": 0}

        async def create_invoice(self, *args, **kwargs):
            return {"success": False, "error": "Flyer API is unavailable"}

        async def get_invoice(self, *args, **kwargs):
            return {"status": "unknown"}

        async def create_withdrawal(self, *args, **kwargs):
            return {"success": False, "error": "Flyer API is unavailable"}

        async def get_transaction(self, *args, **kwargs):
            return {"status": "unknown"}

        async def get_history(self, *args, **kwargs):
            return []

        async def set_webhook(self, *args, **kwargs):
            return False

        async def disallow(self, *args, **kwargs):
            return False

        async def request(self, *args, **kwargs):
            return {}

        async def check_status(self, *args, **kwargs):
            return {"status": "unavailable"}

import json

import time

from zoneinfo import ZoneInfo

last_withdrawal_notification = {}

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

FLYER_API_KEY = os.getenv("FLYER_API_KEY", "")

ADMIN_IDS = [7975675184]

FLZAD = 0.011

# 🔹 Глобальные переменные криптобота
DEFAULT_CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "")

CRYPTO_BOT_TOKEN: str = DEFAULT_CRYPTO_BOT_TOKEN

crypto_enabled: bool = True

# Инициализация бота
bot = Bot(token=BOT_TOKEN or "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")

dp = Dispatcher()

# В обработчике баланса резерва
from datetime import datetime, timedelta

# Часовой пояс Москва (МСК)
MSK = ZoneInfo("Europe/Moscow")

def now_msk() -> datetime:
    """Возвращает текущее московское время"""
    return datetime.now(MSK)

# ==================== GRAMADS INTEGRATION ====================
GRAMADS_API_URL = "https://api.gramads.net/ad/SendPost"

GRAMADS_TOKEN = ""

gramads_enabled = False

last_ad_times = {}  # Кеш останніх показів реклами

AD_COOLDOWN = 300  # 10 хвилин між показами

# Инициализация Flyer
flyer = None

flyer_enabled = False

async def flyer_get_tasks(user_id, language_code, limit=10):
    """Получает задания Flyer для пользователя."""
    if flyer is None:
        return []
    return await flyer.get_tasks(user_id, language_code, limit=limit)


_user_notification_times = {}


async def get_user_last_notification(user_id):
    """Возвращает время последнего уведомления пользователя в формате ISO."""
    return _user_notification_times.get(str(user_id))


async def update_user_notification_time(user_id):
    """Сохраняет время отправки последнего уведомления пользователя."""
    _user_notification_times[str(user_id)] = datetime.now().isoformat()

# Проверка новых заданий Flyer без отправки личных уведомлений
async def check_new_tasks_for_user(user_id, language_code):
    """Проверяет наличие заданий Flyer; пользователям ничего не отправляет."""
    if not flyer_enabled:
        print(f"❌ Flyer отключен для пользователя {user_id}")
        return

    try:
        # Не отправляем запрос Flyer от имени самого бота: bot ID не является user ID.
        try:
            own_bot_id = int(BOT_TOKEN.split(":", 1)[0])
        except (AttributeError, ValueError):
            own_bot_id = None

        if own_bot_id is not None and int(user_id) == own_bot_id:
            print(f"⏭️ Пропускаем собственный аккаунт бота в Flyer: {user_id}")
            return

        if len(str(user_id)) <= 6:
            print(f"⏭️ Пропускаем технический ID: {user_id}")
            return

        tasks = await flyer_get_tasks(user_id, language_code, limit=10)
        print(f"🔍 Для пользователя {user_id} получено {len(tasks)} заданий")
        if tasks:
            print(f"ℹ️ У пользователя {user_id} есть задания Flyer; личное уведомление отключено")
    except Exception as e:
        print(f"❌ Ошибка проверки заданий для пользователя {user_id}: {e}")

async def get_all_users():
    """Возвращает ID всех пользователей из таблицы users."""
    database_path = os.getenv("DATABASE_PATH", "database.db")
    async with aiosqlite.connect('bot_database.db') as db:
        cursor = await db.execute("SELECT user_id FROM users")
        return await cursor.fetchall()

async def scheduled_task_checker():
    """Періодично перевіряє нові завдання для всіх користувачів"""
    while True:
        try:
            if flyer_enabled:
                users = await get_all_users()
                print(f"🔍 Проверяем новые задания для {len(users)} пользователей...")

                # Обмежте кількість одночасних перевірок
                semaphore = asyncio.Semaphore(10)  # Не більше 10 одночасно

                async def check_user_with_semaphore(user_id):
                    async with semaphore:
                        try:
                            await check_new_tasks_for_user(user_id, 'ru')
                            await asyncio.sleep(0.1)  # Зменшена затримка
                        except Exception as e:
                            print(f"Error checking tasks for user {user_id}: {e}")

                # Запускаємо перевірку паралельно
                tasks = [check_user_with_semaphore(user[0]) for user in users]
                await asyncio.gather(*tasks, return_exceptions=True)

            # ✅ Збільште інтервал до 30 хвилин
            print("⏰ Следующая проверка заданий через 30 минут...")
            await asyncio.sleep(1800)  # 30 хвилин

        except Exception as e:
            print(f"Error in scheduled task checker: {e}")
            await asyncio.sleep(300)

# Получение пользователя из базы данных
async def get_user(user_id):
    database_path = os.getenv("DATABASE_PATH", "database.db")
    async with aiosqlite.connect('bot_database.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        )
        user = await cursor.fetchone()
        return dict(user) if user is not None else None


# Проверка блокировки пользователя
async def check_user_blocked(user_id):
    user = await get_user(user_id)
    return bool(user and user.get("is_blocked", False))

from math import ceil

async def is_valid_bot_link(link: str) -> bool:
    """Просто проверяет, является ли текст ссылкой"""
    return link.startswith("https://t.me/") or link.startswith("@")

async def is_user_subscribed(bot, user_id: int, channel_username: str) -> bool:
    """
    Перевіряє, чи користувач підписаний на канал.
    Повертає True, якщо підписаний, інакше False.
    """
    try:
        if not channel_username.startswith("@"):
            channel_username = f"@{channel_username}"

        chat = await bot.get_chat(channel_username)
        member = await bot.get_chat_member(chat.id, user_id)
        return member.status in ("member", "administrator", "creator")

    except Exception:
        # Якщо бот не має доступу до каналу або помилка — вважаємо, що не підписаний
        return False

def normalize_tg_link(value: str) -> str:
    if not value:
        return ""
    value = value.strip()
    if value.startswith("@"):
        return f"https://t.me/{value[1:]}"
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://t.me/{value}"

from datetime import datetime, timedelta

USERS_PER_PAGE = 10  # количество пользователей на одной странице

# ----------------- НАЧАЛО: Блок управления требованиями для вывода -----------------
ALL_LANGUAGES = [
    ("en", "🇺🇸", "Английский"),
    ("ar", "🇸🇦", "Арабский"),
    ("be", "🇧🇾", "Белорусский"),
    ("hu", "🇭🇺", "Венгерский"),
    ("he", "🇮🇱", "Иврит"),
    ("id", "🇮🇩", "Индонезийский"),
    ("es", "🇪🇸", "Испанский"),
    ("it", "🇮🇹", "Итальянский"),
    ("kk", "🇰🇿", "Казахский"),
    ("ca", "🇪🇸", "Каталанский"),
    ("ko", "🇰🇷", "Корейский"),
    ("ms", "🇲🇾", "Малайский"),
    ("de", "🇩🇪", "Немецкий"),
    ("nl", "🇳🇱", "Нидерландский"),
    ("nb", "🇳🇴", "Норвежский букмол"),
    ("fa", "🇮🇷", "Персидский"),
    ("pl", "🇵🇱", "Польский"),
    ("pt", "🇵🇹", "Португальский"),
    ("ru", "🇷🇺", "Русский"),
    ("sr", "🇷🇸", "Сербский"),
    ("sk", "🇸🇰", "Словацкий"),
    ("tr", "🇹🇷", "Турецкий"),
    ("uz", "🇺🇿", "Узбекский"),
    ("uk", "🇺🇦", "Украинский"),
    ("fi", "🇫🇮", "Финский"),
    ("fr", "🇫🇷", "Французский"),
    ("hr", "🇭🇷", "Хорватский"),
    ("cs", "🇨🇿", "Чешский"),
    ("sv", "🇸🇪", "Шведский")

]

LANGS_PER_PAGE = 9  # 3 buttons per row, 3 rows per сторінку

async def get_reserve_status(total_balance: float):
    if total_balance < 1:
        return "🚨 КРИТИЧЕСКИ НИЗКИЙ", "🔴", "❌ СРОЧНО ПОПОЛНИТЕ РЕЗЕРВ!"
    elif total_balance < 3:
        return "⚠️ НИЗКИЙ", "🟡", "💡 Рекомендуется пополнить резерв"
    elif total_balance < 5:
        return "ℹ️ СРЕДНИЙ", "🟢", "✅ Резерв в норме"
    else:
        return "✅ ДОСТАТОЧНЫЙ", "💚", "🎉 Отличный запас!"

async def check_channel_subscription(task_id, user_id, task, call):
    """Проверяет подписку на канал"""
    try:
        channel_username = task['channel_username']

        # Проверяем подписку
        chat_member = await bot.get_chat_member(f"@{channel_username}", user_id)

        if chat_member.status in ['member', 'administrator', 'creator']:
            # Пользователь подписан - начисляем награду
            reward_handler = globals().get("complete_and_reward")
            if callable(reward_handler):
                await reward_handler(task_id, user_id, task, call)
            else:
                await call.answer(
                    "✅ Подписка подтверждена, но обработчик начисления награды не настроен.",
                    show_alert=True
                )
        else:
            await call.answer(
                f"❌ Вы не подписаны на канал @{channel_username}!\n\n"
                f"Подпишитесь и попробуйте снова.",
                show_alert=True
            )

    except Exception as e:
        print(f"❌ Ошибка проверки подписки: {e}")

        error_msg = str(e).lower()

        if "member list is inaccessible" in error_msg:
            await call.answer(
                f"🔒 <b>Ошибка прав доступа!</b>\n\n"
                f"Бот @{(await bot.get_me()).username} не имеет права 'Просмотр участников' в канале @{task['channel_username']}\n\n"
                f"📋 <b>Решение:</b>\n"
                f"1. Откройте настройки канала\n"
                f"2. Перейдите в 'Администраторы'\n" 
                f"3. Найдите @{(await bot.get_me()).username}\n"
                f"4. Включите право <b>'Просмотр участников'</b>\n\n"
                f"⚠️ Без этого права проверка подписки невозможна",
                parse_mode='HTML',
                show_alert=True
            )
        elif "user not found" in error_msg or "chat not found" in error_msg:
            await call.answer(
                f"❌ Ошибка проверки подписки!\n\n"
                f"Бот не добавлен в администраторы канала @{task['channel_username']}\n"
                f"или канал не существует.",
                show_alert=True
            )
        else:
            await call.answer(
                f"❌ Ошибка проверки подписки!\n\n{str(e)}",
                show_alert=True
            )

# РЕЄСТРАЦІЯ (додайте це там, де ініціалізуєте dp)
async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found, reportMissingImports]
except ModuleNotFoundError:
    Image = None
    ImageDraw = None
    ImageFont = None

STATS_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats_template.png")
STATS_FONT_PATH = r"C:\Windows\Fonts\arialbd.ttf"  # Arial Bold из Windows


def render_stats_image(users_total, users_today, tasks_done,
                       withdrawn_total, withdrawn_today, deposits,
                       path="stats_temp.png"):
    if Image is None or ImageDraw is None or ImageFont is None:
        raise RuntimeError("Pillow (PIL) is required to render stats image. Install it with: pip install pillow")

    img = Image.open(STATS_TEMPLATE).convert("RGBA")
    W, H = img.size
    d = ImageDraw.Draw(img)

    try:
        f_val = ImageFont.truetype(STATS_FONT_PATH, int(H * 0.044))
    except (OSError, IOError):
        # На Linux/сервере Windows-шрифт может отсутствовать.
        try:
            f_val = ImageFont.truetype("DejaVuSans-Bold.ttf", int(H * 0.044))
        except (OSError, IOError):
            f_val = ImageFont.load_default()

    # центры под подписями, в долях высоты шаблона
    values = [
        (str(users_total),            0.3742),  # Пользователей всего
        (str(tasks_done),             0.5205),  # Выполнено заданий
        (f"{withdrawn_total:.2f}$",   0.6631),  # Выведено всего
        (f"{deposits:.2f}$",          0.8019),  # Пополнено средств
    ]
    for text, fy in values:
        d.text((W / 2, fy * H), text, font=f_val,
               fill=(255, 255, 255), anchor="mm")

    img.convert("RGB").save(path)
    return os.path.abspath(path)

api_key = None
piarflow_enabled = False
piarflow_pending = {}      # user_id -> {signature: link} — задания на проверке
piarflow_completed = {}    # user_id -> {signature: {"link":..., "ts":...}} — выполненные на контроле отписок
PIARFLOW_REWARD_PERCENT = 100  # сколько % от цены спонсора платить пользователю

