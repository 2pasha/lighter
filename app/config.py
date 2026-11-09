from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ALL_QUEUES: list[str] = [
    "1.1", "1.2",
    "2.1", "2.2",
    "3.1", "3.2",
    "4.1", "4.2",
    "5.1", "5.2",
    "6.1", "6.2",
]


class Settings:
    def __init__(self) -> None:
        self.bot_token: str = os.getenv("BOT_TOKEN", "")
        self.supabase_url: str = os.getenv("SUPABASE_URL", "").strip()
        self.supabase_key: str = os.getenv("SUPABASE_KEY", "").strip()
        self.database_url: str = os.getenv("DATABASE_URL", "").strip()
        self.database_path: str = os.getenv("DATABASE_PATH", "data/svitlo_bot.db")
        self.timezone_name: str = os.getenv("TIMEZONE", "Europe/Kyiv")
        self.poll_interval_minutes: int = int(os.getenv("POLL_INTERVAL_MINUTES", "5"))

        self.voe_base_url: str = os.getenv("VOE_BASE_URL", "https://www.voe.com.ua")
        self.voe_github_json_url: str = os.getenv(
            "VOE_GITHUB_JSON_URL",
            "https://raw.githubusercontent.com/vn-progr/gpv-voe-vinnytsia/main/data/Vinnytsiaoblenerho.json",
        )

        self.telethon_enabled: bool = os.getenv("TELETHON_ENABLED", "false").lower() in ("1", "true", "yes")
        raw_api_id = os.getenv("TELETHON_API_ID", "").strip()
        self.telethon_api_id: Optional[int] = int(raw_api_id) if raw_api_id.isdigit() else None
        self.telethon_api_hash: Optional[str] = os.getenv("TELETHON_API_HASH") or None
        self.telethon_session_string: Optional[str] = os.getenv("TELETHON_SESSION_STRING") or None
        self.official_voe_bot_username: str = os.getenv("OFFICIAL_VOE_BOT_USERNAME", "pat_voe_bot")

        self.port: int = int(os.getenv("PORT", "8080"))
        raw_pub_url = (
            os.getenv("PUBLIC_BASE_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or (
                f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}"
                if os.getenv("RAILWAY_PUBLIC_DOMAIN")
                else ""
            )
            or (
                f"https://{os.getenv('FLY_APP_NAME')}.fly.dev"
                if os.getenv("FLY_APP_NAME")
                else ""
            )
            or "https://svitlo-bot-voe.onrender.com"
        )
        self.public_base_url: str = raw_pub_url.strip().rstrip("/")
        self.widget_secret: str = (
            os.getenv("WIDGET_SECRET") or self.bot_token or "svitlo_widget_default_secret"
        ).strip()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def ensure_dirs(self) -> None:
        db_dir = Path(self.database_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
