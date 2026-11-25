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

REGION_VOE = "voe"
REGION_IF = "if"
SUPPORTED_REGIONS: tuple[str, ...] = (REGION_VOE, REGION_IF)

REGION_TITLES_UA: dict[str, str] = {
    REGION_VOE: "Вінницька область",
    REGION_IF: "Івано-Франківська область",
}

REGION_SHORT_LABELS_UA: dict[str, str] = {
    REGION_VOE: "Вінницька обл.",
    REGION_IF: "Івано-Франківська обл.",
}


def split_queue_key(raw_queue: str) -> tuple[str, str]:
    """
    Розділяє ключ черги з БД на (region, queue_code).
    Підтримує зворотну сумісність:
      - '3.2' -> ('voe', '3.2')
      - 'voe:3.2' -> ('voe', '3.2')
      - 'if:4.1' -> ('if', '4.1')
    """
    s = (raw_queue or "").strip()
    if ":" in s:
        prefix, code = s.split(":", 1)
        norm_p = prefix.strip().lower()
        if norm_p in ("if", "іф", "ivano-frankivsk", "if_oe"):
            return REGION_IF, code.strip().replace(",", ".")
        return REGION_VOE, code.strip().replace(",", ".")
    return REGION_VOE, s.replace(",", ".")


def make_queue_key(region: str, queue_code: str) -> str:
    """
    Формує канонічний ключ черги для збереження в БД без потреби міграції схеми:
      - Вінницька область ('voe'): '3.2' (100% сумісно з існуючими записами)
      - Івано-Франківська область ('if'): 'if:4.1'
    """
    _, clean_code = split_queue_key(queue_code)
    norm_reg = (region or REGION_VOE).strip().lower()
    if norm_reg in ("if", "іф", "ivano-frankivsk", "if_oe"):
        return f"{REGION_IF}:{clean_code}"
    return clean_code


def is_valid_queue_key(raw_queue: str) -> bool:
    reg, code = split_queue_key(raw_queue)
    return reg in SUPPORTED_REGIONS and code in ALL_QUEUES


def get_region_label(region: str, short: bool = False) -> str:
    if short:
        return REGION_SHORT_LABELS_UA.get(region, REGION_SHORT_LABELS_UA[REGION_VOE])
    return REGION_TITLES_UA.get(region, REGION_TITLES_UA[REGION_VOE])


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
        self.if_api_base_url: str = os.getenv(
            "IF_API_BASE_URL", "https://be-svitlo.oe.if.ua"
        )
        self.if_github_json_url: str = os.getenv(
            "IF_GITHUB_JSON_URL",
            "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Prykarpattiaoblenerho.json",
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

        raw_admin_ids = os.getenv("ADMIN_IDS", "").strip()
        self.admin_ids: set[int] = {
            int(part.strip())
            for part in raw_admin_ids.split(",")
            if part.strip().lstrip("-").isdigit()
        }

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def ensure_dirs(self) -> None:
        db_dir = Path(self.database_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
