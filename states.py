# -*- coding: utf-8 -*-
"""Все FSM-состояния бота."""
import asyncio
import sqlite3
import json
import time
import random
import os
import re
import aiohttp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core import State, StatesGroup


# Состояния для FSM
class WithdrawalStates(StatesGroup):
    waiting_for_amount = State()

class AdminStates(StatesGroup):
    waiting_payments_channel = State()
    waiting_payments_channell = State()
    waiting_chat_link = State()
    waiting_flyer_key = State()
    waiting_min_withdrawal = State()
    waiting_currency = State()
    waiting_broadcast = State()
    waiting_broadcast_buttons = State()
    waiting_approve_receipt = State()
    waiting_user_id = State()
    waiting_balance_amount = State()
    waiting_ban_reason = State()
    waiting_task_name = State()
    waiting_task_description = State()
    waiting_task_url = State()
    waiting_task_button_text = State()
    waiting_task_max_completions = State()
    waiting_custom_task_price = State()
    waiting_edit_value = State()
    waiting_for_amount = State()
    waiting_crypto_token = State()
    waiting_ref_level1 = State()
    waiting_ref_level2 = State()
    waiting_promotion_price = State()
    waiting_ref_bonus_level1 = State()
    waiting_ref_bonus_level2 = State()
    waiting_ref_tasks_level1 = State()
    waiting_ref_tasks_level2 = State()
    waiting_gramads_token = State()

class TGRASSS(StatesGroup):
    wait_tgrass_key = State()

class AdTopupStates(StatesGroup):
    waiting_for_amount = State()

# ✅ ДОБАВЬТЕ ЭТОТ КЛАСС ДЛЯ ПОПОЛНЕНИЯ РЕЗЕРВА
class ReserveTopupStates(StatesGroup):
    waiting_for_amount = State()

class PromotionStates(StatesGroup):
    waiting_channel_link = State()
    waiting_task_type = State()
    waiting_bot_link = State()
    waiting_count = State()
    waiting_confirm = State()

class EditUserTaskReward(StatesGroup):
    waiting_for_new_value = State()
