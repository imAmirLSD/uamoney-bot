# -*- coding: utf-8 -*-
"""Middleware UA BOT: языковые ограничения, Gramads и TGRASS."""

import random
import time

from aiogram import BaseMiddleware, F
from aiogram.types import (
    Message,
    CallbackQuery,
    Update,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import core
from db import get_setting
from services import show_gramads_ad, get_tgrass_offers


async def _language_is_allowed(user) -> bool:
    """Проверяет язык Telegram, если ограничение включено в настройках."""
    if user is None or user.id in core.ADMIN_IDS:
        return True

    if await get_setting("req_language") != "1":
        return True

    allowed = [
        item.strip().lower()
        for item in (await get_setting("allowed_languages") or "uk,ru").split(",")
        if item.strip()
    ]
    user_lang = (user.language_code or "").lower().split("-")[0]
    return user_lang in allowed


class LanguageMessageMiddleware(BaseMiddleware):
    async def __call__(self, handler, message: Message, data):
        user = message.from_user
        if await _language_is_allowed(user):
            return await handler(message, data)

        await message.answer(
            "❌ Доступ заблокирован.\n\n"
            "Язык вашего Telegram не поддерживается."
        )
        return None


class LanguageCallbackMiddleware(BaseMiddleware):
    async def __call__(self, handler, call: CallbackQuery, data):
        if await _language_is_allowed(call.from_user):
            return await handler(call, data)

        await call.answer("Язык Telegram не поддерживается", show_alert=True)
        return None


class GramadsMiddleware(BaseMiddleware):
    """Показывает рекламу после нажатия кнопок главного меню."""

    MAIN_MENU_BUTTONS = {
        "Задания",
        "Задания №2",
        "Задания №3",
        "Задания №4",
        "Рефералы",
        "Кабинет",
        "Продвижение",
        "Инфо",
    }

    def __init__(self):
        self.ad_cooldown = 300
        self.last_ad_times = {}

    async def __call__(self, handler, event, data):
        result = await handler(event, data)

        # dp.update.middleware получает Update, поэтому извлекаем Message.
        message = event.message if isinstance(event, Update) else event
        if not isinstance(message, Message) or not message.text or not message.from_user:
            return result
        if message.text not in self.MAIN_MENU_BUTTONS:
            return result

        user_id = message.from_user.id
        if user_id in core.ADMIN_IDS:
            return result

        now = time.time()
        if now - self.last_ad_times.get(user_id, 0) < self.ad_cooldown:
            return result

        # 30% вероятность показа, не 100%.
        if random.random() < 0.30:
            try:
                if await show_gramads_ad(user_id):
                    self.last_ad_times[user_id] = now
            except Exception as exc:
                print(f"⚠️ Ошибка в GramadsMiddleware: {exc}")

        return result


class TgrassMiddleware(BaseMiddleware):
    """Ограничивает доступ до выполнения обязательных подписок TGRASS."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            user = getattr(event, "from_user", None)
        if user is None or user.id in core.ADMIN_IDS:
            return await handler(event, data)

        if not await _language_is_allowed(user):
            if isinstance(event, Message):
                await event.answer(
                    "❌ Доступ заблокирован.\n\n"
                    "Язык вашего Telegram не поддерживается."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("Язык Telegram не поддерживается", show_alert=True)
            return None

        # Команда /start должна быть доступна, чтобы пользователь мог начать работу.
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        # Для callback_query пропускаем только проверку подписки и админские действия,
        # чтобы пользователь не мог обойти блокировку через кнопки.
        if isinstance(event, CallbackQuery):
            cb_data = event.data or ""
            # Разрешаем нажатие "Я подписался" и админские настройки TGRASS
            if cb_data in (
                "check_tgrass_sub",
                "admin_tgrass_settings",
                "set_tgrass_key",
            ) or cb_data.startswith(("set_tgrass_key", "admin_tgrass")):
                return await handler(event, data)

            # Для остальных callback'ов проверяем подписки TGRASS
            try:
                offers = await get_tgrass_offers(
                    user.id,
                    user.username,
                    bool(getattr(user, "is_premium", False)),
                )
            except Exception as exc:
                print(f"⚠️ Ошибка запроса TGRASS (callback): {exc}")
                return await handler(event, data)

            # Любой статус, кроме 'ok', считается непройденной проверкой
            if not offers or offers.get("status") != "ok":
                unsubscribed = [
                    o for o in (offers or {}).get("offers", []) if not o.get("subscribed")
                ]
                if unsubscribed:
                    await event.answer(
                        "⚠️ <b>Доступ заблокирован!</b>\n\n"
                        "Чтобы пользоваться ботом, подпишитесь на каналы спонсоров TGRASS.",
                        show_alert=True,
                    )
                    return None

        if isinstance(event, Message):
            try:
                offers = await get_tgrass_offers(
                    user.id,
                    user.username,
                    bool(getattr(user, "is_premium", False)),
                )
            except Exception as exc:
                print(f"⚠️ Ошибка запроса TGRASS: {exc}")
                return await handler(event, data)

            # Блокируем только когда реально есть неподписанные каналы.
            # Статусы 'no_offers' / 'not_ok' без офферов и ошибки API не должны
            # глушить обычные сообщения (иначе кнопки нижнего меню не работают).
            if offers and offers.get("status") != "ok":
                unsubscribed = [
                    offer
                    for offer in (offers.get("offers") or [])
                    if not offer.get("subscribed")
                ]
                if unsubscribed:
                    await self.show_tgrass_tasks(event, offers)
                    return None

        return await handler(event, data)

    async def show_tgrass_tasks(self, event: Message, offers_data: dict):
        offers = offers_data.get("offers") or []
        unsubscribed = [offer for offer in offers if not offer.get("subscribed")]
        if not unsubscribed:
            return

        keyboard = InlineKeyboardBuilder()
        for offer in unsubscribed:
            link = offer.get("link")
            if link:
                keyboard.row(InlineKeyboardButton(text="🌿 Подписаться", url=link))
        keyboard.row(
            InlineKeyboardButton(
                text="✅ Я подписался",
                callback_data="check_tgrass_sub",
            )
        )

        await event.answer(
            "⚠️ <b>Доступ заблокирован!</b>\n\n"
            "Чтобы пользоваться ботом, подпишитесь на каналы спонсоров:",
            reply_markup=keyboard.as_markup(),
            parse_mode="HTML",
        )
