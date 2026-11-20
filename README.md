# -*- coding: utf-8 -*-
"""Middleware: языковые проверки и показ рекламы."""
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

from db import get_setting

class LanguageMessageMiddleware(BaseMiddleware):
    async def __call__(self, handler, message: Message, data):
        user = message.from_user
        if not user:
            return await handler(message, data)

        if user.id in ADMIN_IDS:
            return await handler(message, data)
        enabled = await get_setting("req_language")
        if enabled != "1":
            return await handler(message, data)

        allowed = [
            x.strip().lower()
            for x in (await get_setting("allowed_languages") or "ru").split(",")
            if x.strip()
        ]

        user_lang = (user.language_code or "").lower().split("-")[0]

        if user_lang not in allowed:
            await message.answer(
                "❌ Доступ заблокирован.\n\n"
                "Язык вашего Telegram не поддерживается."
            )
            return  # ⬅️ КРИТИЧНО

        return await handler(message, data)

class LanguageCallbackMiddleware(BaseMiddleware):
    async def __call__(self, handler, call: CallbackQuery, data):
        user = call.from_user
        if not user:
            return await handler(call, data)

        if user.id in ADMIN_IDS:
            return await handler(call, data)
        enabled = await get_setting("req_language")
        if enabled != "1":
            return await handler(call, data)

        allowed = [
            x.strip().lower()
            for x in (await get_setting("allowed_languages") or "ru").split(",")
            if x.strip()
        ]

        user_lang = (user.language_code or "").lower().split("-")[0]

        if user_lang not in allowed:
            await call.answer(
                "Язык Telegram не поддерживается",
                show_alert=True
            )
            return  # ⬅️ КРИТИЧНО

        return await handler(call, data)


class GramadsMiddleware(BaseMiddleware):
    """Middleware для показа рекламы после взаимодействия с главным меню"""

    def __init__(self):
        self.ad_cooldown = 300  # 5 минут между показами
        self.last_ad_times = {}

    async def __call__(self, handler, event, data):
        # Сначала обрабатываем событие
        result = await handler(event, data)

        try:
            if isinstance(event, Message) and event.text:
                user_id = event.from_user.id

                # Список ВСЕХ кнопок главного меню, при которых хотим показывать рекламу
                main_menu_buttons = [
                    "📝 Задания",
                    "📝 Задания №2", 
                    "📝 Задания №3",
                    "👥 Рефералы",
                    "📱 Кабинет",
                    "📢 Продвижение",
                    "📚 Инфо"
                ]

                # Проверяем, является ли текст кнопкой главного меню
                if event.text in main_menu_buttons:
                    current_time = time.time()
                    last_time = self.last_ad_times.get(user_id, 0)

                    # Проверяем прошло ли достаточно времени
                    if current_time - last_time < self.ad_cooldown:
                        print(f"⏰ Пользователю {user_id} недавно показывали рекламу ({int(current_time - last_time)} сек назад)")
                        return result

                    # Показываем рекламу с вероятностью 30% (можно настроить)
                    import random
                    show_probability = 0.3  # 30% вероятность показа

                    if random.random() < show_probability:
                        print(f"🎲 Показываем рекламу пользователю {user_id} (кнопка: {event.text})")
                        from services import show_gramads_ad
                        success = await show_gramads_ad(user_id)
                        if success:
                            self.last_ad_times[user_id] = current_time
                            print(f"✅ Реклама успешно показана пользователю {user_id}")
                        else:
                            print(f"❌ Не удалось показать рекламу пользователю {user_id}")

        except Exception as e:
            print(f"⚠️ Ошибка в GramadsMiddleware: {e}")

        return result

class TgrassMiddleware(BaseMiddleware):

    async def __call__(self, handler, event, data):

        user = data.get('event_from_user')
        if user is None:
            return await handler(event, data)

        if user.id in ADMIN_IDS:
            return await handler(event, data)

        # 1. СПОЧАТКУ перевіряємо мову
        enabled = await get_setting("req_language")
        if enabled == "1":
            allowed = [
                x.strip().lower()
                for x in (await get_setting("allowed_languages") or "ru").split(",")
                if x.strip()
            ]

            user_lang = (user.language_code or "").lower().split("-")[0]

            if user_lang not in allowed:
                if isinstance(event, Message):
                    await event.answer(
                        "❌ Доступ заблокирован.\n\n"
                        "Язык вашего Telegram не поддерживается."
                    )
                elif isinstance(event, CallbackQuery):
                    await event.answer(
                        "Язык Telegram не поддерживается",
                        show_alert=True
                    )
                return  # Блокуємо

        if isinstance(event, Message) and event.text:

            if event.text.startswith('/start'):

                return await handler(event, data)

            from services import get_tgrass_offers
            offers_data = await get_tgrass_offers(

                user.id,

                user.username,

                user.is_premium or False)

            if offers_data and offers_data.get('status') == 'not_ok':

                await self.show_tgrass_tasks(event, user.id, offers_data)

                return

        return await handler(event, data)
                # Показываем задания TGRASS


    async def show_tgrass_tasks(self, event, user_id: int, offers_data: dict):
        """Показывает задания TGRASS"""
        if not offers_data or 'offers' not in offers_data:
            return

        unsubscribed = [o for o in offers_data['offers'] if not o.get('subscribed')]

        if not unsubscribed:
            return

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

        text = (
            "⚠️ <b>Доступ заблокирован!</b>\n\n"
            "Чтобы пользоваться ботом, вы должны подписаться на каналы спонсоров:"
        )

        if isinstance(event, Message):
            await event.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
