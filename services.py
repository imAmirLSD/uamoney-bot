# -*- coding: utf-8 -*-
"""Внешние интеграции: Flyer, Gramads, PiarFlow, TGRASS, CryptoBot и вебхуки."""
import asyncio, hashlib, json, random, sqlite3, time, os, inspect, hmac, ipaddress
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import aiohttp
from aiohttp import web
import core
from core import *
from db import *
from states import *
from aiogram.types import LinkPreviewOptions
from aiogram.types import FSInputFile as _AiogramFSInputFile


# ===== services_flyer.py =====
# -*- coding: utf-8 -*-
"""Интеграция Flyer: инициализация, проверки заданий, штрафы."""



# Инициализация Flyer
def init_flyer():
    try:
        if core.FLYER_API_KEY and core.FLYER_API_KEY != 'your_flyer_api_key_here':
            core.flyer = Flyer(core.FLYER_API_KEY)
            core.flyer_enabled = True
            print("✅ Flyer API инициализирован")
            return True
        else:
            print("⚠️ Flyer API ключ не настроен")
            return False
    except Exception as e:
        print(f"❌ Ошибка инициализации Flyer: {e}")
        return False

async def flyer_get_tasks(user_id, language_code, limit=15):

    if not core.flyer_enabled or not core.flyer:
        print("❌ Flyer не инициализирован")
        return []

    try:
        tasks = await core.flyer.get_tasks(
            user_id=user_id,
            language_code=language_code,
            limit=limit
        )

        if tasks:
            print(f"✅ Flyer вернул {len(tasks)} заданий для пользователя {user_id}")

            # 🔍 Додаємо детальне логування для діагностики
            print("📦 FLYER RAW TASKS (DEBUG):")
            for i, task in enumerate(tasks, 1):
                try:
                    print(f"\n🔸 Task #{i}")
                    print(f"Задание: {task.get('task')}")
                    print(f"Цена: {task.get('price')}")
                    print(f"Ссылкв: {task.get('links')[0] if task.get('links') else '—'}")
                    print(f"Название: {task.get('name')}")
                except Exception as e:
                    print(f"⚠️ Ошибка при выводе задачи #{i}: {e}")

        return tasks or []

    except Exception as e:
        print(f"❌ Flyer get_tasks error: {e}")
        # Якщо проблема з авторизацією — вимикаємо Flyer
        if "auth" in str(e).lower() or "key" in str(e).lower() or "401" in str(e):
            core.flyer_enabled = False
            print("❌ Flyer отключен из-за ошибки авторизации")
        return []

# Flyer проверка задания
async def flyer_check_task(signature, user_id=None):
    """Проверяет подпись задания в поддерживаемых версиях flyerapi.

    flyerapi 1.3.x: check_task(signature)
    flyerapi 1.2.x: check_task(user_id=..., signature=...)
    """
    if not core.flyer_enabled or not core.flyer:
        return None

    method = core.flyer.check_task
    try:
        # В актуальном flyerapi (1.3.x) user_id в check_task больше не нужен.
        status = await method(str(signature))
    except TypeError as exc:
        error_text = str(exc).lower()
        # Совместимость со старым API 1.2.x, который требовал оба аргумента.
        old_api_signature_error = (
            "missing" in error_text and "signature" in error_text
        )
        if old_api_signature_error and user_id is not None:
            try:
                status = await method(user_id=int(user_id), signature=str(signature))
            except Exception as retry_exc:
                print(f"❌ Flyer check_task error for {signature}: {retry_exc}")
                return None
        else:
            print(f"❌ Flyer check_task error for {signature}: {exc}")
            return None
    except Exception as exc:
        print(f"❌ Flyer check_task error for {signature}: {exc}")
        return None

    if isinstance(status, dict):
        status = status.get("status")
    print(f"🔍 Flyer check_task result for signature {signature}: {status}")
    return status

def check_flyer_methods():
    if core.flyer:
        methods = [method for method in dir(core.flyer) if not method.startswith('_')]
        print(f"🔧 Доступные методы Flyer: {methods}")
    else:
        print("❌ Flyer не инициализирован")


# ===== services_gramads.py =====
# -*- coding: utf-8 -*-
"""Интеграция Gramads: показ рекламы, история показов."""



async def load_gramads_token():
    """Загрузка токена Gramads из БД"""

    token_from_db = await get_setting("gramads_token")

    if token_from_db and token_from_db.startswith("eyJ"):
        core.GRAMADS_TOKEN = token_from_db
        core.gramads_enabled = True
        print(f"✅ Загружен токен Gramads из БД")
        return True
    else:
        core.gramads_enabled = False
        print("⚠️ Токен Gramads не найден в БД")
        return False


async def load_tgrass_key():
    """Загрузка ключа TGRASS из БД при старте бота."""
    key = await get_setting("tgrass_api_key")
    if key:
        print(f"✅ Ключ TGRASS загружен из БД")
        return True
    else:
        print("⚠️ Ключ TGRASS не найден в БД (спонсорские подписки будут отключены)")
        return False

async def show_gramads_ad(user_id: int):
    """Показать рекламу через Gramads API"""

    if not core.gramads_enabled or not core.GRAMADS_TOKEN:
        print(f"⚠️ Gramads отключен для пользователя {user_id}")
        return False

    # Проверяем кэш последних показов
    current_time = time.time()
    if user_id in last_ad_times:
        time_diff = current_time - last_ad_times[user_id]
        if time_diff < AD_COOLDOWN:
            print(f"⏰ Пользователю {user_id} недавно показывали рекламу ({int(time_diff)} сек назад)")
            return False

    try:
        headers = {
            'Authorization': f'Bearer {core.GRAMADS_TOKEN}',
            'Content-Type': 'application/json',
        }

        print(f"📺 Показываем рекламу пользователю {user_id}")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                GRAMADS_API_URL,
                headers=headers,
                json={'SendToChatId': user_id},
                timeout=10
            ) as response:

                # Получаем текст ответа в любом формате
                response_text = await response.text()

                # Проверяем статус код
                if response.status == 200:
                    print(f"✅ Реклама успешно показана пользователю {user_id}")
                    print(f"📊 Ответ API: {response_text}")

                    # Пытаемся разобрать JSON, если это возможно
                    try:
                        data = json.loads(response_text)
                        print(f"📦 JSON данные: {data}")
                    except json.JSONDecodeError:
                        # Если ответ не JSON, но статус 200 - всё равно считаем успехом
                        print(f"⚠️ Ответ не в JSON формате, но статус 200: {response_text}")

                    # Обновляем время последнего показа
                    last_ad_times[user_id] = current_time

                    # Сохраняем в БД для persistence
                    await set_setting(f"last_ad_{user_id}", str(current_time))

                    return True
                else:
                    print(f"❌ Ошибка Gramads API: {response.status} - {response_text}")

                    # Если ошибка авторизации - отключаем Gramads
                    if response.status == 401:
                        core.gramads_enabled = False
                        print("🔴 Gramads отключен из-за ошибки авторизации")

                    return False

    except asyncio.TimeoutError:
        print(f"⚠️ Timeout при показе рекламы пользователю {user_id}")
        return False
    except Exception as e:
        print(f"❌ Ошибка при показе рекламы: {e}")
        return False

async def show_ad_at_opportunity(user_id: int, context: str = "general"):
    """Показать рекламу при удобном случае"""
    try:
        # Пропускаем админов
        if user_id in ADMIN_IDS:
            return False

        # Пропускаем ботов
        if len(str(user_id)) <= 6:
            return False

        # Проверяем настройки пользователя (можно добавить позже)
        # user = await get_user(user_id)
        # if user.get('disable_ads', False):
        #     return False

        # Определяем вероятность показа в зависимости от контекста
        probabilities = {
            "after_task": 0.8,    # 80% после задания
            "after_withdrawal": 0.6, # 60% после вывода
            "after_topup": 0.7,   # 70% после пополнения
            "general": 0.3        # 30% в общих случаях
        }

        prob = probabilities.get(context, 0.3)

        # Случайное решение показывать или нет
        if random.random() > prob:
            print(f"🎲 Пропускаем рекламу для {user_id} (вероятность {prob})")
            return False

        # Показываем рекламу
        return await show_gramads_ad(user_id)

    except Exception as e:
        print(f"❌ Ошибка в show_ad_at_opportunity: {e}")
        return False

async def schedule_periodic_ads():
    """Периодический показ рекламы активным пользователям"""
    while True:
        try:
            if core.gramads_enabled:
                print("📢 Запуск периодического показа рекламы...")

                # Получаем активных пользователей (последние 7 дней)
                async with aiosqlite.connect('bot_database.db') as db:
                    async with db.execute('''
                        SELECT user_id FROM users WHERE is_blocked = FALSE LIMIT 200
                    ''') as cursor:
                        users = await cursor.fetchall()

                print(f"📊 Найдено {len(users)} пользователей для рекламы")

                for user_row in users:
                    user_id = user_row[0]
                    if user_id in ADMIN_IDS:
                        continue

                    # Показываем с меньшей вероятностью для периодических показов
                    if random.random() < 0.2:  # 20% вероятность
                        await show_ad_at_opportunity(user_id, "periodic")

                    # Небольшая задержка между пользователями
                    await asyncio.sleep(0.5)

            # Ждем 1 час до следующей проверки
            await asyncio.sleep(3600)

        except Exception as e:
            print(f"❌ Ошибка в schedule_periodic_ads: {e}")
            await asyncio.sleep(300)

async def load_ad_history_from_db():
    """Загрузка истории показов из БД при запуске"""
    try:
        async with aiosqlite.connect('bot_database.db') as db:
            async with db.execute("SELECT key, value FROM settings WHERE key LIKE 'last_ad_%'") as cursor:
                rows = await cursor.fetchall()

                for key, value in rows:
                    try:
                        user_id = int(key.replace('last_ad_', ''))
                        timestamp = float(value)
                        last_ad_times[user_id] = timestamp
                    except:
                        continue

        print(f"✅ Загружена история показов для {len(rows)} пользователей")
    except Exception as e:
        print(f"⚠️ Ошибка загрузки истории показов: {e}")


# ===== services_piarflow.py =====
# -*- coding: utf-8 -*-
"""Интеграция PiarFlow: задания-спонсоры, проверка, снятие награды при отписке (без штрафа)."""


PIARFLOW_API_URL = "https://piarflow.com/v1"
_session = None
_webhook_runner = None
_piarflow_last_error = None
_PIARFLOW_RETRY_STATUSES = {429, 500, 502, 503, 504}
_PIARFLOW_RETRY_DELAYS = (0.5, 1.5, 3.0)


def piarflow_last_error():
    """Возвращает последнюю ошибку запроса, если сервис был недоступен."""
    return _piarflow_last_error


async def close_external_sessions():
    """Закрывает внешние aiohttp-сессии и вебхук-сервер при остановке бота."""
    global _session, _webhook_runner
    if _session is not None and not _session.closed:
        await _session.close()
        _session = None
    if _webhook_runner is not None:
        await _webhook_runner.cleanup()
        _webhook_runner = None


def _ensure_piarflow_storage():
    if not hasattr(core, "piarflow_pending"):
        core.piarflow_pending = {}
    if not hasattr(core, "piarflow_completed"):
        core.piarflow_completed = {}


async def _ensure_piarflow_db():
    """Таблица связи задание -> ссылка. Живёт в БД, переживает перезапуск."""
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS piarflow_links (
                user_id INTEGER NOT NULL,
                signature TEXT NOT NULL,
                link TEXT NOT NULL,
                PRIMARY KEY (user_id, signature)
            )""")
        await db.commit()


async def _save_link(user_id: int, signature: str, link: str):
    await _ensure_piarflow_db()
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute(
            "INSERT OR REPLACE INTO piarflow_links (user_id, signature, link) VALUES (?, ?, ?)",
            (user_id, signature, link))
        await db.commit()


async def _get_link(user_id: int, signature: str):
    await _ensure_piarflow_db()
    async with aiosqlite.connect('bot_database.db') as db:
        async with db.execute(
            "SELECT link FROM piarflow_links WHERE user_id = ? AND signature = ?",
            (user_id, signature)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def _clear_stale_piarflow_pending(user_id: int):
    """Удаляет старые неоплаченные ссылки перед выдачей нового меню заданий.

    Оставляем только ссылки, по которым уже создана заморозка: они нужны
    watcher'у для контроля отписок. Так «Проверить» оплачивает только те
    задания, которые показаны пользователю в текущем меню.
    """
    await _ensure_piarflow_db()
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute("""
            SELECT pl.signature
            FROM piarflow_links pl
            WHERE pl.user_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM frozen_transactions ft
                  WHERE ft.user_id = pl.user_id
                    AND ft.task_type = 'piarflow'
                    AND CAST(ft.task_id AS TEXT) = pl.signature
              )
        """, (int(user_id),)) as cursor:
            stale = [str(row[0]) for row in await cursor.fetchall()]
        if stale:
            await db.executemany(
                "DELETE FROM piarflow_links WHERE user_id = ? AND signature = ?",
                [(int(user_id), sig) for sig in stale],
            )
            await db.commit()

    pending = core.piarflow_pending.get(int(user_id), {})
    for sig in stale:
        pending.pop(sig, None)
    if stale:
        print(f"🧹 PiarFlow: удалено {len(stale)} устаревших неоплаченных ссылок для {user_id}")
    return len(stale)


async def _load_piarflow_pending(user_id: int):
    """Загружает выданные ссылки из SQLite, чтобы проверка переживала рестарт."""
    await _ensure_piarflow_db()
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute("""
            SELECT pl.signature, pl.link
            FROM piarflow_links pl
            WHERE pl.user_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM frozen_transactions ft
                  WHERE ft.user_id = pl.user_id
                    AND ft.task_type = 'piarflow'
                    AND CAST(ft.task_id AS TEXT) = pl.signature
                    AND ft.penalty_applied = 0
              )
              AND COALESCE((
                  SELECT ct.status FROM completed_tasks ct
                  WHERE ct.user_id = pl.user_id AND ct.signature = pl.signature
                  LIMIT 1
              ), '') NOT IN ('completed', 'done', 'complete', 'waiting')
        """, (int(user_id),)) as cursor:
            rows = await cursor.fetchall()
    pending = core.piarflow_pending.setdefault(int(user_id), {})
    for signature, link in rows:
        pending[str(signature)] = {"link": str(link), "price": piarflow_reward()}
    return pending


def _normalize_piarflow_link(link: str) -> str:
    """Нормализует URL для сопоставления ответа API с исходной ссылкой."""
    from urllib.parse import urlsplit, urlunsplit
    value = str(link or "").strip()
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.port:
            host = f"{host}:{parts.port}"
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))
    except Exception:
        return value.rstrip("/")


def _base_piarflow_link(link: str) -> str:
    """Ссылка без query-строки — для сопоставления канала независимо от ref-метки."""
    from urllib.parse import urlsplit, urlunsplit
    value = str(link or "").strip()
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), host, path, "", ""))
    except Exception:
        return value.rstrip("/")


async def _delete_link(user_id: int, signature: str):
    await _ensure_piarflow_db()
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute(
            "DELETE FROM piarflow_links WHERE user_id = ? AND signature = ?",
            (user_id, signature))
        await db.commit()


def signature_for_link(link: str) -> str:
    return "pf_" + hashlib.md5(link.encode()).hexdigest()[:16]


def piarflow_reward(price=None) -> float:
    """ФИКСИРОВАННАЯ ставка за задание. Меняется одной цифрой в core.py."""
    try:
        return round(float(getattr(core, "PIARFLOW_FIXED_REWARD", 0.006)), 3)
    except (TypeError, ValueError):
        return 0.006


async def load_piarflow_key():
    _ensure_piarflow_storage()
    key = (await get_setting("piarflow_api_key") or "").strip()
    if key:
        core.piarflow_api_key = key
        core.piarflow_enabled = True
        print("✅ PiarFlow: ключ загружен из БД")
        return True
    core.piarflow_enabled = False
    print("⚠️ PiarFlow: ключ не найден в БД")
    return False


async def _piarflow_request(path: str, payload: dict = None) -> dict:
    if not getattr(core, "piarflow_enabled", False) or not getattr(core, "piarflow_api_key", None):
        return {}
    global _session, _piarflow_last_error
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    headers = {
        "Authorization": f"Bearer {core.piarflow_api_key}",
        "Content-Type": "application/json",
    }
    method = "POST" if payload is not None else "GET"
    for attempt, delay in enumerate((*_PIARFLOW_RETRY_DELAYS, None)):
        try:
            async with _session.request(
                method,
                PIARFLOW_API_URL + path,
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status == 401:
                    _piarflow_last_error = "HTTP 401"
                    core.piarflow_enabled = False
                    print("🔴 PiarFlow отключен: неверный API ключ")
                    return {}
                if resp.status in _PIARFLOW_RETRY_STATUSES and delay is not None:
                    _piarflow_last_error = f"HTTP {resp.status}"
                    print(
                        f"⚠️ PiarFlow HTTP {resp.status} на {path}; "
                        f"повтор {attempt + 1} через {delay}с"
                    )
                    await asyncio.sleep(delay)
                    continue
                if resp.status != 200:
                    _piarflow_last_error = f"HTTP {resp.status}"
                    print(f"⚠️ PiarFlow HTTP {resp.status} на {path}")
                    return {}
                _piarflow_last_error = None
                return await resp.json(content_type=None)
        except Exception as e:
            if delay is not None:
                _piarflow_last_error = str(e)
                print(f"⚠️ PiarFlow error ({path}): {e}; повтор {attempt + 1} через {delay}с")
                await asyncio.sleep(delay)
                continue
            _piarflow_last_error = str(e)
            print(f"⚠️ PiarFlow error ({path}): {e}")
            return {}
    return {}


async def get_piarflow_tasks(user_id: int, chat_id: int, max_sponsors: int = 5):
    if not getattr(core, "piarflow_enabled", False):
        return []
    _ensure_piarflow_storage()
    data = await _piarflow_request("/sponsors", {
        "user_id": int(user_id),
        "chat_id": int(chat_id),
        "max_sponsors": int(max_sponsors),
    })
    # Если PiarFlow временно недоступен, не трогаем старые pending-ссылки
    # и не выдаём «пустое меню» как успешный ответ.
    if piarflow_last_error():
        print(f"⚠️ PiarFlow: задания не выданы, последняя ошибка {piarflow_last_error()}")
        return []
    # Очищаем неоплаченные ссылки прошлых открытий меню только после
    # успешного ответа API — так проверка оплачивает только текущие задания.
    await _clear_stale_piarflow_pending(user_id)
    sponsors = data.get("sponsors") or []
    tasks = []
    for s in sponsors:
        link = s.get("link")
        status = str(s.get("status") or "").strip().lower()
        # Выдаём только реально доступные подписки. Статусы subscribed /
        # not_counted / unavailable означают, что задание уже не может быть
        # выполнено заново — иначе оно сразу попадёт в pending и будет
        # ошибочно оплачено при первом же нажатии "Проверить".
        if not link or (status and status not in ("unsubscribed", "not_subscribed")):
            continue
        sig = signature_for_link(link)
        reward = piarflow_reward()
        tasks.append({
            "type": "piarflow",
            "name": "Подписка на спонсора",
            "description": "Подпишитесь и вернитесь для проверки",
            "url": link,
            "button_text": "💫 Подписаться",
            "price": reward,
            "signature": sig,
        })
        core.piarflow_pending.setdefault(user_id, {})[sig] = {"link": link, "price": reward}
        await _save_link(user_id, sig, link)
    print(f"🔍 PiarFlow: выдано {len(tasks)} заданий пользователю {user_id}, в ожидании: {len(core.piarflow_pending.get(user_id, {}))}")
    return tasks


# ==================== ПРОВЕРКА ЗАДАНИЙ ====================

async def piarflow_check_and_complete(user_id: int):
    """Проверяет выданные PiarFlow задания и атомарно начисляет награду за подтверждённую подписку."""
    _ensure_piarflow_storage()
    pending = await _load_piarflow_pending(user_id)
    if not pending:
        print(f"ℹ️ PiarFlow check: у {user_id} нет ожидающих заданий в SQLite")
        return 0, 0.0

    # Не отправляем повторно уже оплаченные задания, но сохраняем их ссылки в БД
    # для watcher, который отслеживает отписки до окончания 24-часовой заморозки.
    # Индексируем ссылками тремя способами, т.к. PiarFlow API может вернуть ссылки
    # в ином формате (без query-параметров ref, с другим регистром, без scheme и т.п.).
    check_items = {}          # нормализованная ссылка -> (signature, original_link)
    check_items_by_base = {}  # ссылка без query-строки -> (signature, original_link)
    check_items_by_orig = {}  # исходная ссылка как есть -> (signature, original_link)
    for signature, value in list(pending.items()):
        link = value.get("link") if isinstance(value, dict) else value
        if not link:
            continue
        norm = _normalize_piarflow_link(link)
        base = _base_piarflow_link(link)
        check_items[norm] = (str(signature), str(link))
        check_items_by_base[base] = (str(signature), str(link))
        check_items_by_orig[str(link)] = (str(signature), str(link))

    if not check_items:
        return 0, 0.0

    result = await _piarflow_request("/sponsors/check", {
        "user_id": int(user_id),
        "links": [item[1] for item in check_items.values()],
    })
    print(f"🔍 PiarFlow check запрос: {len(check_items)} ссылок")
    print(f"🔍 PiarFlow check ответ: {result}")

    if not isinstance(result, dict) or result.get("status") != "ok":
        print("⚠️ PiarFlow check: API не подтвердил проверку — начислений нет")
        return 0, 0.0

    done = 0
    total_reward = 0.0
    # ВАЖНО: not_counted у PiarFlow означает "ссылка не учтена", а НЕ "подписка
    # подтверждена". API отдаёт not_counted даже для ссылок, где пользователь
    # вообще не подписывался — из-за этого один клик начислял за все 5 заданий.
    # Оплачиваем только реально подтверждённую подписку.
    accepted_statuses = {"subscribed"}
    for item in result.get("sponsors") or []:
        if not isinstance(item, dict):
            continue
        response_link = str(item.get("link") or "")
        status = str(item.get("status") or "").strip().lower()

        # Сопоставляем по всем доступным ключам: нормализованная → базовая (без query) → исходная.
        matched = (
            check_items.get(_normalize_piarflow_link(response_link))
            or check_items_by_base.get(_base_piarflow_link(response_link))
            or check_items_by_orig.get(response_link)
        )
        if not matched:
            print(f"⚠️ PiarFlow: не удалось сопоставить ссылку из ответа: {response_link}")
            continue
        signature, original_link = matched

        if status not in accepted_statuses:
            print(f"   ↳ {original_link}: статус '{status}' — пока не начисляем")
            continue

        reward = piarflow_reward()
        credited = False
        try:
            # Все финансовые и статусные изменения одной транзакцией: повторный
            # callback/webhook не сможет начислить вторую награду.
            async with aiosqlite.connect("bot_database.db") as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute("""
                    SELECT 1 FROM frozen_transactions
                    WHERE user_id = ? AND task_type = 'piarflow'
                      AND CAST(task_id AS TEXT) = ? AND penalty_applied = 0
                    LIMIT 1
                """, (int(user_id), signature)) as cursor:
                    existing = await cursor.fetchone()
                if existing:
                    await db.rollback()
                    core.piarflow_pending.get(user_id, {}).pop(signature, None)
                    continue

                async with db.execute(
                    "SELECT 1 FROM users WHERE user_id = ?", (int(user_id),)
                ) as cursor:
                    user_exists = await cursor.fetchone()
                if not user_exists:
                    await db.rollback()
                    print(f"⚠️ PiarFlow: пользователь {user_id} отсутствует в users; начисление пропущено")
                    continue

                await db.execute("""
                    UPDATE users
                    SET frozen_balance = COALESCE(frozen_balance, 0) + ?,
                        completed_tasks = COALESCE(completed_tasks, 0) + 1,
                        lifetime_earned = COALESCE(lifetime_earned, 0) + ?
                    WHERE user_id = ?
                """, (reward, reward, int(user_id)))
                await db.execute("""
                    INSERT INTO frozen_transactions
                        (user_id, amount, task_type, task_id, unfreeze_at, penalty_applied, penalty_amount)
                    VALUES (?, ?, 'piarflow', ?, datetime('now', '+24 hours'), 0, 0)
                """, (int(user_id), reward, signature))
                await db.execute("""
                    INSERT OR REPLACE INTO completed_tasks (user_id, signature, status)
                    VALUES (?, ?, 'completed')
                """, (int(user_id), signature))
                await db.commit()
                credited = True
        except Exception as exc:
            print(f"❌ PiarFlow: ошибка атомарного начисления {user_id}/{signature}: {exc}")
            continue

        if credited:
            done += 1
            total_reward += reward
            core.piarflow_pending.get(user_id, {}).pop(signature, None)
            core.piarflow_completed.setdefault(user_id, {})[signature] = {
                "link": original_link, "ts": time.time()
            }
            print(f"   ✅ Подписка подтверждена: {original_link}; заморожено {reward:.3f}$ на 24 часа")

    if done:
        print(f"✅ PiarFlow: {done} заданий выполнено пользователем {user_id} ({total_reward:.3f}$)")
        # Задания №3 увеличивают completed_tasks напрямую в SQL, поэтому
        # стандартный вызов из update_user_balance() не срабатывает.
        try:
            await check_and_pay_referral_bonus(user_id)
        except Exception as exc:
            print(f"⚠️ PiarFlow: ошибка проверки реферальных бонусов: {exc}")
    else:
        print(f"🔎 PiarFlow check result: user_id={user_id}, completed=0, total_reward=0.000000")
    return done, total_reward


# ==================== ОТПИСКИ (снимаем награду, без штрафа) ====================

async def _find_piarflow_tx(user_id: int, signature: str):
    """Ищет активную заморозку только по конкретному заданию PiarFlow."""
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute("""
            SELECT id, user_id, amount, task_type, task_id,
                   frozen_at, unfreeze_at, is_unfrozen, penalty_applied
            FROM frozen_transactions
            WHERE user_id = ?
              AND task_type = 'piarflow'
              AND CAST(task_id AS TEXT) = ?
              AND penalty_applied = 0
              AND is_unfrozen = 0
            ORDER BY id DESC LIMIT 1
        """, (int(user_id), str(signature))) as cursor:
            return await cursor.fetchone()


async def apply_piarflow_unsubscribe_penalty(user_id: int, signature: str, link: str = ""):
    """Списывает награду только пока соответствующая транзакция ещё заморожена."""
    try:
        async with aiosqlite.connect("bot_database.db") as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("""
                SELECT id, amount FROM frozen_transactions
                WHERE user_id = ? AND task_type = 'piarflow'
                  AND CAST(task_id AS TEXT) = ?
                  AND penalty_applied = 0 AND is_unfrozen = 0
                ORDER BY id DESC LIMIT 1
            """, (int(user_id), str(signature))) as cursor:
                tx = await cursor.fetchone()
            if not tx:
                await db.rollback()
                print(f"ℹ️ PiarFlow: выплата {user_id}/{signature} уже разморожена или отсутствует; списания нет")
                return False

            tx_id, raw_amount = tx
            amount = max(0.0, float(raw_amount or 0.0))
            if amount <= 0:
                await db.rollback()
                return False

            async with db.execute(
                "SELECT frozen_balance FROM users WHERE user_id = ?", (int(user_id),)
            ) as cursor:
                user = await cursor.fetchone()
            frozen_balance = max(0.0, float(user[0] or 0.0)) if user else 0.0
            if not user or frozen_balance + 1e-9 < amount:
                await db.rollback()
                print(f"⚠️ PiarFlow: недостаточно замороженных средств {user_id}/{signature}; доступный баланс не трогаем")
                return False

            cur = await db.execute("""
                UPDATE frozen_transactions
                SET penalty_applied = 1, penalty_amount = ?, is_unfrozen = 1
                WHERE id = ? AND penalty_applied = 0 AND is_unfrozen = 0
            """, (amount, int(tx_id)))
            if cur.rowcount != 1:
                await db.rollback()
                return False
            # Списываем сначала из заморозки, остаток — с основного баланса
            async with db.execute(
                'SELECT frozen_balance FROM users WHERE user_id = ?',
                (int(user_id),)
            ) as cur:
                frow = await cur.fetchone()
            frozen_now = max(0.0, float(frow[0] or 0.0)) if frow else 0.0

            if frozen_now + 1e-9 >= amount:
                await db.execute("""
                    UPDATE users SET frozen_balance = frozen_balance - ?
                    WHERE user_id = ? AND frozen_balance >= ?
                """, (amount, int(user_id), amount))
            else:
                from_frozen = min(frozen_now, amount)
                from_balance = max(0.0, amount - from_frozen)
                await db.execute("""
                    UPDATE users
                    SET frozen_balance = MAX(0, frozen_balance - ?),
                        balance = MAX(0, balance - ?)
                    WHERE user_id = ?
                """, (from_frozen, from_balance, int(user_id)))

            # Откатываем статистику выполненных заданий и заработка
            await db.execute("""
                UPDATE users SET completed_tasks = MAX(0, completed_tasks - 1)
                WHERE user_id = ?
            """, (int(user_id),))
            await db.execute("""
                UPDATE users SET lifetime_earned = MAX(0, lifetime_earned - ?)
                WHERE user_id = ?
            """, (amount, int(user_id)))
            await db.execute("""
                UPDATE completed_tasks SET status = 'unsubscribed'
                WHERE user_id = ? AND signature = ?
            """, (int(user_id), str(signature)))
            await db.commit()

        core.piarflow_completed.get(user_id, {}).pop(signature, None)
        core.piarflow_pending.get(user_id, {}).pop(signature, None)
        await _delete_link(user_id, signature)
        print(f"✅ PiarFlow: отписка {user_id}/{signature}; списано {amount:.6f}$ только из заморозки")
        return True
    except Exception as exc:
        print(f"❌ PiarFlow penalty error: {exc}")
        return False


async def piarflow_unsubscribe_watcher(interval: int = 300):
    """Каждые 5 минут берёт активные PiarFlow-заморозки из БД и сверяет подписки с API."""
    await _ensure_piarflow_db()
    await asyncio.sleep(60)
    while True:
        try:
            async with aiosqlite.connect('bot_database.db') as db:
                async with db.execute("""
                    SELECT user_id, task_id
                    FROM frozen_transactions
                    WHERE task_type = 'piarflow'
                      AND penalty_applied = 0 AND is_unfrozen = 0
                """) as cursor:
                    rows = await cursor.fetchall()

            by_user = {}
            for uid, sig in rows:
                by_user.setdefault(uid, []).append(sig)

            for uid, sigs in by_user.items():
                links, sig_link = [], {}
                for sig in sigs:
                    link = await _get_link(uid, sig)
                    if link:
                        links.append(link)
                        sig_link[_normalize_piarflow_link(link)] = sig
                if not links:
                    continue
                result = await _piarflow_request("/sponsors/check",
                                                 {"user_id": uid, "links": links})
                for item in (result.get("sponsors") or []) if isinstance(result, dict) else []:
                    link = item.get("link")
                    normalized_link = _normalize_piarflow_link(link)
                    if item.get("status") == "unsubscribed" and normalized_link in sig_link:
                        await apply_piarflow_unsubscribe_penalty(uid, sig_link[normalized_link], link)
        except Exception as e:
            print(f"❌ PiarFlow watcher error: {e}")
        await asyncio.sleep(interval)


async def _check_sponsors(user_id, links):
    """HTTP 404 на /sponsors/check означает 'активных заданий нет' — это норма."""
    result = await _piarflow_request("/sponsors/check",
                                     {"user_id": user_id, "links": links})
    if not result or not isinstance(result, dict):
        print("PiarFlow check: активных заданий по этим ссылкам нет — ок")
        return {"status": "ok", "sponsors": []}
    return result


# ===== services_pulsar.py =====
# -*- coding: utf-8 -*-
"""Интеграция Pulsar Ads (Задания №4): список спонсоров, пооплатная заморозка и контроль отписок."""


PULSAR_API_URL = "https://pulsarx.sale/api/v1"
# Задания №4 работают как обычные: список спонсоров берётся из /op/sponsors,
# подписки проверяются через /op/tasks/check. Mini App не используется.
PULSAR_DEFAULT_MINI_APP_URL = "https://pulsarx.sale/bot-tasks/index.html?platform_id=991"
_pulsar_last_error = None
_pulsar_empty_reason = ""
_PULSAR_RETRY_DELAYS = (0.5, 1.5, 3.0)


def pulsar_last_error():
    """Последняя ошибка запроса к Pulsar API."""
    return _pulsar_last_error


def pulsar_empty_reason():
    """Причина пустого списка спонсоров от Pulsar."""
    return _pulsar_empty_reason


def pulsar_reward() -> float:
    """Фиксированная оплата за одну подписку."""
    try:
        return round(float(getattr(core, "PULSAR_FIXED_REWARD", 0.006)), 3)
    except (TypeError, ValueError):
        return 0.006


def _pulsar_base_url() -> str:
    return (getattr(core, "pulsar_base_url", "") or PULSAR_API_URL).rstrip("/")


def _pulsar_mini_app_url() -> str:
    return getattr(core, "pulsar_mini_app_url", "") or PULSAR_DEFAULT_MINI_APP_URL


async def load_pulsar_config():
    core.pulsar_platform_id = (await get_setting("pulsar_platform_id") or "").strip()
    core.pulsar_api_key = (await get_setting("pulsar_api_key") or "").strip()
    base = (await get_setting("pulsar_base_url") or "").strip()
    mini = (await get_setting("pulsar_mini_app_url") or "").strip()
    core.pulsar_base_url = base or PULSAR_API_URL
    if not mini or "t.me/" in mini:
        core.pulsar_mini_app_url = PULSAR_DEFAULT_MINI_APP_URL
        await set_setting("pulsar_mini_app_url", core.pulsar_mini_app_url)
    else:
        core.pulsar_mini_app_url = mini
    core.pulsar_enabled = bool(core.pulsar_platform_id and core.pulsar_api_key)
    if core.pulsar_enabled:
        print("✅ Pulsar Ads: ключ и площадка загружены из БД")
    else:
        print("⚠️ Pulsar Ads: ключ или platform_id не найдены в БД")
    return core.pulsar_enabled


async def pulsar_request(method: str, path: str, params: dict = None, payload: dict = None) -> dict:
    """Запрос к Pulsar API. GET — X-Api-Key, POST — api_key в теле."""
    global _session, _pulsar_last_error
    if not getattr(core, "pulsar_enabled", False):
        return {}
    platform = getattr(core, "pulsar_platform_id", "")
    key = getattr(core, "pulsar_api_key", "")
    if not platform or not key:
        return {}
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    headers = {"X-Api-Key": key, "Content-Type": "application/json"}
    body = None
    if payload is not None:
        body = dict(payload)
        body.setdefault("platform_id", platform)
        body.setdefault("api_key", key)
    url = _pulsar_base_url() + path
    for attempt, delay in enumerate((*_PULSAR_RETRY_DELAYS, None)):
        try:
            async with _session.request(
                method, url, params=params, json=body, headers=headers
            ) as resp:
                if resp.status == 200:
                    _pulsar_last_error = None
                    return await resp.json(content_type=None)
                _pulsar_last_error = f"HTTP {resp.status}"
                if resp.status in _PIARFLOW_RETRY_STATUSES and delay is not None:
                    print(
                        f"⚠️ Pulsar HTTP {resp.status} на {path}; "
                        f"повтор {attempt + 1} через {delay}с"
                    )
                    await asyncio.sleep(delay)
                    continue
                print(f"⚠️ Pulsar HTTP {resp.status} на {path}")
                return {}
        except Exception as exc:
            if delay is not None:
                _pulsar_last_error = str(exc)
                print(f"⚠️ Pulsar error ({path}): {exc}; повтор {attempt + 1} через {delay}с")
                await asyncio.sleep(delay)
                continue
            _pulsar_last_error = str(exc)
            print(f"⚠️ Pulsar error ({path}): {exc}")
            return {}
    return {}


async def _ensure_pulsar_db():
    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pulsar_links (
                user_id INTEGER NOT NULL,
                signature TEXT NOT NULL,
                link TEXT NOT NULL,
                sponsor_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, signature)
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pulsar_users (
                user_id INTEGER PRIMARY KEY,
                language_code TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.commit()


async def _save_pulsar_language(user_id: int, language_code: str = ""):
    await _ensure_pulsar_db()
    code = (str(language_code or "")[:16] or "ru").lower()
    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute("""
            INSERT INTO pulsar_users (user_id, language_code, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                language_code = excluded.language_code,
                updated_at = CURRENT_TIMESTAMP
        """, (int(user_id), code))
        await db.commit()


async def _pulsar_language(user_id: int) -> str:
    await _ensure_pulsar_db()
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute(
            "SELECT language_code FROM pulsar_users WHERE user_id = ?", (int(user_id),)
        ) as cursor:
            row = await cursor.fetchone()
    return str(row[0] or "ru") if row else "ru"


async def _save_pulsar_link(user_id: int, signature: str, link: str, sponsor_id: str = ""):
    await _ensure_pulsar_db()
    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute("""
            INSERT OR REPLACE INTO pulsar_links (user_id, signature, link, sponsor_id)
            VALUES (?, ?, ?, ?)
        """, (int(user_id), str(signature), str(link), str(sponsor_id or "")))
        await db.commit()


async def _get_pulsar_link(user_id: int, signature: str):
    await _ensure_pulsar_db()
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute("""
            SELECT link FROM pulsar_links WHERE user_id = ? AND signature = ?
        """, (int(user_id), str(signature))) as cursor:
            row = await cursor.fetchone()
    return str(row[0]) if row else None


def _pulsar_sponsor_identity(sponsor) -> str:
    if isinstance(sponsor, dict):
        for key in ("link", "url", "channel_link", "channel", "username", "id", "sponsor_id"):
            value = sponsor.get(key)
            if value:
                return str(value)
        return json.dumps(sponsor, ensure_ascii=False, sort_keys=True)
    return str(sponsor or "")


def pulsar_sponsor_link(sponsor) -> str:
    if isinstance(sponsor, dict):
        for key in ("link", "url", "channel_link", "channel", "username"):
            value = sponsor.get(key)
            if value:
                return str(value)
    return _pulsar_sponsor_identity(sponsor)


def pulsar_signature(sponsor) -> str:
    return "pls_" + hashlib.md5(_pulsar_sponsor_identity(sponsor).encode()).hexdigest()[:16]


async def pulsar_get_sponsors(user_id: int, language_code: str = "ru") -> list:
    """Только неподписанные спонсоры для показа в меню (режим op)."""
    global _pulsar_empty_reason
    if not getattr(core, "pulsar_enabled", False):
        return []
    await _save_pulsar_language(user_id, language_code)
    data = await pulsar_request("GET", "/op/sponsors", params={
        "platform_id": getattr(core, "pulsar_platform_id", ""),
        "telegram_user_id": int(user_id),
        "language_code": (str(language_code or "ru")[:16] or "ru").lower(),
    })
    if pulsar_last_error():
        _pulsar_empty_reason = ""
        return []
    _pulsar_empty_reason = str(
        data.get("empty_reason_text")
        or data.get("listing_message")
        or data.get("empty_reason")
        or ""
    )
    sponsors = data.get("sponsors") or []
    result = []
    for sponsor in sponsors:
        if not isinstance(sponsor, dict) and not sponsor:
            continue
        status = str(sponsor.get("status") or "").strip().lower() if isinstance(sponsor, dict) else ""
        if status in ("subscribed", "completed", "done"):
            continue
        result.append(sponsor)
    return result


async def pulsar_check_and_complete(user_id: int, language_code: str = "ru"):
    """Проверяет мини-апп и начисляет по 0.006$ за каждого подписанного спонсора."""
    if not getattr(core, "pulsar_enabled", False):
        return 0, 0.0
    await _ensure_pulsar_db()
    await _save_pulsar_language(user_id, language_code)
    code = (str(language_code or "ru")[:16] or "ru").lower()
    data = await pulsar_request("POST", "/op/tasks/check", payload={
        "telegram_user_id": int(user_id),
        "language_code": code,
        "task_id": "subscribe",
    })
    if pulsar_last_error():
        return 0, 0.0

    subscribed = data.get("subscribed_sponsors") or []
    if not subscribed:
        saved = await pulsar_request("GET", "/op/tasks/status", params={
            "platform_id": getattr(core, "pulsar_platform_id", ""),
            "telegram_user_id": int(user_id),
        })
        subscribed = saved.get("subscribed_sponsors") or [] if not pulsar_last_error() else []

    done = 0
    total_reward = 0.0
    reward = pulsar_reward()
    for sponsor in subscribed:
        signature = pulsar_signature(sponsor)
        link = pulsar_sponsor_link(sponsor)
        sponsor_id = str(sponsor.get("id") or sponsor.get("sponsor_id") or "") if isinstance(sponsor, dict) else ""
        credited = False
        try:
            async with aiosqlite.connect("bot_database.db") as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute("""
                    SELECT 1 FROM frozen_transactions
                    WHERE user_id = ? AND task_type = 'pulsar'
                      AND CAST(task_id AS TEXT) = ? AND penalty_applied = 0
                    LIMIT 1
                """, (int(user_id), signature)) as cursor:
                    if await cursor.fetchone():
                        await db.rollback()
                        continue
                async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (int(user_id),)) as cursor:
                    if not await cursor.fetchone():
                        await db.rollback()
                        print(f"⚠️ Pulsar: пользователь {user_id} отсутствует в users; начисление пропущено")
                        continue
                await db.execute("""
                    UPDATE users
                    SET frozen_balance = COALESCE(frozen_balance, 0) + ?,
                        completed_tasks = COALESCE(completed_tasks, 0) + 1,
                        lifetime_earned = COALESCE(lifetime_earned, 0) + ?
                    WHERE user_id = ?
                """, (reward, reward, int(user_id)))
                await db.execute("""
                    INSERT INTO frozen_transactions
                        (user_id, amount, task_type, task_id, unfreeze_at, penalty_applied, penalty_amount)
                    VALUES (?, ?, 'pulsar', ?, datetime('now', '+24 hours'), 0, 0)
                """, (int(user_id), reward, signature))
                await db.execute("""
                    INSERT OR REPLACE INTO completed_tasks (user_id, signature, status)
                    VALUES (?, ?, 'completed')
                """, (int(user_id), signature))
                await db.commit()
                credited = True
        except Exception as exc:
            print(f"❌ Pulsar: ошибка начисления {user_id}/{signature}: {exc}")
            continue

        if credited:
            await _save_pulsar_link(user_id, signature, link, sponsor_id)
            done += 1
            total_reward += reward
            core.pulsar_pending.setdefault(int(user_id), {}).pop(signature, None)
            core.pulsar_completed.setdefault(int(user_id), {})[signature] = {"link": link, "ts": time.time()}
            print(f"   ✅ Pulsar: подписка подтверждена: {link}; заморожено {reward:.3f}$ на 24 часа")

    if done:
        print(f"✅ Pulsar: {done} заданий выполнено пользователем {user_id} ({total_reward:.3f}$)")
        # Как и в PiarFlow: счётчик заданий растёт напрямую в SQL,
        # поэтому реферальные бонусы надо проверять явно.
        try:
            await check_and_pay_referral_bonus(user_id)
        except Exception as exc:
            print(f"⚠️ Pulsar: ошибка проверки реферальных бонусов: {exc}")
    else:
        print(f"🔎 Pulsar check result: user_id={user_id}, completed=0, total_reward=0.000000")
    return done, total_reward


async def apply_pulsar_unsubscribe_penalty(user_id: int, signature: str, link: str = ""):
    """Списывает выплату только из ещё замороженных средств по конкретному спонсору."""
    try:
        async with aiosqlite.connect("bot_database.db") as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("""
                SELECT id, amount FROM frozen_transactions
                WHERE user_id = ? AND task_type = 'pulsar'
                  AND CAST(task_id AS TEXT) = ?
                  AND penalty_applied = 0 AND is_unfrozen = 0
                ORDER BY id DESC LIMIT 1
            """, (int(user_id), str(signature))) as cursor:
                tx = await cursor.fetchone()
            if not tx:
                await db.rollback()
                print(f"ℹ️ Pulsar: выплата {user_id}/{signature} уже разморожена или отсутствует; списания нет")
                return False
            tx_id, raw_amount = tx
            amount = max(0.0, float(raw_amount or 0.0))
            if amount <= 0:
                await db.rollback()
                return False
            async with db.execute("SELECT frozen_balance FROM users WHERE user_id = ?", (int(user_id),)) as cursor:
                user = await cursor.fetchone()
            frozen_balance = max(0.0, float(user[0] or 0.0)) if user else 0.0
            if not user or frozen_balance + 1e-9 < amount:
                await db.rollback()
                print(f"⚠️ Pulsar: недостаточно замороженных средств {user_id}/{signature}; основной баланс не трогаем")
                return False
            cur = await db.execute("""
                UPDATE frozen_transactions
                SET penalty_applied = 1, penalty_amount = ?, is_unfrozen = 1
                WHERE id = ? AND penalty_applied = 0 AND is_unfrozen = 0
            """, (amount, int(tx_id)))
            if cur.rowcount != 1:
                await db.rollback()
                return False
            await db.execute("""
                UPDATE users SET frozen_balance = frozen_balance - ? WHERE user_id = ?
            """, (amount, int(user_id)))
            await db.execute("""
                UPDATE users SET completed_tasks = MAX(0, completed_tasks - 1) WHERE user_id = ?
            """, (int(user_id),))
            await db.execute("""
                UPDATE users SET lifetime_earned = MAX(0, lifetime_earned - ?) WHERE user_id = ?
            """, (amount, int(user_id)))
            await db.execute("""
                UPDATE completed_tasks SET status = 'unsubscribed'
                WHERE user_id = ? AND signature = ?
            """, (int(user_id), str(signature)))
            await db.commit()
        core.pulsar_completed.get(int(user_id), {}).pop(signature, None)
        core.pulsar_pending.get(int(user_id), {}).pop(signature, None)
        await _delete_pulsar_link(user_id, signature)
        print(f"✅ Pulsar: отписка {user_id}/{signature}; списано {amount:.6f}$ только из заморозки")
        return True
    except Exception as exc:
        print(f"❌ Pulsar penalty error: {exc}")
        return False


async def _delete_pulsar_link(user_id: int, signature: str):
    await _ensure_pulsar_db()
    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute(
            "DELETE FROM pulsar_links WHERE user_id = ? AND signature = ?",
            (int(user_id), str(signature)),
        )
        await db.commit()


async def pulsar_unsubscribe_watcher(interval: int = 300):
    """Каждые 5 минут сверяем активные Pulsar-выплаты с текущими подписками."""
    await _ensure_pulsar_db()
    await asyncio.sleep(90)
    while True:
        try:
            async with aiosqlite.connect("bot_database.db") as db:
                async with db.execute("""
                    SELECT user_id, CAST(task_id AS TEXT)
                    FROM frozen_transactions
                    WHERE task_type = 'pulsar'
                      AND penalty_applied = 0 AND is_unfrozen = 0
                """) as cursor:
                    rows = await cursor.fetchall()
            by_user = {}
            for uid, sig in rows:
                by_user.setdefault(int(uid), []).append(str(sig))
            for uid, signatures in by_user.items():
                language = await _pulsar_language(uid)
                data = await pulsar_request("POST", "/op/tasks/check", payload={
                    "telegram_user_id": int(uid),
                    "language_code": language,
                    "task_id": "subscribe",
                })
                if pulsar_last_error() or not isinstance(data, dict):
                    continue
                if data.get("no_sponsors") or "subscribed_sponsors" not in data:
                    continue
                active_signatures = {
                    pulsar_signature(sponsor)
                    for sponsor in (data.get("subscribed_sponsors") or [])
                }
                for signature in signatures:
                    if signature in active_signatures:
                        continue
                    link = await _get_pulsar_link(uid, signature)
                    await apply_pulsar_unsubscribe_penalty(uid, signature, link or "")
        except Exception as exc:
            print(f"❌ Pulsar watcher error: {exc}")
        await asyncio.sleep(interval)


async def pulsar_record_start(user_id: int, language_code: str = "ru"):
    """Фиксируем /start для атрибуции Pulsar (если нет токена спонсор-бота)."""
    if not getattr(core, "pulsar_enabled", False):
        return False
    await _save_pulsar_language(user_id, language_code)
    data = await pulsar_request("POST", "/op/start", payload={
        "telegram_user_id": int(user_id),
        "language_code": (str(language_code or "ru")[:16] or "ru").lower(),
    })
    if pulsar_last_error():
        return False
    print(f"✅ Pulsar: /start зафиксирован для {user_id}")
    return bool(data.get("ok"))


# ===== services_tgrass.py =====
# -*- coding: utf-8 -*-
"""Интеграция TGRASS: офферы, middleware, проверки подписок."""



# --- TGRASS CONFIG ---
# --- TGRASS CONFIG ---
TGRASS_ENDPOINT = "https://tgrass.space/offers"

async def get_tgrass_offers(user_id: int, username: str = None, is_premium: bool = False):
    """Отримання офферів з TGRASS з оптимізацією"""
    api_key = await get_setting('tgrass_api_key')
    if not api_key: 
        return {'offers': []}

    payload = {
        "tg_user_id": int(user_id),
        "tg_login": str(username) if username else "",
        "lang": "ru",
        "is_premium": is_premium
    }
    headers = {"Content-Type": "application/json", "Auth": api_key}

    try:
        async with aiohttp.ClientSession() as session:
            # Зменшити таймаут до 2 секунд
            async with session.post(TGRASS_ENDPOINT, json=payload, headers=headers, timeout=2) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    return {'offers': []}
    except asyncio.TimeoutError:
        print(f"⚠️ TGRASS timeout for user {user_id}")
        return {'offers': []}
    except Exception as e:
        print(f"⚠️ TGRASS error for user {user_id}: {e}")
        return {'offers': []}


# Обработчик проверки подписок
@dp.callback_query(F.data == "check_tgrass_sub")
async def check_tgrass_subscription(call: CallbackQuery):
    """Проверяет подписки через TGRASS"""
    user = call.from_user

    offers_data = await get_tgrass_offers(
        user.id,
        user.username,
        user.is_premium or False
    )

    if offers_data and offers_data.get('status') == 'ok':
        # Все подписки выполнены
        await call.message.delete()
        await call.answer("✅ Отлично! Теперь все функции бота доступны.", show_alert=False)

        # Показываем главное меню без сброса состояния через cmd_start
        from utils import get_main_keyboard
        await bot.send_message(
            call.from_user.id,
            "🏠 Главное меню:",
            reply_markup=get_main_keyboard(call.from_user.id),
        )
    else:
        # Не все подписки выполнены
        unsubscribed = [o for o in offers_data.get('offers', []) if not o.get('subscribed')]

        if unsubscribed:
            kb = InlineKeyboardBuilder()
            for o in unsubscribed:
                kb.row(InlineKeyboardButton(
                    text=f"🌿 Подписаться", 
                    url=o['link']
                ))
            kb.row(InlineKeyboardButton(
                text="✅ Я подписался", 
                callback_data="check_tgrass_sub"
            ))

            await call.message.edit_text(
                "❌ Вы подписались не на все каналы!",
                reply_markup=kb.as_markup(),
                parse_mode="HTML"
            )
            await call.answer("Пожалуйста, подпишитесь на все каналы", show_alert=True)
        else:
            await call.answer("❌ Произошла ошибка проверки", show_alert=True)

# Добавьте обработчик для команды /tgrass для тестирования
@dp.message(Command("tgr"))
async def tgrass_test_command(message: Message):
    """Тестирование TGRASS API"""
    user = message.from_user

    offers_data = await get_tgrass_offers(
        user.id,
        user.username,
        user.is_premium or False
    )

    if offers_data:
        text = f"""
📊 <b>TGRASS Debug Info:</b>

🆔 User ID: {user.id}
👤 Username: @{user.username}
🔑 API Key: (скрыт)

📋 <b>Response:</b>
Status: {offers_data.get('status', 'N/A')}
Offers count: {len(offers_data.get('offers', []))}

📝 <b>Offers:</b>
"""
        for i, offer in enumerate(offers_data.get('offers', [])[:5], 1):
            text += f"{i}. {offer.get('name')} - Subscribed: {offer.get('subscribed')}\n"

        await message.answer(text, parse_mode="HTML")
    else:
        await message.answer("❌ Не удалось получить данные от TGRASS")



# АДМІНКА: Зміна ключа
@dp.callback_query(F.data == "admin_tgrass_settings")
async def admin_tgrass(call: CallbackQuery):
    key = await get_setting('tgrass_api_key') or ""
    masked_key = (key[:3] + '••••••' + key[-3:]) if len(key) > 8 else ('Установлен' if key else 'Не установлен')
    await call.message.edit_text(
        f"⚙️ <b>Настройки Tgrass OP</b>\n\nТекущий ключ: <code>{masked_key}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить ключ", callback_data="set_tgrass_key")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_main")]
        ]), parse_mode="HTML"
    )
    await call.answer()

@dp.callback_query(F.data == "set_tgrass_key")
async def set_tgrass_key_step1(call: CallbackQuery, state: FSMContext):
    await call.message.answer("Пришлите новый API Ключ Tgrass:")
    await state.set_state(TGRASSS.wait_tgrass_key)
    await call.answer()

@dp.message(TGRASSS.wait_tgrass_key)
async def save_tgrass_key(message: Message, state: FSMContext):
    await set_setting('tgrass_api_key', message.text.strip())
    await message.answer("✅ Ключ успешно сохранен в базу данных!")
    await state.clear()


# Обнови функцию admin_settings чтобы добавить кнопку Flyer
@dp.message(F.text == "⚙️ Настройки")
async def admin_settings(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    promo_price = float(await get_setting("promotion_price") or 0.015)
    current_value = await get_setting("user_task_reward") or "0.015"

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔑 Настройки Flyer", callback_data="flyer_settings")
    keyboard.button(text="📺 Настройки Gramads", callback_data="gramads_settings")  # НОВАЯ КНОПКА
    keyboard.button(text="✏️ Сменить канал", callback_data="change_payments_channel")
    keyboard.button(text="💬 Сменить чат", callback_data="change_chat")
    keyboard.button(text="💰 Мин. вывод", callback_data="change_min_withdrawal")
    keyboard.button(text="💵 Валюта", callback_data="change_currency")
    keyboard.button(text="📝 Управление заданиями", callback_data="manage_tasks")
    keyboard.button(text=f"💰 CryptoPay", callback_data="set_crypto_token")
    keyboard.button(text="💸 Настройки рефералов", callback_data="change_referral_settings")
    keyboard.button(text=f"✏️ Цена зад. {promo_price:.3f}", callback_data="edit_promotion_price")
    keyboard.button(text=f"✏️ Выпл.польз.зад. {current_value}", callback_data="edit_user_task_reward")
    keyboard.button(text="📡 Канал выплат", callback_data="set_payment_channel")
    keyboard.button(text="🛡 Требования вывода", callback_data="withdraw_requirements")
    keyboard.button(text="🌿 OP Tgrass", callback_data="admin_tgrass_settings")

    keyboard.adjust(2, 2, 2, 2, 2, 2, 1)

    await message.answer("⚙️ <b>Настройки бота:</b>", reply_markup=keyboard.as_markup(), parse_mode='HTML')


# ===== services_webhooks.py =====
# -*- coding: utf-8 -*-
"""HTTP-сервер приёма вебхуков (постбеков) от PiarFlow и Flyer."""



WEBHOOK_PORT = 8080


async def _read_json(request: web.Request):
    try:
        return await request.json()
    except Exception:
        try:
            return {"raw": await request.text()}
        except Exception:
            return {}


def _check_secret(request: web.Request) -> bool:
    secret = getattr(core, "WEBHOOK_SECRET", "")
    if not secret:
        return False  # без секрета вебхуки закрыты
    got = (request.headers.get("X-Secret-Token")
           or request.headers.get("X-Secret")
           or request.query.get("secret")
           or "")
    return got == secret


def _extract(data: dict):
    """Пытаемся вытащить пользователя, событие и ссылку из типовых полей."""
    if not isinstance(data, dict):
        return None, "", "", ""
    user_id = data.get("user_id") or data.get("userId") or (data.get("user") or {}).get("id")
    event = str(data.get("event") or data.get("type") or data.get("action") or "").lower()
    status = str(data.get("status") or "").lower()
    link = (data.get("link") or data.get("url") or data.get("channel")
            or (data.get("task") or {}).get("link") or "")
    return user_id, event, status, link


def _is_unsubscribe(event: str, status: str) -> bool:
    return ("unsub" in event or "leave" in event or "unfollow" in event
            or status == "unsubscribed" or status == "left")


async def _process(data: dict, service: str):
    user_id, event, status, link = _extract(data)
    if not user_id:
        print(f"⚠️ {service} webhook: нет user_id в payload — присылайте лог, поправлю разбор")
        return
    if not _is_unsubscribe(event, status):
        print(f"ℹ️ {service} webhook: событие '{event or status}' — не отписка, пропускаем")
        return
    try:
        if service == "PiarFlow":
            sig = signature_for_link(link) if link else ""
            await apply_piarflow_unsubscribe_penalty(int(user_id), sig, link)
        else:
            sig = str(data.get("signature") or data.get("task_id") or "")
            await _generic_penalty(int(user_id), sig, service)
    except Exception as e:
        print(f"❌ {service} webhook обработка: {e}")


async def _piarflow_webhook(request: web.Request):
    """Принимает официальный PiarFlow webhook об отписке.

    PiarFlow payload:
      tg_user_id, offer_link, status, chat_id, bot_id
    В тестовом запросе дополнительно приходит test=true.
    """
    payload = await _read_json(request)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    # PiarFlow отправляет тестовый webhook при настройке endpoint.
    if payload.get("test") is True or str(payload.get("test", "")).lower() in ("1", "true", "yes"):
        print("✅ PiarFlow webhook: тестовый запрос получен")
        return web.json_response({"ok": True})

    # Если секрет настроен, проверяем его. PiarFlow поддерживает URL с query-параметром;
    # также принимаем стандартные заголовки. Если секрет не задан, сверяем bot_id ниже.
    expected_secret = (
        os.getenv("WEBHOOK_SECRET", "").strip()
        or str(getattr(core, "WEBHOOK_SECRET", "") or "").strip()
    )
    if expected_secret:
        supplied_secret = (
            request.headers.get("X-Secret-Token")
            or request.headers.get("X-Secret")
            or request.query.get("secret")
            or ""
        )
        if not hmac.compare_digest(str(supplied_secret), expected_secret):
            print("⛔ PiarFlow webhook: неверный секрет")
            return web.json_response({"ok": False}, status=403)

    status = str(payload.get("status") or "").strip().lower()
    if status != "unsubscribed":
        print(f"ℹ️ PiarFlow webhook: статус {status!r}, отписки нет")
        return web.json_response({"ok": True})

    try:
        user_id = int(payload.get("tg_user_id"))
    except (TypeError, ValueError):
        print(f"⚠️ PiarFlow webhook: неверный tg_user_id: {payload}")
        return web.json_response({"ok": False, "error": "invalid_user_id"}, status=400)

    offer_link = str(payload.get("offer_link") or "").strip()
    if user_id <= 0 or not offer_link:
        print(f"⚠️ PiarFlow webhook: отсутствует tg_user_id/offer_link: {payload}")
        return web.json_response({"ok": False, "error": "missing_fields"}, status=400)

    # Если PiarFlow передал bot_id, проверяем, что событие адресовано именно этому боту.
    event_bot_id = payload.get("bot_id")
    if event_bot_id not in (None, ""):
        try:
            me = await bot.get_me()
            if int(event_bot_id) != int(me.id):
                print(
                    f"⛔ PiarFlow webhook: bot_id не совпадает "
                    f"(получен {event_bot_id}, наш {me.id})"
                )
                return web.json_response({"ok": False}, status=403)
        except Exception as exc:
            print(f"⚠️ PiarFlow webhook: не удалось проверить bot_id: {exc}")
            return web.json_response({"ok": False}, status=503)

    try:
        await _ensure_piarflow_db()
        offer_norm = _normalize_piarflow_link(offer_link)
        offer_base = _base_piarflow_link(offer_link)

        signature = None
        matched_link = None

        # Ищем сохранённую ссылку: сначала точное совпадение, затем нормализованное,
        # затем по базовой ссылке (без query-строки) — PiarFlow может прислать
        # offer_link в несколько другом формате, чем сохранён при выдаче задания.
        async with aiosqlite.connect("bot_database.db") as db:
            async with db.execute("""
                SELECT signature, link FROM piarflow_links WHERE user_id = ?
            """, (user_id,)) as cursor:
                rows = await cursor.fetchall()

        for saved_sig, saved_link in rows:
            saved_norm = _normalize_piarflow_link(saved_link)
            saved_base = _base_piarflow_link(saved_link)
            if saved_link == offer_link or saved_norm == offer_norm or saved_base == offer_base:
                signature = str(saved_sig)
                matched_link = saved_link
                break

        if not signature:
            # Ссылка не найдена в piarflow_links. Пробуем найти по активным заморозкам.
            async with aiosqlite.connect("bot_database.db") as db:
                async with db.execute("""
                    SELECT CAST(task_id AS TEXT)
                    FROM frozen_transactions
                    WHERE user_id = ? AND task_type = 'piarflow'
                      AND penalty_applied = 0 AND is_unfrozen = 0
                    ORDER BY id DESC
                """, (user_id,)) as cursor:
                    active = await cursor.fetchall()
            unique_signatures = list(dict.fromkeys(str(r[0]) for r in active if r[0]))
            if len(unique_signatures) == 1:
                signature = unique_signatures[0]
                # Подтянем оригинальную ссылку для лога
                matched_link = await _get_link(user_id, signature)
                print(f"ℹ️ PiarFlow webhook: ссылка не найдена в piarflow_links; однозначно сопоставлена активная выплата {signature}")
            else:
                print(
                    f"⚠️ PiarFlow webhook: ссылка не найдена и активных выплат для однозначного сопоставления: "
                    f"{user_id}: {offer_link[:250]}. Оставляем watcher для проверки."
                )
                return web.json_response({"ok": True, "matched": False})

        await apply_piarflow_unsubscribe_penalty(user_id, signature, matched_link or offer_link)
        print(
            f"✅ PiarFlow webhook: отписка обработана "
            f"user_id={user_id}, signature={signature}, link={matched_link or offer_link}"
        )
        return web.json_response({"ok": True})

    except Exception as exc:
        print(f"❌ PiarFlow webhook error user_id={user_id}: {exc}")
        return web.json_response({"ok": False}, status=500)


def _check_flyer_webhook_auth(request: web.Request) -> bool:
    """Секретный query-параметр или официальный IP-адрес Flyer."""
    secret = (os.getenv("FLYER_WEBHOOK_SECRET", "").strip()
              or str(getattr(core, "FLYER_WEBHOOK_SECRET", "") or "").strip())
    supplied = (request.query.get("secret", "")
                or request.headers.get("X-Secret-Token", "")
                or request.headers.get("X-Secret", ""))
    if secret and supplied and hmac.compare_digest(supplied, secret):
        return True
    try:
        ip = ipaddress.ip_address(request.remote or "")
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return str(ip) == "192.146.138.60"
    except ValueError:
        return False


async def _apply_flyer_unsubscribe_event(user_id: int, signature: str, link: str = ""):
    """Находит и штрафует только выплату, связанную с точной подписью Flyer."""
    if await get_task_status(user_id, signature) == "unsubscribed":
        print(f"ℹ️ Flyer: повторное событие {user_id}/{signature} пропущено")
        return

    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, user_id, amount, task_type, task_id, frozen_at,
                   unfreeze_at, is_unfrozen, penalty_applied
            FROM frozen_transactions
            WHERE user_id = ? AND task_type = 'core.flyer'
              AND CAST(task_id AS TEXT) = ?
            ORDER BY id DESC LIMIT 1
        """, (user_id, str(signature))) as cursor:
            tx_row = await cursor.fetchone()

    if tx_row is None:
        print(f"⚠️ Flyer: выплата не найдена для {user_id}/{signature}; штраф не применён")
        return

    applied = await apply_penalty_for_transaction(tx_row)
    if applied:
        print(f"✅ Flyer: отписка обработана {user_id}/{signature}, {link}")


async def _flyer_webhook(request: web.Request):
    if not _check_flyer_webhook_auth(request):
        print(f"⛔ Flyer webhook отклонён: {request.remote}")
        return web.json_response({"status": False}, status=403)

    payload = await _read_json(request)
    if not isinstance(payload, dict):
        return web.json_response({"status": False}, status=400)

    event = str(payload.get("type") or "").strip().lower()
    print(f"🔔 Flyer webhook ({event}): {json.dumps(payload, ensure_ascii=False)[:2000]}")
    if event == "test":
        return web.json_response({"status": True})
    if event != "new_status":
        return web.json_response({"status": True})

    event_data = payload.get("data")
    if not isinstance(event_data, dict):
        return web.json_response({"status": False}, status=400)
    if str(event_data.get("status") or "").strip().lower() != "abort":
        return web.json_response({"status": True})

    try:
        user_id = int(event_data.get("user_id"))
        signature = str(event_data.get("signature") or "").strip()
        link = str(event_data.get("link") or "").strip()
    except (TypeError, ValueError):
        return web.json_response({"status": False}, status=400)
    if user_id <= 0 or not signature:
        print(f"⚠️ Flyer webhook: отсутствует user_id/signature: {event_data}")
        return web.json_response({"status": False}, status=400)

    try:
        await _apply_flyer_unsubscribe_event(user_id, signature, link)
    except Exception as exc:
        print(f"❌ Flyer webhook error {user_id}/{signature}: {exc}")
        return web.json_response({"status": False}, status=500)
    return web.json_response({"status": True})


async def start_webhook_server():
    global _webhook_runner
    app = web.Application()
    app.router.add_post("/webhook/piarflow", _piarflow_webhook)
    app.router.add_post("/webhook/flyer", _flyer_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    _webhook_runner = runner
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"🌐 Вебхук-сервер запущен на порту {WEBHOOK_PORT}")


# ===== Crypto Pay API =====
CRYPTO_PAY_API = "https://pay.crypt.bot/api/"

async def _crypto_request(method: str, payload: dict | None = None):
    token = (core.CRYPTO_BOT_TOKEN or "").strip()
    if not token:
        print("⚠️ CryptoBot: API-токен не задан")
        return None
    headers = {"Crypto-Pay-API-Token": token, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(CRYPTO_PAY_API + method, headers=headers, json=payload or {}) as response:
                data = await response.json(content_type=None)
                if response.status != 200 or not data.get("ok"):
                    print(f"⚠️ CryptoBot {method}: HTTP {response.status}; {data.get('error', data)}")
                    return None
                return data.get("result")
    except Exception as exc:
        print(f"⚠️ CryptoBot request {method}: {exc}")
        return None

async def load_crypto_token():
    token = await get_setting("core.CRYPTO_BOT_TOKEN") or await get_setting("crypto_bot_token") or core.DEFAULT_CRYPTO_BOT_TOKEN
    core.CRYPTO_BOT_TOKEN = (token or "").strip()
    core.crypto_enabled = bool(core.CRYPTO_BOT_TOKEN)
    return core.crypto_enabled

async def get_crypto_bot_balance():
    result = await _crypto_request("getBalance")
    if not result:
        return 0.0
    for item in result:
        if str(item.get("currency_code", "")).upper() in ("USDT", "USD"): 
            try: return float(item.get("available", 0))
            except (TypeError, ValueError): return 0.0
    return 0.0

async def _create_invoice(amount, user_id, description):
    result = await _crypto_request("createInvoice", {"asset":"USDT", "amount":str(round(float(amount), 2)), "description":description, "payload":str(user_id), "allow_comments":False, "allow_anonymous":False})
    if not result: return None
    return result

async def create_ad_invoice(user_id, amount):
    invoice = await _create_invoice(amount, user_id, "Пополнение рекламного баланса")
    if not invoice: return None
    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute("INSERT INTO ad_topups (user_id, amount, invoice_id, status) VALUES (?, ?, ?, 'pending')", (user_id, float(amount), str(invoice.get("invoice_id"))))
        await db.commit()
    return invoice.get("bot_invoice_url") or invoice.get("pay_url")

async def check_ad_invoice(invoice_id):
    result = await _crypto_request("getInvoices", {"invoice_ids":str(invoice_id)})
    items = result.get("items", []) if isinstance(result, dict) else []
    if not items: return False
    invoice = items[0]
    if invoice.get("status") != "paid": return False
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute("SELECT user_id, amount, status FROM ad_topups WHERE invoice_id = ?", (str(invoice_id),)) as cur:
            row = await cur.fetchone()
        if not row: return False
        if row[2] == "paid": return "already_paid"
        await db.execute("UPDATE users SET ad_balance = COALESCE(ad_balance,0) + ? WHERE user_id = ?", (float(row[1]), int(row[0])))
        await db.execute("UPDATE ad_topups SET status='paid' WHERE invoice_id=?", (str(invoice_id),))
        await db.commit()
    return True

async def create_reserve_topup_invoice(amount, user_id):
    invoice = await _create_invoice(amount, user_id, "Пополнение резерва системы")
    return (invoice or {}).get("bot_invoice_url") or (invoice or {}).get("pay_url")

async def generate_crypto_receipt(amount, user_id):
    result = await _crypto_request("createCheck", {"asset":"USDT", "amount":str(round(float(amount), 2)), "pin_to_user_id":int(user_id)})
    if not result: return False, None
    return True, result.get("bot_check_url")

# Generic unsubscribe fallback for webhook events. Unknown signatures are ignored safely.
async def _generic_penalty(user_id: int, signature: str, service: str):
    if not signature: return
    status = await get_task_status(user_id, signature)
    if status in ("unsubscribed", "cancelled"): return
    await set_task_status(user_id, signature, "unsubscribed")
