# -*- coding: utf-8 -*-
"""Backend Mini App: безопасная авторизация через Telegram initData.

Маршруты регистрируются в уже существующем aiohttp-сервере (порт 8080),
Nginx проксирует /miniapi/ на этот сервер.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
import uuid

import aiohttp.web as web
import aiosqlite

import core
import services
from db import get_setting, get_task_status, get_user

STAR_USD = 0.017
MINIAPP_URL = os.getenv("MINIAPP_URL", "https://uamoneybots.ru/miniapp/").rstrip("/") + "/"
MINIAPP_DEMO = os.getenv("MINIAPP_DEMO", "").strip().lower() in {"1", "true", "yes", "on"}
MAX_AUTH_AGE = 24 * 60 * 60

DONE_STATUSES = {"completed", "done", "complete", True, "waiting", "unsubscribed"}


def _tg_link(value) -> str:
    """Приводит @username / t.me/... к обычной ссылке Telegram."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        return "https://t.me/" + raw[1:]
    if raw.startswith("https://t.me/") or raw.startswith("http://t.me/"):
        return raw
    if raw.startswith("t.me/"):
        return "https://" + raw
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", raw):
        return "https://t.me/" + raw
    return raw


def _task(group: str, key: str, title: str, type_label: str, url: str,
          reward_usd: float, icon: str = "📢", button_text: str = "Подписаться") -> dict:
    return {
        "id": f"{group}_{key}"[:80],
        "group": group,
        "title": str(title or "Задание")[:80],
        "type": type_label,
        "url": url,
        "icon": icon,
        "reward_usd": round(float(reward_usd or 0), 6),
        "reward_stars": _stars(reward_usd),
        "button_text": button_text,
        "demo": False,
    }


async def _is_done(user_id: int, signature: str) -> bool:
    if not signature:
        return False
    try:
        status = await get_task_status(user_id, signature)
        return status in DONE_STATUSES
    except Exception:
        return False


async def _collect_task1(user_id: int, language: str):
    """Задания №1: Flyer + кастомные задания на подписку."""
    items = []
    try:
        flyer_tasks = await core.flyer_get_tasks(user_id, language, limit=10)
        for index, task in enumerate(flyer_tasks or []):
            signature = task.get("signature", "") or ""
            if await _is_done(user_id, signature):
                continue
            links = task.get("links")
            url = ""
            if isinstance(links, list) and links:
                url = links[0]
            if not url:
                url = task.get("url") or task.get("link") or ""
            if not url and task.get("username"):
                url = f"https://t.me/{str(task['username']).lstrip('@')}"
            if not url and task.get("resource_id"):
                url = f"https://t.me/{str(task['resource_id']).replace('@', '')}"
            url = _tg_link(url)
            if not url:
                continue
            reward = float(task.get("price") or 0) or float(core.FLZAD)
            title = task.get("name") or task.get("title") or "Задание Flyer"
            task_kind = str(task.get("task") or task.get("type") or "")
            button = "Запустить" if "bot" in task_kind else "Подписаться"
            items.append(_task("task1", signature or str(index), title, task_kind or "Flyer",
                               url, reward, "🤖" if button == "Запустить" else "📢", button))
    except Exception as exc:
        print(f"⚠️ Mini App task1 Flyer: {exc}")

    try:
        async with aiosqlite.connect("bot_database.db") as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM custom_tasks WHERE is_active=1 "
                "AND (max_completions=0 OR current_completions < max_completions)"
            ) as cursor:
                custom_tasks = await cursor.fetchall()
            for task in custom_tasks:
                async with db.execute(
                    "SELECT 1 FROM custom_task_completions WHERE task_id=? AND user_id=?",
                    (task["id"], user_id),
                ) as cursor:
                    if await cursor.fetchone():
                        continue
                raw_url = task["url"]
                if task.get("task_type") == "subscribe_channel" and task.get("channel_username"):
                    raw_url = task["channel_username"]
                url = _tg_link(raw_url)
                if not url:
                    continue
                items.append(_task("task1", f"custom_{task['id']}", task["name"],
                                   "Кастомное", url, float(task.get("price") or 0), "📢",
                                   task.get("button_text") or "Выполнить"))
    except Exception as exc:
        print(f"⚠️ Mini App task1 custom: {exc}")
    return items


async def _collect_task2(user_id: int):
    """Задания №2: продвигаемые пользователями каналы и чаты."""
    items = []
    try:
        reward = float(await get_setting("user_task_reward") or 0.015)
        async with aiosqlite.connect("bot_database.db") as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM user_tasks WHERE is_active=TRUE "
                "AND task_type IN ('channel','chat') AND COALESCE(is_paused,0)=0 "
                "AND current_completions < max_completions "
                "AND id NOT IN (SELECT task_id FROM user_task_completions WHERE user_id=?)",
                (user_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            for task in rows:
                url = _tg_link(task["channel_link"] or task["channel_username"])
                if not url:
                    continue
                task_type = task["task_type"] or "channel"
                items.append(_task(
                    "task2", str(task["id"]), task["channel_username"] or "Продвигаемый канал",
                    "Канал" if task_type == "channel" else "Чат", url, reward,
                    "📢" if task_type == "channel" else "💬",
                    "Подписаться" if task_type == "channel" else "Вступить",
                ))
    except Exception as exc:
        print(f"⚠️ Mini App task2: {exc}")
    return items


async def _collect_task3(user_id: int):
    """Задания №3: PiarFlow."""
    items = []
    try:
        if services.piarflow_last_error():
            return items
        piarflow_tasks = await services.get_piarflow_tasks(user_id, user_id, max_sponsors=5)
        for index, task in enumerate(piarflow_tasks or []):
            signature = task.get("signature", "") or ""
            if await _is_done(user_id, signature):
                continue
            url = _tg_link(task.get("url") or "")
            if not url:
                continue
            title = task.get("name") or task.get("username") or f"Спонсор {index + 1}"
            items.append(_task("task3", signature or str(index), title, "PiarFlow", url,
                               float(task.get("price") or 0), "📢", "Подписаться"))
    except Exception as exc:
        print(f"⚠️ Mini App task3: {exc}")
    return items


async def _collect_task4(user_id: int, language: str):
    """Задания №4: Pulsar-спонсоры."""
    items = []
    try:
        if services.pulsar_last_error():
            return items
        sponsors = await services.pulsar_get_sponsors(user_id, language)
        reward = services.pulsar_reward()
        for index, sponsor in enumerate(sponsors or []):
            signature = services.pulsar_signature(sponsor)
            if await _is_done(user_id, signature):
                continue
            url = services.pulsar_sponsor_link(sponsor)
            if not url:
                continue
            sponsor_id = ""
            if isinstance(sponsor, dict):
                sponsor_id = str(sponsor.get("id") or sponsor.get("sponsor_id") or "")
            await services.save_pulsar_offer(user_id, signature, url, sponsor_id)
            title = (sponsor.get("username") if isinstance(sponsor, dict) else None) or f"Спонсор {sponsor_id or index + 1}"
            items.append(_task("task4", signature or str(index), title, "Pulsar", url,
                               float(reward), "📢", "Подписаться"))
    except Exception as exc:
        print(f"⚠️ Mini App task4: {exc}")
    return items


def _stars(usd: float) -> float:
    return round(float(usd or 0) / STAR_USD, 2)


HISTORY_TASK_LABELS = {
    "core.flyer": "Задание №1 · Flyer",
    "custom_subscribe": "Задание №1 · подписка",
    "user_task": "Задание №2 · продвижение",
    "piarflow": "Задание №3 · PiarFlow",
    "pulsar": "Задание №4 · Pulsar",
}

HISTORY_STATUS_LABELS = {
    "pending": "ожидает",
    "approved": "одобрено",
    "declined": "отклонено",
    "completed": "выполнено",
    "done": "выполнено",
    "complete": "выполнено",
    "waiting": "ожидает проверки",
    "unsubscribed": "отписка",
}


def _history_date(value) -> str:
    raw = str(value or "").replace("T", " ")
    if len(raw) >= 16:
        return raw[8:10] + "." + raw[5:7] + " " + raw[11:16]
    return raw[:16] or "—"


def _history_item(item_id, title, meta, amount_usd, status, sort_key=None):
    amount = round(float(amount_usd or 0), 6)
    return {
        "id": str(item_id),
        "title": title,
        "meta": meta,
        "amount_usd": amount,
        "amount_stars": round(abs(amount) / STAR_USD, 2),
        "sign": "+" if amount >= 0 else "−",
        "status": status,
        "date": meta,
        "_sort": str(sort_key or meta or ""),
    }


CASE_DEFS = {
    "start": {
        "id": "start",
        "title": "Старт",
        "cost_stars": 2.5,
        "max_stars": 50,
        "prizes": (
            (0.35, 0.40),
            (0.70, 0.25),
            (1.25, 0.15),
            (2.50, 0.10),
            (5.00, 0.06),
            (15.00, 0.03),
            (50.00, 0.01),
        ),
    },
    "pro": {
        "id": "pro",
        "title": "Про",
        "cost_stars": 12.5,
        "max_stars": 250,
        "prizes": (
            (1.75, 0.40),
            (3.50, 0.25),
            (6.25, 0.15),
            (12.50, 0.10),
            (25.00, 0.06),
            (75.00, 0.03),
            (250.00, 0.01),
        ),
    },
    "giant": {
        "id": "giant",
        "title": "Гигант",
        "cost_stars": 50.0,
        "max_stars": 500,
        "prizes": (
            (5.00, 0.35),
            (15.00, 0.30),
            (50.00, 0.20),
            (125.00, 0.09),
            (250.00, 0.05),
            (500.00, 0.01),
        ),
    },
}


def _case_public(case: dict) -> dict:
    return {
        "id": case["id"],
        "title": case["title"],
        "cost_stars": case["cost_stars"],
        "cost_usd": round(case["cost_stars"] * STAR_USD, 6),
        "max_stars": case["max_stars"],
        "prizes": [
            {"stars": stars, "chance": chance}
            for stars, chance in case["prizes"]
        ],
    }


def _case_expected_stars(case: dict) -> float:
    return round(sum(stars * chance for stars, chance in case["prizes"]), 4)


def _case_round_code(round_id: str, case_id: str, prize_stars: float, nonce: str) -> str:
    key = hmac.new(
        core.BOT_TOKEN.encode("utf-8"), b"miniapp-case-round", hashlib.sha256
    ).digest()
    message = f"{round_id}:{case_id}:{prize_stars}:{nonce}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:16].upper()


async def _ensure_case_schema() -> None:
    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS miniapp_case_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                case_id TEXT NOT NULL,
                cost_usd REAL NOT NULL,
                prize_usd REAL NOT NULL,
                prize_stars REAL NOT NULL,
                round_id TEXT NOT NULL UNIQUE,
                code TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()


async def miniapp_cases(request: web.Request) -> web.Response:
    await _identity(request)
    cases = [_case_public(CASE_DEFS[key]) for key in ("start", "pro", "giant")]
    for item in cases:
        item["expected_stars"] = _case_expected_stars(CASE_DEFS[item["id"]])
    return web.json_response(
        {
            "cases": cases,
            "star_usd": STAR_USD,
            "message": "Приз зачисляется сразу на выводимый баланс. Код раунда сохраняется в истории.",
        }
    )


async def miniapp_case_open(request: web.Request) -> web.Response:
    user_id = await _identity(request)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный запрос"}, status=400)

    case_id = str(payload.get("case_id") or "").strip().lower()
    case = CASE_DEFS.get(case_id)
    if not case:
        return web.json_response({"ok": False, "error": "Такого кейса нет"}, status=400)

    await _ensure_case_schema()
    cost_usd = round(case["cost_stars"] * STAR_USD, 6)
    user = await get_user(user_id)
    if not user or float(user.get("balance", 0) or 0) + 1e-9 < cost_usd:
        return web.json_response(
            {
                "ok": False,
                "error": f"Не хватает {case['cost_stars']} ⭐ на балансе",
                "need_stars": case["cost_stars"],
            },
            status=400,
        )

    roll = secrets.SystemRandom().random()
    cumulative = 0.0
    prize_stars = case["prizes"][-1][0]
    for stars, chance in case["prizes"]:
        cumulative += chance
        if roll < cumulative:
            prize_stars = stars
            break

    prize_usd = round(prize_stars * STAR_USD, 6)
    round_id = uuid.uuid4().hex
    nonce = secrets.token_hex(8)
    code = _case_round_code(round_id, case_id, prize_stars, nonce)

    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (cost_usd, user_id, cost_usd),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return web.json_response(
                {"ok": False, "error": "Недостаточно средств"}, status=400
            )
        await db.execute(
            "UPDATE users SET balance = balance + ?, "
            "lifetime_earned = COALESCE(lifetime_earned, 0) + ? WHERE user_id = ?",
            (prize_usd, prize_usd, user_id),
        )
        await db.execute(
            "INSERT INTO miniapp_case_rounds "
            "(user_id, case_id, cost_usd, prize_usd, prize_stars, round_id, code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, case_id, cost_usd, prize_usd, prize_stars, round_id, code),
        )
        await db.execute(
            "INSERT INTO balance_history (user_id, amount, type, reason) "
            "VALUES (?, ?, ?, ?)",
            (user_id, -cost_usd, "case", f"Кейс {case['title']} · ставка"),
        )
        await db.execute(
            "INSERT INTO balance_history (user_id, amount, type, reason) "
            "VALUES (?, ?, ?, ?)",
            (user_id, prize_usd, "case", f"Кейс {case['title']} · код {code}"),
        )
        await db.commit()

    balance_row = await get_user(user_id)
    return web.json_response(
        {
            "ok": True,
            "case": _case_public(case),
            "prize_stars": prize_stars,
            "prize_usd": prize_usd,
            "cost_usd": cost_usd,
            "round_id": round_id,
            "code": code,
            "roll": round(roll, 8),
            "balance_usd": round(float(balance_row.get("balance", 0) or 0), 6)
            if balance_row
            else 0,
            "balance_stars": round(float(balance_row.get("balance", 0) or 0) / STAR_USD, 2)
            if balance_row
            else 0,
            "message": f"{case['title']}: +{prize_stars} ⭐",
        }
    )


ROULETTE_MULTIPLIERS = (
    0.00, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70,
    0.80, 0.90, 1.00, 1.20, 1.40, 1.60, 1.98, 2.40,
)
ROULETTE_BETS = (1.25, 2.50, 5.00)


def _roulette_public() -> dict:
    return {
        "sectors": len(ROULETTE_MULTIPLIERS),
        "multipliers": list(ROULETTE_MULTIPLIERS),
        "return_percent": round(
            sum(ROULETTE_MULTIPLIERS) / len(ROULETTE_MULTIPLIERS) * 100, 2
        ),
        "max_multiplier": max(ROULETTE_MULTIPLIERS),
        "bets": [
            {
                "stars": stars,
                "usd": round(stars * STAR_USD, 6),
                "max_win_stars": round(stars * max(ROULETTE_MULTIPLIERS), 2),
            }
            for stars in ROULETTE_BETS
        ],
        "star_usd": STAR_USD,
    }


def _roulette_round_code(round_id: str, bet_stars: float, multiplier: float, nonce: str) -> str:
    key = hmac.new(
        core.BOT_TOKEN.encode("utf-8"), b"miniapp-roulette-round", hashlib.sha256
    ).digest()
    message = f"{round_id}:{bet_stars}:{multiplier}:{nonce}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:16].upper()


async def _ensure_roulette_schema() -> None:
    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS miniapp_roulette_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bet_usd REAL NOT NULL,
                bet_stars REAL NOT NULL,
                multiplier REAL NOT NULL,
                payout_usd REAL NOT NULL,
                payout_stars REAL NOT NULL,
                round_id TEXT NOT NULL UNIQUE,
                code TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()


async def miniapp_roulette(request: web.Request) -> web.Response:
    await _identity(request)
    return web.json_response(
        {
            **_roulette_public(),
            "message": "Выпадет число — ставка умножится на него. Ноль — ставка сгорает.",
        }
    )


async def miniapp_roulette_spin(request: web.Request) -> web.Response:
    user_id = await _identity(request)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный запрос"}, status=400)

    try:
        bet_stars = float(payload.get("bet_stars") or 0)
    except (TypeError, ValueError):
        bet_stars = 0.0
    if bet_stars not in ROULETTE_BETS:
        return web.json_response(
            {"ok": False, "error": "Выберите ставку 1.25, 2.5 или 5 ⭐"}, status=400
        )

    await _ensure_roulette_schema()
    bet_usd = round(bet_stars * STAR_USD, 6)
    user = await get_user(user_id)
    if not user or float(user.get("balance", 0) or 0) + 1e-9 < bet_usd:
        return web.json_response(
            {
                "ok": False,
                "error": f"Не хватает {bet_stars} ⭐ на ставку",
                "need_stars": bet_stars,
            },
            status=400,
        )

    multiplier = ROULETTE_MULTIPLIERS[secrets.SystemRandom().randrange(len(ROULETTE_MULTIPLIERS))]
    payout_stars = round(bet_stars * multiplier, 4)
    payout_usd = round(payout_stars * STAR_USD, 6)
    round_id = uuid.uuid4().hex
    nonce = secrets.token_hex(8)
    code = _roulette_round_code(round_id, bet_stars, multiplier, nonce)

    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (bet_usd, user_id, bet_usd),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return web.json_response(
                {"ok": False, "error": "Недостаточно средств"}, status=400
            )
        if payout_usd > 0:
            await db.execute(
                "UPDATE users SET balance = balance + ?, "
                "lifetime_earned = COALESCE(lifetime_earned, 0) + ? WHERE user_id = ?",
                (payout_usd, payout_usd, user_id),
            )
        await db.execute(
            "INSERT INTO miniapp_roulette_rounds "
            "(user_id, bet_usd, bet_stars, multiplier, payout_usd, payout_stars, round_id, code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, bet_usd, bet_stars, multiplier, payout_usd, payout_stars, round_id, code),
        )
        await db.execute(
            "INSERT INTO balance_history (user_id, amount, type, reason) "
            "VALUES (?, ?, ?, ?)",
            (user_id, -bet_usd, "roulette", f"Рулетка · ставка {bet_stars} ⭐"),
        )
        if payout_usd > 0:
            await db.execute(
                "INSERT INTO balance_history (user_id, amount, type, reason) "
                "VALUES (?, ?, ?, ?)",
                (user_id, payout_usd, "roulette", f"Рулетка · x{multiplier} · код {code}"),
            )
        await db.commit()

    balance_row = await get_user(user_id)
    balance = float(balance_row.get("balance", 0) or 0) if balance_row else 0
    if multiplier == 0:
        message = "Сгорело. Код раунда " + code
    elif multiplier < 1:
        message = f"Возврат {payout_stars} ⭐. Код раунда {code}"
    else:
        message = f"Выигрыш +{payout_stars} ⭐. Код раунда {code}"

    return web.json_response(
        {
            "ok": True,
            "bet_stars": bet_stars,
            "bet_usd": bet_usd,
            "multiplier": multiplier,
            "payout_stars": payout_stars,
            "payout_usd": payout_usd,
            "round_id": round_id,
            "code": code,
            "balance_usd": round(balance, 6),
            "balance_stars": round(balance / STAR_USD, 2),
            "message": message,
        }
    )


async def miniapp_history(request: web.Request) -> web.Response:
    user_id = await _identity(request)
    items = []

    try:
        async with aiosqlite.connect("bot_database.db") as db:
            db.row_factory = aiosqlite.Row

            async with db.execute(
                "SELECT id, amount, task_type, task_id, frozen_at, is_unfrozen "
                "FROM frozen_transactions WHERE user_id=? ORDER BY id DESC LIMIT 20",
                (user_id,),
            ) as cursor:
                for row in await cursor.fetchall():
                    label = HISTORY_TASK_LABELS.get(row["task_type"], "Задание")
                    status = "разморожено" if row["is_unfrozen"] else "заморожено на 24ч"
                    items.append(_history_item(
                        f"frozen_{row['id']}",
                        label,
                        _history_date(row["frozen_at"]),
                        row["amount"],
                        status,
                        row["frozen_at"],
                    ))

            async with db.execute(
                "SELECT id, amount, payout_method, stars_amount, status, created_at "
                "FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 20",
                (user_id,),
            ) as cursor:
                for row in await cursor.fetchall():
                    method = row["payout_method"] or "usdt"
                    stars = row["stars_amount"]
                    if method == "stars" and stars:
                        title = f"Вывод Stars · {stars} ⭐"
                    else:
                        title = "Вывод USDT"
                    status = HISTORY_STATUS_LABELS.get(row["status"], row["status"] or "—")
                    items.append(_history_item(
                        f"withdrawal_{row['id']}",
                        title,
                        _history_date(row["created_at"]),
                        -abs(float(row["amount"] or 0)),
                        status,
                        row["created_at"],
                    ))

            async with db.execute(
                "SELECT id, amount, type, reason, created_at "
                "FROM balance_history WHERE user_id=? ORDER BY id DESC LIMIT 20",
                (user_id,),
            ) as cursor:
                for row in await cursor.fetchall():
                    title = row["reason"] or row["type"] or "Изменение баланса"
                    items.append(_history_item(
                        f"balance_{row['id']}",
                        str(title)[:80],
                        _history_date(row["created_at"]),
                        row["amount"],
                        "зачислено" if float(row["amount"] or 0) >= 0 else "списано",
                        row["created_at"],
                    ))

            async with db.execute(
                "SELECT signature, status, completed_at "
                "FROM completed_tasks WHERE user_id=? ORDER BY completed_at DESC LIMIT 20",
                (user_id,),
            ) as cursor:
                for row in await cursor.fetchall():
                    signature = str(row["signature"] or "")
                    label = "Задание"
                    if signature.startswith("custom_"):
                        label = "Задание №1 · подписка"
                    elif "pulsar" in signature.lower():
                        label = "Задание №4 · Pulsar"
                    elif "piarflow" in signature.lower():
                        label = "Задание №3 · PiarFlow"
                    elif "flyer" in signature.lower() or "core" in signature.lower():
                        label = "Задание №1 · Flyer"
                    status = HISTORY_STATUS_LABELS.get(row["status"], row["status"] or "—")
                    items.append(_history_item(
                        f"task_{signature[:40]}",
                        label,
                        _history_date(row["completed_at"]),
                        0,
                        status,
                        row["completed_at"],
                    ))

            try:
                async with db.execute(
                    "SELECT case_id, prize_usd, prize_stars, code, created_at "
                    "FROM miniapp_case_rounds WHERE user_id=? ORDER BY id DESC LIMIT 20",
                    (user_id,),
                ) as cursor:
                    for row in await cursor.fetchall():
                        title = CASE_DEFS.get(row["case_id"], {}).get("title", "Кейс")
                        items.append(_history_item(
                            f"case_{row['code']}",
                            f"Кейс · {title}",
                            _history_date(row["created_at"]) + " · код " + row["code"],
                            row["prize_usd"],
                            f"выигрыш {row['prize_stars']} ⭐",
                            row["created_at"],
                        ))
            except Exception:
                pass

            try:
                async with db.execute(
                    "SELECT bet_stars, multiplier, payout_usd, payout_stars, code, created_at "
                    "FROM miniapp_roulette_rounds WHERE user_id=? ORDER BY id DESC LIMIT 20",
                    (user_id,),
                ) as cursor:
                    for row in await cursor.fetchall():
                        items.append(_history_item(
                            f"roulette_{row['code']}",
                            f"Рулетка · x{row['multiplier']}",
                            _history_date(row["created_at"]) + " · код " + row["code"],
                            row["payout_usd"] if row["payout_usd"] else -round(
                                float(row["bet_stars"] or 0) * STAR_USD, 6
                            ),
                            f"ставка {row['bet_stars']} ⭐",
                            row["created_at"],
                        ))
            except Exception:
                pass
    except Exception as exc:
        print(f"⚠️ Mini App history: {exc}")
        raise web.HTTPInternalServerError(reason="History unavailable")

    items.sort(key=lambda item: item.pop("_sort", ""), reverse=True)
    return web.json_response(
        {
            "items": items[:40],
            "count": len(items),
            "message": "" if items else "Операций пока нет. Выполни задание или выведи средства.",
        }
    )


def _ensure_admin(user_id: int) -> None:
    """Mini App на этом этапе доступен только администраторам."""
    admin_ids = {int(x) for x in core.ADMIN_IDS if str(x).isdigit()}
    if int(user_id) not in admin_ids:
        print(f"⚠️ Mini App: доступ запрещён user_id={int(user_id)}", flush=True)
        raise web.HTTPForbidden(reason="Admin only")


def _verify_init_data(init_data: str) -> int | None:
    """Проверяет подпись Telegram WebApp initData и возвращает user_id."""
    if not init_data:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        data = dict(pairs)
        received_hash = data.pop("hash", "")
        if not received_hash:
            return None

        auth_date = int(data.get("auth_date", "0") or 0)
        if auth_date <= 0 or abs(time.time() - auth_date) > MAX_AUTH_AGE:
            return None

        check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(data.items())
        )
        secret_key = hmac.new(
            b"WebAppData", core.BOT_TOKEN.encode("utf-8"), hashlib.sha256
        ).digest()
        expected_hash = hmac.new(
            secret_key, check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_hash, received_hash):
            return None

        user_raw = data.get("user")
        if not user_raw:
            return None
        user = json.loads(user_raw)
        user_id = int(user["id"])
        return user_id if user_id > 0 else None
    except Exception as exc:
        print(f"⚠️ Mini App initData: не удалось проверить подпись: {exc}")
        return None


async def _identity(request: web.Request) -> int:
    """Возвращает user_id или выбрасывает 401/403."""
    demo = request.query.get("demo") == "1"
    if demo:
        if not MINIAPP_DEMO:
            raise web.HTTPUnauthorized(reason="Demo mode disabled")
        try:
            user_id = int(request.query.get("user_id") or core.ADMIN_IDS[0])
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(reason="Invalid demo user_id")
        _ensure_admin(user_id)
        return user_id

    init_data = (
        request.headers.get("X-Telegram-Init-Data")
        or request.query.get("init_data")
        or ""
    )
    user_id = _verify_init_data(init_data)
    if not user_id:
        raise web.HTTPUnauthorized(reason="Invalid Telegram initData")
    _ensure_admin(user_id)
    return user_id


async def _get_user_or_403(user_id: int):
    user = await get_user(user_id)
    if not user:
        raise web.HTTPForbidden(reason="User not found")
    return user


async def miniapp_profile(request: web.Request) -> web.Response:
    user_id = await _identity(request)
    user = await _get_user_or_403(user_id)

    balance = float(user.get("balance", 0) or 0)
    frozen = float(user.get("frozen_balance", 0) or 0)
    lifetime = float(user.get("lifetime_earned", 0) or 0)
    completed = int(user.get("completed_tasks", 0) or 0)
    level = completed // 3
    exp = completed % 3

    return web.json_response(
        {
            "user": {
                "id": user_id,
                "username": user.get("username") or "Пользователь",
                "completed": completed,
                "level": level,
                "exp": exp,
                "exp_next": 3,
                "referral_link": f"https://t.me/UAMONEY_BOT?start={user_id}",
            },
            "balance": {
                "usd": round(balance, 3),
                "stars": _stars(balance),
                "frozen_usd": round(frozen, 3),
                "frozen_stars": _stars(frozen),
                "lifetime_usd": round(lifetime, 3),
                "lifetime_stars": _stars(lifetime),
                "star_usd": STAR_USD,
            },
            "miniapp_url": MINIAPP_URL,
        }
    )


async def miniapp_tasks(request: web.Request) -> web.Response:
    user_id = await _identity(request)
    language = (request.query.get("lang") or "ru")[:16]

    groups = []
    warnings = []
    collectors = (
        ("Задания №1", "task1", lambda: _collect_task1(user_id, language)),
        ("Задания №2", "task2", lambda: _collect_task2(user_id)),
        ("Задания №3", "task3", lambda: _collect_task3(user_id)),
        ("Задания №4", "task4", lambda: _collect_task4(user_id, language)),
    )
    for label, key, collector in collectors:
        try:
            tasks = await collector()
        except Exception as exc:
            tasks = []
            warnings.append(f"{label}: {exc}")
            print(f"⚠️ Mini App {label}: {exc}")
        groups.append({"key": key, "label": label, "tasks": tasks})

    all_tasks = [task for group in groups for task in group["tasks"]]
    return web.json_response(
        {
            "tasks": all_tasks,
            "groups": groups,
            "demo": False,
            "warnings": warnings,
            "message": "Нажмите «Проверить задания» после подписки — результат придёт в чат с ботом.",
        }
    )


async def miniapp_tasks_check(request: web.Request) -> web.Response:
    await _identity(request)
    return web.json_response(
        {
            "ok": True,
            "message": "Нажмите «Проверить задания» в Mini App — результат придёт в чат с ботом.",
        }
    )


def register_routes(app: web.Application) -> None:
    app.router.add_get("/miniapi/profile", miniapp_profile)
    app.router.add_get("/miniapi/tasks", miniapp_tasks)
    app.router.add_post("/miniapi/tasks/check", miniapp_tasks_check)
    app.router.add_get("/miniapi/history", miniapp_history)
    app.router.add_get("/miniapi/cases", miniapp_cases)
    app.router.add_post("/miniapi/cases/open", miniapp_case_open)
    app.router.add_get("/miniapi/roulette", miniapp_roulette)
    app.router.add_post("/miniapi/roulette/spin", miniapp_roulette_spin)
