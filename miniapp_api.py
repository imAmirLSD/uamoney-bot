# -*- coding: utf-8 -*-
"""Backend Mini App: безопасная авторизация через Telegram initData.

Маршруты регистрируются в уже существующем aiohttp-сервере (порт 8080),
Nginx проксирует /miniapi/ на этот сервер.
"""
import hashlib
import hmac
import math
from datetime import datetime, timedelta, timezone
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
            (50.00, 0.0040),
            (10.00, 0.0375),
            (3.70, 0.1800),
            (1.50, 0.3500),
            (1.00, 0.4285),
        ),
    },
    "pro": {
        "id": "pro",
        "title": "Про",
        "cost_stars": 12.5,
        "max_stars": 250,
        "prizes": (
            (250.00, 0.0020),
            (50.00, 0.0620),
            (17.00, 0.1800),
            (12.50, 0.2000),
            (3.30, 0.5560),
        ),
    },
    "giant": {
        "id": "giant",
        "title": "Гигант",
        "cost_stars": 50.0,
        "max_stars": 500,
        "prizes": (
            (500.00, 0.0050),
            (150.00, 0.0620),
            (75.00, 0.1800),
            (37.00, 0.3200),
            (12.50, 0.4330),
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
        item["return_percent"] = round(
            item["expected_stars"] / item["cost_stars"] * 100, 1
        )

    recent_wins = []
    wins_total_stars = 0.0
    wins_count = 0
    try:
        async with aiosqlite.connect("bot_database.db") as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(prize_stars), 0) AS s "
                "FROM miniapp_case_rounds WHERE created_at >= datetime('now', '-1 day')"
            ) as cursor:
                stat = await cursor.fetchone()
            if stat:
                wins_count = int(stat["n"])
                wins_total_stars = float(stat["s"])
            async with db.execute(
                "SELECT case_id, prize_stars, created_at FROM miniapp_case_rounds "
                "ORDER BY id DESC LIMIT 8"
            ) as cursor:
                for row in await cursor.fetchall():
                    recent_wins.append({
                        "case": CASE_DEFS.get(row["case_id"], {}).get("title", "Кейс"),
                        "stars": row["prize_stars"],
                        "time": _history_date(row["created_at"]),
                    })
    except Exception:
        pass

    return web.json_response(
        {
            "cases": cases,
            "recent_wins": recent_wins,
            "wins_total_stars": round(wins_total_stars, 2),
            "wins_count": wins_count,
            "star_usd": STAR_USD,
            "message": "Приз зачисляется сразу на выводимый баланс. Каждый раунд проверяется по коду.",
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


STARFIELD_CELLS = 25
STARFIELD_MINES = 6
STARFIELD_BETS = (1.25, 2.50, 5.00)
STARFIELD_RETURN = 0.80
STARFIELD_MAX_MULT = 82.56


def _starfield_multipliers() -> dict:
    result = {}
    survival = 1.0
    for step in range(1, STARFIELD_CELLS - STARFIELD_MINES + 1):
        survival *= (STARFIELD_CELLS - STARFIELD_MINES - step + 1) / (
            STARFIELD_CELLS - step + 1
        )
        result[step] = min(
            STARFIELD_MAX_MULT, math.floor(STARFIELD_RETURN / survival * 100) / 100
        )
    return result


STARFIELD_MULTIPLIERS = _starfield_multipliers()


def _starfield_public() -> dict:
    return {
        "cells": STARFIELD_CELLS,
        "mines": STARFIELD_MINES,
        "stars": STARFIELD_CELLS - STARFIELD_MINES,
        "return_percent": int(STARFIELD_RETURN * 100),
        "max_multiplier": STARFIELD_MAX_MULT,
        "multipliers": [STARFIELD_MULTIPLIERS[i] for i in sorted(STARFIELD_MULTIPLIERS)],
        "bets": [
            {
                "stars": stars,
                "usd": round(stars * STAR_USD, 6),
                "max_win_stars": round(stars * STARFIELD_MAX_MULT, 2),
            }
            for stars in STARFIELD_BETS
        ],
        "star_usd": STAR_USD,
    }


def _starfield_round_code(round_id: str, bet_stars: float, multiplier: float, nonce: str) -> str:
    key = hmac.new(
        core.BOT_TOKEN.encode("utf-8"), b"miniapp-starfield-round", hashlib.sha256
    ).digest()
    message = f"{round_id}:{bet_stars}:{multiplier}:{nonce}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:16].upper()


async def _ensure_starfield_schema() -> None:
    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS miniapp_starfield_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                round_id TEXT NOT NULL UNIQUE,
                bet_usd REAL NOT NULL,
                bet_stars REAL NOT NULL,
                mines TEXT NOT NULL,
                opened TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                multiplier REAL NOT NULL DEFAULT 1.0,
                payout_usd REAL NOT NULL DEFAULT 0,
                payout_stars REAL NOT NULL DEFAULT 0,
                code TEXT NOT NULL,
                nonce TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()


def _starfield_state(row) -> dict:
    if not row:
        return None
    return {
        "round_id": row["round_id"],
        "bet_stars": row["bet_stars"],
        "cells": STARFIELD_CELLS,
        "mines_count": STARFIELD_MINES,
        "opened": json.loads(row["opened"] or "[]"),
        "multiplier": row["multiplier"],
        "status": row["status"],
        "can_cashout": row["status"] == "active",
        "code": row["code"],
    }


async def _starfield_active(db, user_id: int):
    async with db.execute(
        "SELECT * FROM miniapp_starfield_rounds "
        "WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ) as cursor:
        return await cursor.fetchone()


async def miniapp_starfield(request: web.Request) -> web.Response:
    user_id = await _identity(request)
    await _ensure_starfield_schema()
    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        active = _starfield_state(await _starfield_active(db, user_id))
    return web.json_response({**_starfield_public(), "active": active})


async def miniapp_starfield_start(request: web.Request) -> web.Response:
    user_id = await _identity(request)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный запрос"}, status=400)

    try:
        bet_stars = float(payload.get("bet_stars") or 0)
    except (TypeError, ValueError):
        bet_stars = 0.0
    if bet_stars not in STARFIELD_BETS:
        return web.json_response(
            {"ok": False, "error": "Выберите ставку 1.25, 2.5 или 5 ⭐"}, status=400
        )

    await _ensure_starfield_schema()
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

    round_id = uuid.uuid4().hex
    nonce = secrets.token_hex(8)
    code = _starfield_round_code(round_id, bet_stars, 1.0, nonce)
    mines = sorted(secrets.SystemRandom().sample(range(STARFIELD_CELLS), STARFIELD_MINES))

    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (bet_usd, user_id, bet_usd),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return web.json_response({"ok": False, "error": "Недостаточно средств"}, status=400)
        await db.execute(
            "INSERT INTO miniapp_starfield_rounds "
            "(user_id, round_id, bet_usd, bet_stars, mines, opened, status, multiplier, code, nonce) "
            "VALUES (?, ?, ?, ?, ?, '[]', 'active', 1.0, ?, ?)",
            (user_id, round_id, bet_usd, bet_stars, json.dumps(mines), code, nonce),
        )
        await db.execute(
            "INSERT INTO balance_history (user_id, amount, type, reason) VALUES (?, ?, ?, ?)",
            (user_id, -bet_usd, "starfield", f"Звёздное поле · ставка {bet_stars} ⭐"),
        )
        await db.commit()
        state = _starfield_state(await _starfield_active(db, user_id))

    balance_row = await get_user(user_id)
    balance = float(balance_row.get("balance", 0) or 0) if balance_row else 0
    return web.json_response(
        {
            "ok": True,
            **state,
            "message": f"Ставка {bet_stars} ⭐ принята. Открывай клетки и забирай, когда захочешь.",
            "balance_usd": round(balance, 6),
            "balance_stars": round(balance / STAR_USD, 2),
        }
    )


async def miniapp_starfield_open(request: web.Request) -> web.Response:
    user_id = await _identity(request)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный запрос"}, status=400)

    round_id = str(payload.get("round_id") or "")
    try:
        index = int(payload.get("index"))
    except (TypeError, ValueError):
        index = -1
    if not round_id or not 0 <= index < STARFIELD_CELLS:
        return web.json_response({"ok": False, "error": "Некорректная клетка"}, status=400)

    await _ensure_starfield_schema()
    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT * FROM miniapp_starfield_rounds WHERE round_id=? AND user_id=? AND status='active'",
            (round_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            await db.rollback()
            return web.json_response({"ok": False, "error": "Раунд не найден или завершён"}, status=404)

        opened = json.loads(row["opened"] or "[]")
        if index in opened:
            await db.rollback()
            return web.json_response({"ok": False, "error": "Клетка уже открыта"}, status=400)
        opened.append(index)
        mines = set(json.loads(row["mines"] or "[]"))
        hit_mine = index in mines
        stars_opened = len(opened)
        multiplier = row["multiplier"] if hit_mine else STARFIELD_MULTIPLIERS.get(
            stars_opened, STARFIELD_MAX_MULT
        )
        status = "lost" if hit_mine else "active"
        code = _starfield_round_code(
            round_id, row["bet_stars"], 0.0 if hit_mine else multiplier, row["nonce"]
        )
        await db.execute(
            "UPDATE miniapp_starfield_rounds SET opened=?, status=?, multiplier=?, code=? WHERE id=?",
            (json.dumps(opened), status, multiplier, code, row["id"]),
        )
        await db.commit()

    if hit_mine:
        message = f"Чёрная дыра. Ставка сгорела. Код раунда {code}"
    else:
        message = f"Звезда найдена · x{multiplier}. Код раунда {code}"
    return web.json_response(
        {
            "ok": True,
            "round_id": round_id,
            "index": index,
            "hit": "mine" if hit_mine else "star",
            "opened": opened,
            "opened_count": stars_opened,
            "multiplier": multiplier,
            "can_cashout": not hit_mine,
            "game_over": hit_mine,
            "code": code,
            "message": message,
        }
    )


async def miniapp_starfield_cashout(request: web.Request) -> web.Response:
    user_id = await _identity(request)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный запрос"}, status=400)
    round_id = str(payload.get("round_id") or "")
    if not round_id:
        return web.json_response({"ok": False, "error": "Раунд не найден"}, status=400)

    await _ensure_starfield_schema()
    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT * FROM miniapp_starfield_rounds WHERE round_id=? AND user_id=? AND status='active'",
            (round_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            await db.rollback()
            return web.json_response({"ok": False, "error": "Раунд уже завершён"}, status=404)

        multiplier = float(row["multiplier"] or 1.0)
        payout_stars = round(float(row["bet_stars"]) * multiplier, 4)
        payout_usd = round(payout_stars * STAR_USD, 6)
        code = _starfield_round_code(round_id, row["bet_stars"], multiplier, row["nonce"])
        await db.execute(
            "UPDATE miniapp_starfield_rounds SET status='cashed', payout_usd=?, payout_stars=?, code=? WHERE id=?",
            (payout_usd, payout_stars, code, row["id"]),
        )
        if payout_usd > 0:
            await db.execute(
                "UPDATE users SET balance = balance + ?, "
                "lifetime_earned = COALESCE(lifetime_earned, 0) + ? WHERE user_id = ?",
                (payout_usd, payout_usd, user_id),
            )
            await db.execute(
                "INSERT INTO balance_history (user_id, amount, type, reason) VALUES (?, ?, ?, ?)",
                (user_id, payout_usd, "starfield", f"Звёздное поле · x{multiplier} · код {code}"),
            )
        await db.commit()

    balance_row = await get_user(user_id)
    balance = float(balance_row.get("balance", 0) or 0) if balance_row else 0
    return web.json_response(
        {
            "ok": True,
            "multiplier": multiplier,
            "payout_stars": payout_stars,
            "payout_usd": payout_usd,
            "code": code,
            "balance_usd": round(balance, 6),
            "balance_stars": round(balance / STAR_USD, 2),
            "message": f"Забрано {payout_stars} ⭐ · код раунда {code}",
        }
    )


DAILY_TASK_GOAL = 3
DAILY_REWARDS = (0.05, 0.08, 0.12, 0.18, 0.25, 0.35, 0.50)
DAILY_TZ = timezone(timedelta(hours=3))


def _daily_today() -> str:
    return datetime.now(DAILY_TZ).date().isoformat()


def _daily_yesterday() -> str:
    return (datetime.now(DAILY_TZ).date() - timedelta(days=1)).isoformat()


def _daily_code(user_id: int, day: str, streak: int) -> str:
    key = hmac.new(
        core.BOT_TOKEN.encode("utf-8"), b"miniapp-daily-chest", hashlib.sha256
    ).digest()
    message = f"{user_id}:{day}:{streak}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:10].upper()


def _daily_reward(streak: int) -> dict:
    usd = DAILY_REWARDS[max(0, min(streak, 7)) - 1] if streak else 0
    return {"usd": usd, "stars": round(usd / STAR_USD, 2)}


async def _ensure_daily_schema() -> None:
    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS miniapp_daily_streak (
                user_id INTEGER PRIMARY KEY,
                streak INTEGER NOT NULL DEFAULT 0,
                last_day TEXT NOT NULL DEFAULT ''
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS miniapp_daily_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                streak INTEGER NOT NULL,
                reward_usd REAL NOT NULL,
                reward_stars REAL NOT NULL,
                code TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, day)
            )
            """
        )
        await db.commit()


async def _daily_completed_today(db, user_id: int) -> int:
    statuses = list(DONE_STATUSES)
    marks = ",".join("?" for _ in statuses)
    today = _daily_today()
    async with db.execute(
        f"SELECT COUNT(*) AS n FROM completed_tasks "
        f"WHERE user_id=? AND date(completed_at, '+3 hours')=? AND status IN ({marks})",
        (user_id, today, *statuses),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row["n"] if row else 0)


async def _daily_state(db, user_id: int) -> dict:
    today = _daily_today()
    yesterday = _daily_yesterday()
    async with db.execute(
        "SELECT streak, last_day FROM miniapp_daily_streak WHERE user_id=?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        streak, last_day = 0, ""
    else:
        streak, last_day = int(row["streak"]), row["last_day"]
    if last_day not in (today, yesterday):
        streak = 0
    completed = await _daily_completed_today(db, user_id)
    claimed_today = last_day == today
    return {
        "goal": DAILY_TASK_GOAL,
        "completed_today": completed,
        "unlocked": completed >= DAILY_TASK_GOAL,
        "streak": streak,
        "next_streak": min(7, streak + 1) if not claimed_today else streak,
        "claimed_today": claimed_today,
        "today": today,
        "days": [
            {**_daily_reward(i), "day": i, "claimed": i <= streak}
            for i in range(1, 8)
        ],
        "today_reward": _daily_reward(min(7, streak + 1) if not claimed_today else streak),
    }


async def miniapp_daily(request: web.Request) -> web.Response:
    user_id = await _identity(request)
    await _ensure_daily_schema()
    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        state = await _daily_state(db, user_id)
    return web.json_response({
        **state,
        "message": "Заходи каждый день: серия растёт до 7 дней, и сундук с ней.",
    })


async def miniapp_daily_claim(request: web.Request) -> web.Response:
    user_id = await _identity(request)
    await _ensure_daily_schema()
    async with aiosqlite.connect("bot_database.db") as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        state = await _daily_state(db, user_id)
        if state["claimed_today"]:
            await db.rollback()
            return web.json_response({"ok": False, "error": "Сундук уже открыт сегодня"}, status=400)
        if not state["unlocked"]:
            await db.rollback()
            return web.json_response(
                {
                    "ok": False,
                    "error": f"Нужно {DAILY_TASK_GOAL} задания сегодня. Сделано: {state['completed_today']}",
                },
                status=400,
            )

        streak = state["next_streak"]
        reward = _daily_reward(streak)
        code = _daily_code(user_id, state["today"], streak)
        await db.execute(
            "INSERT INTO miniapp_daily_streak (user_id, streak, last_day) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET streak=excluded.streak, last_day=excluded.last_day",
            (user_id, streak, state["today"]),
        )
        await db.execute(
            "INSERT INTO miniapp_daily_claims "
            "(user_id, day, streak, reward_usd, reward_stars, code) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, state["today"], streak, reward["usd"], reward["stars"], code),
        )
        await db.execute(
            "UPDATE users SET balance = balance + ?, "
            "lifetime_earned = COALESCE(lifetime_earned, 0) + ? WHERE user_id = ?",
            (reward["usd"], reward["usd"], user_id),
        )
        await db.execute(
            "INSERT INTO balance_history (user_id, amount, type, reason) VALUES (?, ?, ?, ?)",
            (
                user_id,
                reward["usd"],
                "daily_chest",
                f"Сундук дня · день {streak} · код {code}",
            ),
        )
        await db.commit()

    balance_row = await get_user(user_id)
    balance = float(balance_row.get("balance", 0) or 0) if balance_row else 0
    return web.json_response(
        {
            "ok": True,
            "streak": streak,
            "reward_usd": reward["usd"],
            "reward_stars": reward["stars"],
            "code": code,
            "balance_usd": round(balance, 6),
            "balance_stars": round(balance / STAR_USD, 2),
            "message": f"День {streak} · +{reward['stars']} ⭐ · код {code}",
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

            try:
                async with db.execute(
                    "SELECT bet_stars, multiplier, payout_usd, payout_stars, status, code, created_at "
                    "FROM miniapp_starfield_rounds WHERE user_id=? ORDER BY id DESC LIMIT 20",
                    (user_id,),
                ) as cursor:
                    for row in await cursor.fetchall():
                        if row["status"] == "lost":
                            amount = -round(float(row["bet_stars"] or 0) * STAR_USD, 6)
                            status = "сгорело"
                        elif row["status"] == "cashed":
                            amount = row["payout_usd"]
                            status = f"выигрыш {row['payout_stars']} ⭐"
                        else:
                            amount = -round(float(row["bet_stars"] or 0) * STAR_USD, 6)
                            status = "в игре"
                        items.append(_history_item(
                            f"starfield_{row['code']}",
                            f"Звёздное поле · x{row['multiplier']}",
                            _history_date(row["created_at"]) + " · код " + row["code"],
                            amount,
                            status,
                            row["created_at"],
                        ))
            except Exception:
                pass

            try:
                async with db.execute(
                    "SELECT streak, reward_usd, reward_stars, code, created_at "
                    "FROM miniapp_daily_claims WHERE user_id=? ORDER BY id DESC LIMIT 10",
                    (user_id,),
                ) as cursor:
                    for row in await cursor.fetchall():
                        items.append(_history_item(
                            f"daily_{row['code']}",
                            f"Сундук дня · день {row['streak']}",
                            _history_date(row["created_at"]) + " · код " + row["code"],
                            row["reward_usd"],
                            f"+{row['reward_stars']} ⭐",
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

    # Для золотой дорожки: друг считается «в деле», когда сделал 5 заданий.
    async with aiosqlite.connect("bot_database.db") as db:
        async with db.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN COALESCE(completed_tasks, 0) >= 5 THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(completed_tasks), 0) "
            "FROM users WHERE referrer_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    gold_friends = int(row[0] or 0) if row else 0
    friends_completed = int(row[1] or 0) if row else 0

    return web.json_response(
        {
            "user": {
                "id": user_id,
                "username": user.get("username") or "Пользователь",
                "completed": completed,
                "level": level,
                "exp": exp,
                "exp_next": 3,
                "gold_friends": gold_friends,
                "friends_completed": friends_completed,
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
    app.router.add_get("/miniapi/starfield", miniapp_starfield)
    app.router.add_post("/miniapi/starfield/start", miniapp_starfield_start)
    app.router.add_post("/miniapi/starfield/open", miniapp_starfield_open)
    app.router.add_post("/miniapi/starfield/cashout", miniapp_starfield_cashout)
    app.router.add_get("/miniapi/daily", miniapp_daily)
    app.router.add_post("/miniapi/daily/claim", miniapp_daily_claim)
