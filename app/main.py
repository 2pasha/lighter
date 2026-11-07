from __future__ import annotations

import asyncio
import logging
import sys
from asyncio import StreamReader, StreamWriter
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.handlers import setup_routers
from app.config import settings
from app.database import Database
from app.services.notifier import NotificationEngine
from app.services.voe_client import VOEDataClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("svitlo_bot")


async def _handle_healthcheck(reader: StreamReader, writer: StreamWriter) -> None:
    """Мінімальний HTTP-сервер для перевірок здоров'я (Render / Railway / Fly.io)."""
    try:
        await reader.read(1024)
        body = b'{"status":"ok","service":"svitlo_bot"}'
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(response)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def start_healthcheck_server(port: int) -> Optional[asyncio.AbstractServer]:
    try:
        server = await asyncio.start_server(_handle_healthcheck, "0.0.0.0", port)
        logger.info("Healthcheck HTTP server listening on 0.0.0.0:%d", port)
        return server
    except Exception as exc:
        logger.warning("Could not bind healthcheck server on port %d: %s", port, exc)
        return None


async def main() -> None:
    if not settings.bot_token or settings.bot_token == "your_telegram_bot_token_here":
        logger.error(
            "BOT_TOKEN не заданий! Скопіюйте .env.example у .env та вкажіть ваш токен від @BotFather."
        )
        sys.exit(1)

    db = Database(
        db_path=settings.database_path,
        database_url=settings.database_url,
        supabase_url=settings.supabase_url,
        supabase_key=settings.supabase_key,
    )
    await db.init_db()
    if db.is_supabase_rest:
        logger.info("Using Supabase REST API backend (%s)", settings.supabase_url)
    elif db.is_postgres:
        logger.info("Using Supabase / PostgreSQL database backend")
    else:
        logger.info("SQLite database initialized at %s", settings.database_path)

    voe_client = VOEDataClient()
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    notifier = NotificationEngine(bot=bot, db=db, voe_client=voe_client)
    router = setup_routers(db=db, voe_client=voe_client, notifier=notifier)
    dp.include_router(router)

    # Налаштовуємо планувальник фонових задач (APScheduler)
    scheduler = AsyncIOScheduler(timezone=settings.tz)

    # 1) Періодичне опитування графіків (сайт VOE + GitHub JSON + @pat_voe_bot fallback)
    scheduler.add_job(
        notifier.poll_and_process_schedules,
        trigger="interval",
        minutes=max(1, settings.poll_interval_minutes),
        id="poll_schedules",
        replace_existing=True,
    )

    # 2) Щохвилинна перевірка 15-хвилинних попереджень (до вимкнення та до увімкнення)
    scheduler.add_job(
        notifier.check_15min_warnings,
        trigger="cron",
        second=5,
        id="check_15min_warnings",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started (poll every %d min, 15-min warning check every minute, tz=%s)",
        settings.poll_interval_minutes,
        settings.timezone_name,
    )

    # Первинне завантаження графіків під час запуску
    asyncio.create_task(notifier.poll_and_process_schedules())

    health_server = await start_healthcheck_server(settings.port)

    try:
        logger.info("Starting Telegram bot polling...")
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if health_server:
            health_server.close()
            await health_server.wait_closed()
        await voe_client.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
