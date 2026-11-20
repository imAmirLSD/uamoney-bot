# -*- coding: utf-8 -*-
"""Точка входа: инициализация БД, сервисов и Telegram polling."""
import asyncio
import core
from core import bot, dp
import db
import services
import utils
import handlers  # noqa: F401 — регистрация всех обработчиков
from middlewares import LanguageMessageMiddleware, LanguageCallbackMiddleware, GramadsMiddleware, TgrassMiddleware

dp.message.middleware(LanguageMessageMiddleware())
dp.callback_query.middleware(LanguageCallbackMiddleware())
dp.update.middleware(GramadsMiddleware())
dp.message.outer_middleware(TgrassMiddleware())
dp.callback_query.outer_middleware(TgrassMiddleware())

async def main():
    if not core.BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Создайте .env по примеру .env.example")
    await db.init_db()
    await db.update_db_structure()
    # Доплачиваем старые реферальные бонусы, потерянные из-за прямых SQL-инкрементов
    # completed_tasks в заданиях №3/№4. Повторные выплаты защищены ref_paid_level*.
    await db.catch_up_referral_bonuses()
    await services.load_crypto_token()
    await services.load_gramads_token()
    await services.load_piarflow_key()
    await services.load_pulsar_config()
    await services.load_tgrass_key()
    await services.load_ad_history_from_db()
    services.init_flyer()
    services.check_flyer_methods()

    for task in (
        db.check_completed_tasks(),
        db.check_unsubscribed_penalties(),
        db.cleanup_database(),
        db.auto_unfreeze_balances(),
        core.scheduled_task_checker(),
        services.schedule_periodic_ads(),
        services.piarflow_unsubscribe_watcher(),
        services.pulsar_unsubscribe_watcher(),
    ):
        asyncio.create_task(task)
    asyncio.create_task(services.start_webhook_server())

    print("✅ Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        # aiogram сам закроет сессию bot, но внешние aiohttp-сессии
        # (PiarFlow, вебхук-сервер) нужно закрыть явно.
        await services.close_external_sessions()

if __name__ == "__main__":
    asyncio.run(main())
