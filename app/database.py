from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional

from app.config import settings
from app.models import DaySchedule

logger = logging.getLogger(__name__)

try:
    import httpx

    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False
    httpx = Any  # type: ignore

try:
    import aiosqlite

    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False
    aiosqlite = Any  # type: ignore

try:
    import asyncpg

    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False
    asyncpg = Any  # type: ignore


@dataclass
class UserProfile:
    telegram_id: int
    username: Optional[str]
    first_name: Optional[str]
    quiet_hours_enabled: bool
    quiet_start: str  # "00:00"
    quiet_end: str    # "07:00"
    notify_tomorrow: bool
    notify_changes: bool
    notify_15m_off: bool
    notify_15m_on: bool
    notify_possible: bool
    created_at: str


@dataclass
class UserLocation:
    id: int
    telegram_id: int
    label: str
    queue: str
    city_name: Optional[str] = None
    city_id: Optional[str] = None
    street_name: Optional[str] = None
    street_id: Optional[str] = None
    house_num: Optional[str] = None
    house_id: Optional[str] = None
    is_active: bool = True

    @property
    def display_address(self) -> str:
        parts = [p for p in (self.city_name, self.street_name, self.house_num) if p]
        return ", ".join(parts) if parts else f"Черга {self.queue}"


class Database:
    """
    Універсальний шар збереження даних з підтримкою:
    1. Supabase REST API (PostgREST) — якщо задані SUPABASE_URL та SUPABASE_KEY (працює без пароля БД).
    2. Пряме PostgreSQL підключення (asyncpg) — якщо заданий DATABASE_URL=postgresql://...
    3. Локальний SQLite (aiosqlite) — автоматичний fallback.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        database_url: Optional[str] = None,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
    ) -> None:
        self.db_path = db_path or settings.database_path
        self.database_url = (
            database_url if database_url is not None else settings.database_url
        ).strip()
        self.supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).strip().rstrip("/")
        self.supabase_key = (
            supabase_key if supabase_key is not None else settings.supabase_key
        ).strip()
        self._pg_pool: Optional[Any] = None
        self._http_client: Optional[httpx.AsyncClient] = None

    @property
    def is_supabase_rest(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgres://", "postgresql://"))

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            headers = {
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "application/json",
            }
            self._http_client = httpx.AsyncClient(
                base_url=f"{self.supabase_url}/rest/v1",
                headers=headers,
                timeout=15.0,
            )
        return self._http_client

    async def _get_pg_pool(self) -> Any:
        if self._pg_pool is None:
            if not HAS_ASYNCPG:
                raise RuntimeError(
                    "Пакет asyncpg не встановлено. Виконайте: pip install asyncpg"
                )
            self._pg_pool = await asyncpg.create_pool(
                dsn=self.database_url,
                min_size=1,
                max_size=5,
                statement_cache_size=0,
            )
        return self._pg_pool

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        if self._pg_pool is not None:
            await self._pg_pool.close()
            self._pg_pool = None

    async def init_db(self) -> None:
        if self.is_supabase_rest:
            client = self._get_http_client()
            resp = await client.get("/users", params={"select": "telegram_id", "limit": "1"})
            resp.raise_for_status()
            logger.info("Connected to Supabase REST API (%s)", self.supabase_url)
            return

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        telegram_id BIGINT PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        quiet_hours_enabled INTEGER NOT NULL DEFAULT 1,
                        quiet_start TEXT NOT NULL DEFAULT '00:00',
                        quiet_end TEXT NOT NULL DEFAULT '07:00',
                        notify_tomorrow INTEGER NOT NULL DEFAULT 1,
                        notify_changes INTEGER NOT NULL DEFAULT 1,
                        notify_15m_off INTEGER NOT NULL DEFAULT 1,
                        notify_15m_on INTEGER NOT NULL DEFAULT 1,
                        notify_possible INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP::TEXT
                    );

                    CREATE TABLE IF NOT EXISTS locations (
                        id SERIAL PRIMARY KEY,
                        telegram_id BIGINT NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
                        label TEXT NOT NULL,
                        queue TEXT NOT NULL,
                        city_name TEXT,
                        city_id TEXT,
                        street_name TEXT,
                        street_id TEXT,
                        house_num TEXT,
                        house_id TEXT,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP::TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_locations_user ON locations(telegram_id);
                    CREATE INDEX IF NOT EXISTS idx_locations_queue ON locations(queue);

                    CREATE TABLE IF NOT EXISTS schedules (
                        queue TEXT NOT NULL,
                        schedule_date TEXT NOT NULL,
                        slots_json TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'website',
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP::TEXT,
                        PRIMARY KEY (queue, schedule_date)
                    );

                    CREATE TABLE IF NOT EXISTS sent_alerts (
                        telegram_id BIGINT NOT NULL,
                        location_id INTEGER NOT NULL,
                        alert_key TEXT NOT NULL,
                        sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP::TEXT,
                        PRIMARY KEY (telegram_id, location_id, alert_key)
                    );

                    CREATE TABLE IF NOT EXISTS address_cache (
                        city_name TEXT NOT NULL,
                        street_name TEXT NOT NULL DEFAULT '',
                        house_num TEXT NOT NULL DEFAULT '',
                        queue TEXT NOT NULL,
                        city_id TEXT,
                        street_id TEXT,
                        house_id TEXT,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP::TEXT,
                        PRIMARY KEY (city_name, street_name, house_num)
                    );
                    """
                )
            logger.info("Connected to PostgreSQL (Supabase) and verified tables.")
            return

        settings.ensure_dirs()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA foreign_keys=ON;")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    quiet_hours_enabled INTEGER NOT NULL DEFAULT 1,
                    quiet_start TEXT NOT NULL DEFAULT '00:00',
                    quiet_end TEXT NOT NULL DEFAULT '07:00',
                    notify_tomorrow INTEGER NOT NULL DEFAULT 1,
                    notify_changes INTEGER NOT NULL DEFAULT 1,
                    notify_15m_off INTEGER NOT NULL DEFAULT 1,
                    notify_15m_on INTEGER NOT NULL DEFAULT 1,
                    notify_possible INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    queue TEXT NOT NULL,
                    city_name TEXT,
                    city_id TEXT,
                    street_name TEXT,
                    street_id TEXT,
                    house_num TEXT,
                    house_id TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_locations_user ON locations(telegram_id);
                CREATE INDEX IF NOT EXISTS idx_locations_queue ON locations(queue);

                CREATE TABLE IF NOT EXISTS schedules (
                    queue TEXT NOT NULL,
                    schedule_date TEXT NOT NULL,
                    slots_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'website',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (queue, schedule_date)
                );

                CREATE TABLE IF NOT EXISTS sent_alerts (
                    telegram_id INTEGER NOT NULL,
                    location_id INTEGER NOT NULL,
                    alert_key TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (telegram_id, location_id, alert_key)
                );

                CREATE TABLE IF NOT EXISTS address_cache (
                    city_name TEXT NOT NULL,
                    street_name TEXT NOT NULL DEFAULT '',
                    house_num TEXT NOT NULL DEFAULT '',
                    queue TEXT NOT NULL,
                    city_id TEXT,
                    street_id TEXT,
                    house_id TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (city_name, street_name, house_num)
                );
                """
            )
            await db.commit()

    # -------------------------------------------------------------------------
    # Users
    # -------------------------------------------------------------------------
    async def upsert_user(
        self,
        telegram_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> UserProfile:
        if self.is_supabase_rest:
            client = self._get_http_client()
            existing = await self.get_user(telegram_id)
            if existing:
                patch_data: dict[str, Any] = {}
                if username is not None and username != existing.username:
                    patch_data["username"] = username
                if first_name is not None and first_name != existing.first_name:
                    patch_data["first_name"] = first_name
                if patch_data:
                    resp = await client.patch(
                        "/users",
                        params={"telegram_id": f"eq.{telegram_id}"},
                        json=patch_data,
                        headers={"Prefer": "return=representation"},
                    )
                    resp.raise_for_status()
                    rows = resp.json()
                    if rows:
                        return self._row_to_user(rows[0])
                return existing

            payload: dict[str, Any] = {
                "telegram_id": telegram_id,
                "username": username,
                "first_name": first_name,
            }
            resp = await client.post(
                "/users",
                params={"on_conflict": "telegram_id"},
                json=payload,
                headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            )
            resp.raise_for_status()
            return self._row_to_user(resp.json()[0])

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO users (telegram_id, username, first_name)
                    VALUES ($1, $2, $3)
                    ON CONFLICT(telegram_id) DO UPDATE SET
                        username = COALESCE(EXCLUDED.username, users.username),
                        first_name = COALESCE(EXCLUDED.first_name, users.first_name)
                    RETURNING *
                    """,
                    telegram_id,
                    username,
                    first_name,
                )
                return self._row_to_user(row)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT INTO users (telegram_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name)
                """,
                (telegram_id, username, first_name),
            )
            await db.commit()
            user = await self._get_user_in_sqlite(db, telegram_id)
            assert user is not None
            return user

    async def get_user(self, telegram_id: int) -> Optional[UserProfile]:
        if self.is_supabase_rest:
            client = self._get_http_client()
            resp = await client.get(
                "/users", params={"telegram_id": f"eq.{telegram_id}"}
            )
            resp.raise_for_status()
            rows = resp.json()
            return self._row_to_user(rows[0]) if rows else None

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM users WHERE telegram_id = $1", telegram_id
                )
                return self._row_to_user(row) if row else None

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await self._get_user_in_sqlite(db, telegram_id)

    async def _get_user_in_sqlite(
        self, db: aiosqlite.Connection, telegram_id: int
    ) -> Optional[UserProfile]:
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_user(row) if row else None

    @staticmethod
    def _row_to_user(row: Mapping[str, Any]) -> UserProfile:
        return UserProfile(
            telegram_id=int(row["telegram_id"]),
            username=row["username"],
            first_name=row["first_name"],
            quiet_hours_enabled=bool(row["quiet_hours_enabled"]),
            quiet_start=str(row["quiet_start"]),
            quiet_end=str(row["quiet_end"]),
            notify_tomorrow=bool(row["notify_tomorrow"]),
            notify_changes=bool(row["notify_changes"]),
            notify_15m_off=bool(row["notify_15m_off"]),
            notify_15m_on=bool(row["notify_15m_on"]),
            notify_possible=bool(row["notify_possible"]),
            created_at=str(row["created_at"]),
        )

    async def toggle_user_setting(
        self, telegram_id: int, field_name: str
    ) -> Optional[UserProfile]:
        allowed_fields = {
            "quiet_hours_enabled",
            "notify_tomorrow",
            "notify_changes",
            "notify_15m_off",
            "notify_15m_on",
            "notify_possible",
        }
        if field_name not in allowed_fields:
            raise ValueError(f"Invalid setting field: {field_name}")

        if self.is_supabase_rest:
            user = await self.get_user(telegram_id)
            if not user:
                return None
            cur_val = bool(getattr(user, field_name))
            new_val = 0 if cur_val else 1
            client = self._get_http_client()
            resp = await client.patch(
                "/users",
                params={"telegram_id": f"eq.{telegram_id}"},
                json={field_name: new_val},
                headers={"Prefer": "return=representation"},
            )
            resp.raise_for_status()
            rows = resp.json()
            return self._row_to_user(rows[0]) if rows else None

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"UPDATE users SET {field_name} = CASE WHEN {field_name} = 1 THEN 0 ELSE 1 END "
                    f"WHERE telegram_id = $1 RETURNING *",
                    telegram_id,
                )
                return self._row_to_user(row) if row else None

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                f"UPDATE users SET {field_name} = CASE WHEN {field_name} = 1 THEN 0 ELSE 1 END WHERE telegram_id = ?",
                (telegram_id,),
            )
            await db.commit()
            return await self._get_user_in_sqlite(db, telegram_id)

    async def update_quiet_hours(
        self, telegram_id: int, start_str: str, end_str: str
    ) -> Optional[UserProfile]:
        if self.is_supabase_rest:
            client = self._get_http_client()
            resp = await client.patch(
                "/users",
                params={"telegram_id": f"eq.{telegram_id}"},
                json={
                    "quiet_start": start_str,
                    "quiet_end": end_str,
                    "quiet_hours_enabled": 1,
                },
                headers={"Prefer": "return=representation"},
            )
            resp.raise_for_status()
            rows = resp.json()
            return self._row_to_user(rows[0]) if rows else None

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE users SET quiet_start = $1, quiet_end = $2, quiet_hours_enabled = 1 "
                    "WHERE telegram_id = $3 RETURNING *",
                    start_str,
                    end_str,
                    telegram_id,
                )
                return self._row_to_user(row) if row else None

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE users SET quiet_start = ?, quiet_end = ?, quiet_hours_enabled = 1 WHERE telegram_id = ?",
                (start_str, end_str, telegram_id),
            )
            await db.commit()
            return await self._get_user_in_sqlite(db, telegram_id)

    # -------------------------------------------------------------------------
    # Locations
    # -------------------------------------------------------------------------
    async def add_location(
        self,
        telegram_id: int,
        label: str,
        queue: str,
        city_name: Optional[str] = None,
        city_id: Optional[str] = None,
        street_name: Optional[str] = None,
        street_id: Optional[str] = None,
        house_num: Optional[str] = None,
        house_id: Optional[str] = None,
    ) -> UserLocation:
        if city_name and queue:
            try:
                await self.cache_address_queue(
                    city_name=city_name,
                    street_name=street_name,
                    house_num=house_num,
                    queue=queue,
                    city_id=city_id,
                    street_id=street_id,
                    house_id=house_id,
                )
            except Exception as exc:
                logger.debug("cache_address_queue skipped: %s", exc)

        if self.is_supabase_rest:
            client = self._get_http_client()
            payload = {
                "telegram_id": telegram_id,
                "label": label,
                "queue": queue,
                "city_name": city_name,
                "city_id": city_id,
                "street_name": street_name,
                "street_id": street_id,
                "house_num": house_num,
                "house_id": house_id,
                "is_active": 1,
            }
            resp = await client.post(
                "/locations",
                json=payload,
                headers={"Prefer": "return=representation"},
            )
            resp.raise_for_status()
            return self._row_to_location(resp.json()[0])

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO locations (
                        telegram_id, label, queue, city_name, city_id,
                        street_name, street_id, house_num, house_id, is_active
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 1)
                    RETURNING *
                    """,
                    telegram_id,
                    label,
                    queue,
                    city_name,
                    city_id,
                    street_name,
                    street_id,
                    house_num,
                    house_id,
                )
                return self._row_to_location(row)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                INSERT INTO locations (
                    telegram_id, label, queue, city_name, city_id,
                    street_name, street_id, house_num, house_id, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    telegram_id,
                    label,
                    queue,
                    city_name,
                    city_id,
                    street_name,
                    street_id,
                    house_num,
                    house_id,
                ),
            )
            loc_id = cursor.lastrowid
            await db.commit()
            return UserLocation(
                id=loc_id or 0,
                telegram_id=telegram_id,
                label=label,
                queue=queue,
                city_name=city_name,
                city_id=city_id,
                street_name=street_name,
                street_id=street_id,
                house_num=house_num,
                house_id=house_id,
                is_active=True,
            )

    async def get_user_locations(self, telegram_id: int) -> list[UserLocation]:
        if self.is_supabase_rest:
            client = self._get_http_client()
            resp = await client.get(
                "/locations",
                params={"telegram_id": f"eq.{telegram_id}", "order": "id.asc"},
            )
            resp.raise_for_status()
            return [self._row_to_location(r) for r in resp.json()]

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM locations WHERE telegram_id = $1 ORDER BY id ASC",
                    telegram_id,
                )
                return [self._row_to_location(r) for r in rows]

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM locations WHERE telegram_id = ? ORDER BY id ASC",
                (telegram_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_location(r) for r in rows]

    async def get_location_by_id(
        self, loc_id: int, telegram_id: int
    ) -> Optional[UserLocation]:
        if self.is_supabase_rest:
            client = self._get_http_client()
            resp = await client.get(
                "/locations",
                params={"id": f"eq.{loc_id}", "telegram_id": f"eq.{telegram_id}"},
            )
            resp.raise_for_status()
            rows = resp.json()
            return self._row_to_location(rows[0]) if rows else None

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM locations WHERE id = $1 AND telegram_id = $2",
                    loc_id,
                    telegram_id,
                )
                return self._row_to_location(row) if row else None

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM locations WHERE id = ? AND telegram_id = ?",
                (loc_id, telegram_id),
            ) as cursor:
                row = await cursor.fetchone()
                return self._row_to_location(row) if row else None

    async def delete_location(self, loc_id: int, telegram_id: int) -> bool:
        if self.is_supabase_rest:
            client = self._get_http_client()
            resp = await client.delete(
                "/locations",
                params={"id": f"eq.{loc_id}", "telegram_id": f"eq.{telegram_id}"},
                headers={"Prefer": "return=representation"},
            )
            resp.raise_for_status()
            return len(resp.json()) > 0

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                res = await conn.execute(
                    "DELETE FROM locations WHERE id = $1 AND telegram_id = $2",
                    loc_id,
                    telegram_id,
                )
                return res != "DELETE 0"

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM locations WHERE id = ? AND telegram_id = ?",
                (loc_id, telegram_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_all_active_subscriptions(
        self,
    ) -> list[tuple[UserProfile, UserLocation]]:
        """Повертає всі активні підписки (користувач + локація) для розсилки сповіщень."""
        if self.is_supabase_rest:
            client = self._get_http_client()
            result: list[tuple[UserProfile, UserLocation]] = []
            page_size = 1000
            offset = 0
            while True:
                resp = await client.get(
                    "/locations",
                    params={
                        "is_active": "eq.1",
                        "select": "*,users(*)",
                        "order": "id.asc",
                        "limit": str(page_size),
                        "offset": str(offset),
                    },
                )
                resp.raise_for_status()
                rows = resp.json()
                for row in rows:
                    u_data = row.get("users")
                    if not u_data:
                        continue
                    if isinstance(u_data, list):
                        u_data = u_data[0]
                    user = self._row_to_user(u_data)
                    loc = self._row_to_location(row)
                    result.append((user, loc))
                if len(rows) < page_size:
                    break
                offset += page_size
            return result

        query = """
            SELECT
                u.telegram_id, u.username, u.first_name,
                u.quiet_hours_enabled, u.quiet_start, u.quiet_end,
                u.notify_tomorrow, u.notify_changes, u.notify_15m_off,
                u.notify_15m_on, u.notify_possible, u.created_at as u_created_at,
                l.id as loc_id, l.label, l.queue, l.city_name, l.city_id,
                l.street_name, l.street_id, l.house_num, l.house_id, l.is_active
            FROM locations l
            JOIN users u ON u.telegram_id = l.telegram_id
            WHERE l.is_active = 1
        """
        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(query)
                return [self._joined_row_to_sub(r) for r in rows]

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [self._joined_row_to_sub(r) for r in rows]

    @staticmethod
    def _joined_row_to_sub(
        r: Mapping[str, Any],
    ) -> tuple[UserProfile, UserLocation]:
        user = UserProfile(
            telegram_id=int(r["telegram_id"]),
            username=r["username"],
            first_name=r["first_name"],
            quiet_hours_enabled=bool(r["quiet_hours_enabled"]),
            quiet_start=str(r["quiet_start"]),
            quiet_end=str(r["quiet_end"]),
            notify_tomorrow=bool(r["notify_tomorrow"]),
            notify_changes=bool(r["notify_changes"]),
            notify_15m_off=bool(r["notify_15m_off"]),
            notify_15m_on=bool(r["notify_15m_on"]),
            notify_possible=bool(r["notify_possible"]),
            created_at=str(r["u_created_at"]),
        )
        loc = UserLocation(
            id=int(r["loc_id"]),
            telegram_id=int(r["telegram_id"]),
            label=str(r["label"]),
            queue=str(r["queue"]),
            city_name=r["city_name"],
            city_id=r["city_id"],
            street_name=r["street_name"],
            street_id=r["street_id"],
            house_num=r["house_num"],
            house_id=r["house_id"],
            is_active=bool(r["is_active"]),
        )
        return user, loc

    async def get_monitored_queues(self) -> list[str]:
        """Список унікальних черг, на які підписаний хоча б один користувач."""
        if self.is_supabase_rest:
            client = self._get_http_client()
            resp = await client.get(
                "/locations",
                params={"is_active": "eq.1", "select": "queue"},
            )
            resp.raise_for_status()
            return sorted({str(r["queue"]) for r in resp.json()})

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT DISTINCT queue FROM locations WHERE is_active = 1"
                )
                return [str(r["queue"]) for r in rows]

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT DISTINCT queue FROM locations WHERE is_active = 1"
            ) as cursor:
                rows = await cursor.fetchall()
                return [r[0] for r in rows]

    @staticmethod
    def _row_to_location(row: Mapping[str, Any]) -> UserLocation:
        return UserLocation(
            id=int(row["id"]),
            telegram_id=int(row["telegram_id"]),
            label=str(row["label"]),
            queue=str(row["queue"]),
            city_name=row["city_name"],
            city_id=row["city_id"],
            street_name=row["street_name"],
            street_id=row["street_id"],
            house_num=row["house_num"],
            house_id=row["house_id"],
            is_active=bool(row["is_active"]),
        )

    # -------------------------------------------------------------------------
    # Schedules
    # -------------------------------------------------------------------------
    async def get_schedule(
        self, queue: str, schedule_date: date
    ) -> Optional[DaySchedule]:
        date_str = schedule_date.isoformat()
        if self.is_supabase_rest:
            client = self._get_http_client()
            resp = await client.get(
                "/schedules",
                params={"queue": f"eq.{queue}", "schedule_date": f"eq.{date_str}"},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return None
            row = rows[0]
            return DaySchedule.from_json(
                queue=str(row["queue"]),
                schedule_date=date.fromisoformat(str(row["schedule_date"])),
                slots_json=str(row["slots_json"]),
                source=str(row["source"]),
            )

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM schedules WHERE queue = $1 AND schedule_date = $2",
                    queue,
                    date_str,
                )
                if not row:
                    return None
                return DaySchedule.from_json(
                    queue=str(row["queue"]),
                    schedule_date=date.fromisoformat(str(row["schedule_date"])),
                    slots_json=str(row["slots_json"]),
                    source=str(row["source"]),
                )

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM schedules WHERE queue = ? AND schedule_date = ?",
                (queue, date_str),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return DaySchedule.from_json(
                    queue=row["queue"],
                    schedule_date=date.fromisoformat(row["schedule_date"]),
                    slots_json=row["slots_json"],
                    source=row["source"],
                )

    async def save_schedule(
        self, schedule: DaySchedule
    ) -> tuple[bool, Optional[DaySchedule]]:
        date_str = schedule.schedule_date.isoformat()
        existing = await self.get_schedule(schedule.queue, schedule.schedule_date)

        if existing and existing.content_hash == schedule.content_hash:
            return False, existing

        if (
            existing
            and existing.possible_outage_minutes > 0
            and schedule.possible_outage_minutes == 0
            and schedule.source == "voe_json"
        ):
            same_outage_mask = all(
                a.is_outage == b.is_outage
                for a, b in zip(existing.slots, schedule.slots)
            )
            if same_outage_mask:
                return False, existing

        if self.is_supabase_rest:
            client = self._get_http_client()
            payload = {
                "queue": schedule.queue,
                "schedule_date": date_str,
                "slots_json": schedule.to_json(),
                "content_hash": schedule.content_hash,
                "source": schedule.source,
                "updated_at": datetime.utcnow().isoformat(),
            }
            resp = await client.post(
                "/schedules",
                params={"on_conflict": "queue,schedule_date"},
                json=payload,
                headers={"Prefer": "resolution=merge-duplicates"},
            )
            resp.raise_for_status()
            return True, existing

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO schedules (queue, schedule_date, slots_json, content_hash, source, updated_at)
                    VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP::TEXT)
                    ON CONFLICT(queue, schedule_date) DO UPDATE SET
                        slots_json = EXCLUDED.slots_json,
                        content_hash = EXCLUDED.content_hash,
                        source = EXCLUDED.source,
                        updated_at = CURRENT_TIMESTAMP::TEXT
                    """,
                    schedule.queue,
                    date_str,
                    schedule.to_json(),
                    schedule.content_hash,
                    schedule.source,
                )
            return True, existing

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO schedules (queue, schedule_date, slots_json, content_hash, source, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(queue, schedule_date) DO UPDATE SET
                    slots_json = excluded.slots_json,
                    content_hash = excluded.content_hash,
                    source = excluded.source,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    schedule.queue,
                    date_str,
                    schedule.to_json(),
                    schedule.content_hash,
                    schedule.source,
                ),
            )
            await db.commit()

        return True, existing

    # -------------------------------------------------------------------------
    # Sent Alerts (Deduplication)
    # -------------------------------------------------------------------------
    async def has_alert_been_sent(
        self, telegram_id: int, location_id: int, alert_key: str
    ) -> bool:
        if self.is_supabase_rest:
            client = self._get_http_client()
            resp = await client.get(
                "/sent_alerts",
                params={
                    "telegram_id": f"eq.{telegram_id}",
                    "location_id": f"eq.{location_id}",
                    "alert_key": f"eq.{alert_key}",
                    "select": "alert_key",
                },
            )
            resp.raise_for_status()
            return len(resp.json()) > 0

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT 1 FROM sent_alerts
                    WHERE telegram_id = $1 AND location_id = $2 AND alert_key = $3
                    """,
                    telegram_id,
                    location_id,
                    alert_key,
                )
                return row is not None

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT 1 FROM sent_alerts
                WHERE telegram_id = ? AND location_id = ? AND alert_key = ?
                """,
                (telegram_id, location_id, alert_key),
            ) as cursor:
                row = await cursor.fetchone()
                return row is not None

    async def mark_alert_sent(
        self, telegram_id: int, location_id: int, alert_key: str
    ) -> None:
        if self.is_supabase_rest:
            client = self._get_http_client()
            payload = {
                "telegram_id": telegram_id,
                "location_id": location_id,
                "alert_key": alert_key,
            }
            resp = await client.post(
                "/sent_alerts",
                params={"on_conflict": "telegram_id,location_id,alert_key"},
                json=payload,
                headers={"Prefer": "resolution=ignore-duplicates"},
            )
            resp.raise_for_status()
            return

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO sent_alerts (telegram_id, location_id, alert_key)
                    VALUES ($1, $2, $3)
                    ON CONFLICT DO NOTHING
                    """,
                    telegram_id,
                    location_id,
                    alert_key,
                )
            return

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO sent_alerts (telegram_id, location_id, alert_key)
                VALUES (?, ?, ?)
                """,
                (telegram_id, location_id, alert_key),
            )
            await db.commit()

    async def get_sent_alerts_for_keys(
        self, alert_keys: list[str]
    ) -> set[tuple[int, int, str]]:
        """Повертає множину (telegram_id, location_id, alert_key) для списку ключів за один запит."""
        if not alert_keys:
            return set()

        unique_keys = list(set(alert_keys))
        if self.is_supabase_rest:
            client = self._get_http_client()
            result: set[tuple[int, int, str]] = set()
            in_expr = "(" + ",".join(f'"{k}"' for k in unique_keys) + ")"
            page_size = 1000
            offset = 0
            while True:
                resp = await client.get(
                    "/sent_alerts",
                    params={
                        "alert_key": f"in.{in_expr}",
                        "select": "telegram_id,location_id,alert_key",
                        "limit": str(page_size),
                        "offset": str(offset),
                    },
                )
                resp.raise_for_status()
                rows = resp.json()
                for r in rows:
                    result.add(
                        (int(r["telegram_id"]), int(r["location_id"]), str(r["alert_key"]))
                    )
                if len(rows) < page_size:
                    break
                offset += page_size
            return result

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT telegram_id, location_id, alert_key
                    FROM sent_alerts
                    WHERE alert_key = ANY($1::text[])
                    """,
                    unique_keys,
                )
                return {
                    (int(r["telegram_id"]), int(r["location_id"]), str(r["alert_key"]))
                    for r in rows
                }

        placeholders = ",".join("?" for _ in unique_keys)
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"SELECT telegram_id, location_id, alert_key FROM sent_alerts WHERE alert_key IN ({placeholders})",
                tuple(unique_keys),
            ) as cursor:
                rows = await cursor.fetchall()
                return {(int(r[0]), int(r[1]), str(r[2])) for r in rows}

    async def mark_alerts_sent_batch(
        self, entries: list[tuple[int, int, str]]
    ) -> None:
        """Пакетний запис відправлених сповіщень за один запит."""
        if not entries:
            return

        if self.is_supabase_rest:
            client = self._get_http_client()
            chunk_size = 500
            for i in range(0, len(entries), chunk_size):
                chunk = entries[i : i + chunk_size]
                payload = [
                    {
                        "telegram_id": tg_id,
                        "location_id": loc_id,
                        "alert_key": a_key,
                    }
                    for tg_id, loc_id, a_key in chunk
                ]
                resp = await client.post(
                    "/sent_alerts",
                    params={"on_conflict": "telegram_id,location_id,alert_key"},
                    json=payload,
                    headers={"Prefer": "resolution=ignore-duplicates"},
                )
                resp.raise_for_status()
            return

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO sent_alerts (telegram_id, location_id, alert_key)
                    VALUES ($1, $2, $3)
                    ON CONFLICT DO NOTHING
                    """,
                    entries,
                )
            return

        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """
                INSERT OR IGNORE INTO sent_alerts (telegram_id, location_id, alert_key)
                VALUES (?, ?, ?)
                """,
                entries,
            )
            await db.commit()

    # -------------------------------------------------------------------------
    # Address -> Queue Cache
    # -------------------------------------------------------------------------
    async def cache_address_queue(
        self,
        city_name: str,
        street_name: Optional[str],
        house_num: Optional[str],
        queue: str,
        city_id: Optional[str] = None,
        street_id: Optional[str] = None,
        house_id: Optional[str] = None,
    ) -> None:
        """Зберігає відому відповідність (місто, вулиця, будинок) -> черга."""
        c_name = (city_name or "").strip()
        s_name = (street_name or "").strip()
        h_num = (house_num or "").strip()
        if not c_name or not queue:
            return

        if self.is_supabase_rest:
            # У режимі Supabase REST покладаємося на існуючу таблицю locations
            # та локальний AddressIndex без вимоги міграції нової таблиці в PostgREST.
            return

        now_str = datetime.now(settings.tz).isoformat(timespec="seconds")
        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO address_cache (
                        city_name, street_name, house_num, queue,
                        city_id, street_id, house_id, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (city_name, street_name, house_num) DO UPDATE SET
                        queue = EXCLUDED.queue,
                        city_id = COALESCE(EXCLUDED.city_id, address_cache.city_id),
                        street_id = COALESCE(EXCLUDED.street_id, address_cache.street_id),
                        house_id = COALESCE(EXCLUDED.house_id, address_cache.house_id),
                        updated_at = EXCLUDED.updated_at
                    """,
                    c_name,
                    s_name,
                    h_num,
                    queue,
                    city_id,
                    street_id,
                    house_id,
                    now_str,
                )
            return

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO address_cache (
                    city_name, street_name, house_num, queue,
                    city_id, street_id, house_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (city_name, street_name, house_num) DO UPDATE SET
                    queue = excluded.queue,
                    city_id = COALESCE(excluded.city_id, address_cache.city_id),
                    street_id = COALESCE(excluded.street_id, address_cache.street_id),
                    house_id = COALESCE(excluded.house_id, address_cache.house_id),
                    updated_at = excluded.updated_at
                """,
                (c_name, s_name, h_num, queue, city_id, street_id, house_id, now_str),
            )
            await db.commit()

    async def lookup_cached_address(
        self,
        city_name: str,
        street_name: Optional[str] = None,
        house_num: Optional[str] = None,
    ) -> Optional[str]:
        """Шукає збережену чергу для адреси в address_cache або серед існуючих локацій."""
        c_name = (city_name or "").strip()
        s_name = (street_name or "").strip()
        h_num = (house_num or "").strip()
        if not c_name:
            return None

        if self.is_supabase_rest:
            try:
                client = self._get_http_client()
                params: dict[str, str] = {
                    "select": "queue",
                    "city_name": f"eq.{c_name}",
                    "limit": "1",
                }
                if s_name:
                    params["street_name"] = f"eq.{s_name}"
                if h_num:
                    params["house_num"] = f"eq.{h_num}"
                resp = await client.get("/locations", params=params)
                if resp.status_code == 200:
                    rows = resp.json()
                    if rows and rows[0].get("queue"):
                        return str(rows[0]["queue"])
            except Exception:
                pass
            return None

        if self.is_postgres:
            pool = await self._get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT queue FROM address_cache
                    WHERE LOWER(city_name) = LOWER($1)
                      AND LOWER(street_name) = LOWER($2)
                      AND LOWER(house_num) = LOWER($3)
                    LIMIT 1
                    """,
                    c_name,
                    s_name,
                    h_num,
                )
                if row and row["queue"]:
                    return str(row["queue"])
            return None

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT queue FROM address_cache
                WHERE LOWER(city_name) = LOWER(?)
                  AND LOWER(street_name) = LOWER(?)
                  AND LOWER(house_num) = LOWER(?)
                LIMIT 1
                """,
                (c_name, s_name, h_num),
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    return str(row[0])
        return None
