from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.config import ALL_QUEUES, settings
from app.database import Database, UserLocation
from app.models import DaySchedule, SlotState
from app.services.formatter import format_minutes_human
from app.services.voe_client import VOEDataClient

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "scriptable"
LOADER_TEMPLATE_PATH = ASSETS_DIR / "Svitlo.template.js"
OTA_SCRIPT_PATH = ASSETS_DIR / "Svitlo.ota.js"


def generate_widget_token(user_id: int) -> str:
    """Генерує HMAC-SHA256 токен для безпечного доступу віджета до даних користувача."""
    msg = f"svitlo_widget:{user_id}".encode("utf-8")
    secret = settings.widget_secret.encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:24]


def verify_widget_token(user_id: int, token: str) -> bool:
    if not token:
        return False
    expected = generate_widget_token(user_id)
    return hmac.compare_digest(expected, token.strip())


def format_minutes_en(total_minutes: int) -> str:
    hours, mins = divmod(max(0, total_minutes), 60)
    parts: list[str] = []
    if hours > 0:
        parts.append(f"{hours}h")
    if mins > 0:
        parts.append(f"{mins}m")
    return " ".join(parts) if parts else "0m"


def compute_widget_transition(
    today_sched: DaySchedule,
    tomorrow_sched: Optional[DaySchedule],
    cur_minute: int,
    include_possible: bool = True,
) -> tuple[str, str, Optional[str], Optional[int]]:
    """
    Обчислює час до наступної зміни стану (timeUntilNextChange En/Uk),
    nextStatusTime (HH:MM) та точну кількість хвилин до переходу (diff_minutes).
    Враховує перехід через північ, якщо відомий графік на завтра.
    """
    cur_slot_idx = max(0, min(47, cur_minute // 30))

    def is_outage_slot(st: SlotState) -> bool:
        if st == SlotState.OFF:
            return True
        if st == SlotState.POSSIBLE_OFF and include_possible:
            return True
        return False

    cur_is_outage = is_outage_slot(today_sched.slots[cur_slot_idx])

    # Шукаємо першу зміну стану сьогодні
    for idx in range(cur_slot_idx + 1, 48):
        if is_outage_slot(today_sched.slots[idx]) != cur_is_outage:
            target_min = idx * 30
            diff = max(1, target_min - cur_minute)
            h, m = divmod(target_min, 60)
            return (
                format_minutes_en(diff),
                format_minutes_human(diff),
                f"{h:02d}:{m:02d}",
                diff,
            )

    # Якщо до кінця сьогоднішньої доби змін немає, перевіряємо завтрашній графік
    if tomorrow_sched is not None:
        for idx in range(0, 48):
            if is_outage_slot(tomorrow_sched.slots[idx]) != cur_is_outage:
                target_min = idx * 30
                diff = max(1, (1440 - cur_minute) + target_min)
                h, m = divmod(target_min, 60)
                return (
                    format_minutes_en(diff),
                    format_minutes_human(diff),
                    f"{h:02d}:{m:02d}",
                    diff,
                )

    # Якщо на завтра графіка ще немає або весь день такий самий
    diff_to_midnight = max(1, 1440 - cur_minute)
    if cur_is_outage:
        return (
            format_minutes_en(diff_to_midnight),
            format_minutes_human(diff_to_midnight),
            "24:00",
            diff_to_midnight,
        )
    return "0m", "без відкл.", None, None


def build_widget_payload(
    today_sched: DaySchedule,
    tomorrow_sched: Optional[DaySchedule],
    now: datetime,
    location_label: Optional[str] = None,
    include_possible: bool = True,
) -> dict[str, Any]:
    """Формує JSON-відповідь для GET /api/widget/data."""
    cur_min = now.hour * 60 + now.minute
    cur_slot_state = today_sched.state_at_minute(cur_min)

    if cur_slot_state == SlotState.OFF or (
        cur_slot_state == SlotState.POSSIBLE_OFF and include_possible
    ):
        status = "offline"
    else:
        status = "online"

    time_en, time_uk, next_time_str, diff_minutes = compute_widget_transition(
        today_sched=today_sched,
        tomorrow_sched=tomorrow_sched,
        cur_minute=cur_min,
        include_possible=include_possible,
    )

    now_utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    next_change_at_iso = ""
    if diff_minutes is not None:
        # Точний момент наступної зміни (від початку поточної хвилини + diff_minutes)
        now_minute_floor = now.replace(second=0, microsecond=0)
        target_dt = now_minute_floor + timedelta(minutes=diff_minutes)
        next_change_at_iso = target_dt.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    return {
        "status": status,
        "slotState": cur_slot_state.value,
        "statusLabelUk": cur_slot_state.label_ua,
        "currentGroup": f"Group {today_sched.queue}",
        "currentGroupUk": f"Черга {today_sched.queue}",
        "queue": today_sched.queue,
        "locationLabel": location_label or f"Черга {today_sched.queue}",
        "timeUntilNextChange": time_en,
        "timeUntilNextChangeUk": time_uk,
        "nextStatusTime": next_time_str or "",
        "nextChangeAt": next_change_at_iso,
        "totalOutageMinutes": today_sched.total_outage_minutes,
        "totalOutageTimeUk": format_minutes_human(today_sched.total_outage_minutes),
        "updatedAtLocal": now.strftime("%H:%M"),
        "lastUpdated": now_utc,
    }


async def resolve_widget_data(
    db: Database,
    voe_client: VOEDataClient,
    user_id: Optional[int] = None,
    location_id: Optional[int] = None,
    param: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Визначає потрібну локацію/чергу користувача, за потреби оновлює графік
    та повертає готовий JSON-словник для Scriptable-віджета.
    """
    if now is None:
        now = datetime.now(settings.tz)

    today = now.date()
    tomorrow = today + timedelta(days=1)

    include_possible = True
    selected_loc: Optional[UserLocation] = None
    target_queue: Optional[str] = None
    location_label: Optional[str] = None

    # Перевіряємо налаштування користувача та його збережені локації
    locations: list[UserLocation] = []
    if user_id:
        user = await db.get_user(user_id)
        if user:
            include_possible = user.notify_possible
        locations = await db.get_user_locations(user_id)

    # 1. Якщо передано параметр у віджеті (наприклад "3.2" або "Робота")
    clean_param = (param or "").strip()
    if clean_param:
        q_match = re.search(r"([1-6][.,][1-2])", clean_param)
        if q_match:
            target_queue = q_match.group(1).replace(",", ".")
            for loc in locations:
                if loc.queue == target_queue:
                    selected_loc = loc
                    break
        else:
            for loc in locations:
                if clean_param.lower() in loc.label.lower():
                    selected_loc = loc
                    target_queue = loc.queue
                    break

    # 2. Якщо передано конкретний locationId
    if selected_loc is None and not target_queue and location_id and locations:
        for loc in locations:
            if loc.id == location_id:
                selected_loc = loc
                target_queue = loc.queue
                break

    # 3. За замовчуванням беремо першу збережену локацію користувача
    if selected_loc is None and not target_queue and locations:
        selected_loc = locations[0]
        target_queue = selected_loc.queue

    if selected_loc:
        location_label = selected_loc.label
        target_queue = selected_loc.queue

    if not target_queue or target_queue not in ALL_QUEUES:
        target_queue = "3.2"
        if not location_label:
            location_label = f"⚡ Черга {target_queue}"

    # Завантажуємо графік з БД або з GitHub-дзеркала
    today_sched = await db.get_schedule(target_queue, today)
    tomorrow_sched = await db.get_schedule(target_queue, tomorrow)

    if today_sched is None:
        all_github = await voe_client.fetch_all_queues_from_github(ttl_seconds=30.0)
        for (_, d), s in all_github.items():
            if d > today and s.total_outage_minutes == 0:
                continue
            await db.save_schedule(s)
        today_sched = all_github.get((target_queue, today)) or await db.get_schedule(
            target_queue, today
        )
        tomorrow_sched = all_github.get(
            (target_queue, tomorrow)
        ) or await db.get_schedule(target_queue, tomorrow)

    if today_sched is None:
        today_sched = DaySchedule(queue=target_queue, schedule_date=today)

    return build_widget_payload(
        today_sched=today_sched,
        tomorrow_sched=tomorrow_sched,
        now=now,
        location_label=location_label,
        include_possible=include_possible,
    )


def generate_user_loader_js(
    user_id: int,
    base_url: Optional[str] = None,
    default_location_id: Optional[int] = None,
) -> bytes:
    """
    Читає шаблон Svitlo.template.js, підставляє ID користувача, підписаний токен
    та базовий URL сервера, і повертає байти готового файлу Svitlo.js.
    """
    template_str = LOADER_TEMPLATE_PATH.read_text(encoding="utf-8")
    resolved_url = (base_url or settings.public_base_url).strip().rstrip("/")
    token = generate_widget_token(user_id)
    loc_id_str = str(default_location_id) if default_location_id else ""

    js_code = (
        template_str.replace("__BASE_URL__", resolved_url)
        .replace("__USER_ID__", str(user_id))
        .replace("__TOKEN__", token)
        .replace("__DEFAULT_LOCATION_ID__", loc_id_str)
    )
    return js_code.encode("utf-8")


def get_ota_ui_script_bytes() -> bytes:
    """Повертає актуальний код UI-скрипта для OTA-оновлення."""
    return OTA_SCRIPT_PATH.read_bytes()
