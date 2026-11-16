from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import CopyMessage

from app.bot.handlers import execute_broadcast, is_admin
from app.config import Settings, settings
from app.database import Database


def test_settings_admin_ids_parsing(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "111, 222, invalid, -100333")
    parsed = Settings()
    assert parsed.admin_ids == {111, 222, -100333}


def test_is_admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", {12345})
    assert is_admin(12345) is True
    assert is_admin(99999) is False


@pytest.mark.asyncio
async def test_get_all_user_ids_sqlite(tmp_path):
    db_path = tmp_path / "test_broadcast.db"
    db = Database(
        db_path=str(db_path),
        supabase_url="",
        supabase_key="",
        database_url="",
    )
    await db.init_db()

    await db.upsert_user(telegram_id=300, username="u3", first_name="Three")
    await db.upsert_user(telegram_id=100, username="u1", first_name="One")
    await db.upsert_user(telegram_id=200, username="u2", first_name="Two")

    ids = await db.get_all_user_ids()
    assert ids == [100, 200, 300]


@pytest.mark.asyncio
async def test_execute_broadcast_statistics():
    method = CopyMessage(chat_id=1, from_chat_id=1, message_id=1)

    bot = MagicMock()
    bot.copy_message = AsyncMock(
        side_effect=[
            MagicMock(),  # user 1: success
            TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user"),  # user 2: blocked
            TelegramBadRequest(method=method, message="Bad Request: chat not found"),  # user 3: failed
            TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=0),  # user 4: retry
            MagicMock(),  # user 4: retry success
        ]
    )

    progress_calls = []

    async def on_progress(processed: int, total: int) -> None:
        progress_calls.append((processed, total))

    stats = await execute_broadcast(
        bot=bot,
        user_ids=[1, 2, 3, 4],
        from_chat_id=999,
        message_id=42,
        progress_cb=on_progress,
        delay_seconds=0.0,
    )

    assert stats["total"] == 4
    assert stats["sent"] == 2
    assert stats["blocked"] == 1
    assert stats["failed"] == 1
    assert isinstance(stats["duration_sec"], float)
    assert progress_calls == [(1, 4), (2, 4), (3, 4), (4, 4)]
