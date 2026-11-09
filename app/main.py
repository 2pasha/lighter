from __future__ import annotations

import asyncio
import json
import logging
import sys
from asyncio import StreamReader, StreamWriter
from typing import Optional
from urllib.parse import parse_qs, urlparse

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.handlers import setup_routers
from app.config import settings
from app.database import Database
from app.services.notifier import NotificationEngine
from app.services.voe_client import VOEDataClient
from app.services.widget_service import (
    get_ota_ui_script_bytes,
    resolve_widget_data,
    verify_widget_token,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("svitlo_bot")


def _write_http_response(
    writer: StreamWriter,
    status_line: str,
    body: bytes,
    content_type: str = "application/json; charset=utf-8",
) -> None:
    headers = (
        f"HTTP/1.1 {status_line}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    writer.write(headers + body)


async def _handle_http_request(
    reader: StreamReader,
    writer: StreamWriter,
    db: Optional[Database] = None,
    voe_client: Optional[VOEDataClient] = None,
) -> None:
    """HTTP-сервер для перевірок здоров'я (Render / Railway / Fly.io) та Scriptable iOS-віджета."""
    try:
        raw_req = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        req_text = raw_req.decode("utf-8", errors="replace")
        first_line = req_text.split("\r\n", 1)[0]
        parts = first_line.split(" ")
        raw_target = parts[1] if len(parts) >= 2 else "/"
        parsed = urlparse(raw_target)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path == "/api/widget/script":
            js_bytes = get_ota_ui_script_bytes()
            _write_http_response(
                writer,
                "200 OK",
                js_bytes,
                content_type="application/javascript; charset=utf-8",
            )
            await writer.drain()
            return

        if path == "/api/widget/data" and db is not None and voe_client is not None:
            raw_uid = (qs.get("userId") or qs.get("user_id") or [""])[0].strip()
            raw_token = (qs.get("token") or [""])[0].strip()
            raw_loc_id = (qs.get("locationId") or qs.get("locId") or [""])[0].strip()
            raw_param = (qs.get("param") or qs.get("queue") or [""])[0].strip()

            user_id = int(raw_uid) if raw_uid.isdigit() else None
            loc_id = int(raw_loc_id) if raw_loc_id.isdigit() else None

            # Якщо передано токен — перевіряємо його валідність
            if user_id and raw_token and not verify_widget_token(user_id, raw_token):
                err_body = json.dumps({"error": "Invalid widget token"}).encode("utf-8")
                _write_http_response(writer, "403 Forbidden", err_body)
                await writer.drain()
                return

            payload = await resolve_widget_data(
                db=db,
                voe_client=voe_client,
                user_id=user_id,
                location_id=loc_id,
                param=raw_param,
            )
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            _write_http_response(writer, "200 OK", body)
            await writer.drain()
            return

        body = b'{"status":"ok","service":"svitlo_bot"}'
        _write_http_response(writer, "200 OK", body)
        await writer.drain()
    except Exception as exc:
        logger.debug("HTTP request handling error: %s", exc)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start_healthcheck_server(
    port: int,
    db: Optional[Database] = None,
    voe_client: Optional[VOEDataClient] = None,
) -> Optional[asyncio.AbstractServer]:
    try:
        server = await asyncio.start_server(
            lambda r, w: _handle_http_request(r, w, db=db, voe_client=voe_client),
            "0.0.0.0",
            port,
        )
        logger.info("HTTP & Widget API server listening on 0.0.0.0:%d", port)
        return server
    except Exception as exc:
        logger.warning("Could not bind HTTP server on port %d: %s", port, exc)
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

    health_server = await start_healthcheck_server(
        settings.port, db=db, voe_client=voe_client
    )

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
