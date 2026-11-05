from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

try:
    import httpx
except ImportError:
    httpx = Any  # type: ignore

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = Any  # type: ignore

from app.config import ALL_QUEUES, settings
from app.models import DaySchedule, SlotState
from app.services.image_parser import parse_voe_schedule_image

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
}


@dataclass
class AutocompleteItem:
    id: str
    label: str


@dataclass
class AddressScheduleResult:
    queue: str
    schedules: dict[date, DaySchedule]


class VOEDataClient:
    """
    Багаторівневий клієнт для отримання графіків відключень світла (АТ «Вінницяобленерго»):
    1. Сайт voe.com.ua/disconnection/detailed (пошук за адресою: місто -> вулиця -> будинок, визначення черги та таблиці ГПВ).
    2. GitHub JSON mirror (vn-progr/gpv-voe-vinnytsia) — усі 12 черг (1.1..6.2) на сьогодні та завтра.
    3. Офіційний Telegram-бот @pat_voe_bot через Telethon + парсинг зображення графіка.
    """

    def __init__(self) -> None:
        self.base_url = settings.voe_base_url.rstrip("/")
        self.github_json_url = settings.voe_github_json_url
        self._http_client: Optional[httpx.AsyncClient] = None
        self._github_cache_ts: float = 0.0
        self._github_cache_data: dict[tuple[str, date], DaySchedule] = {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                headers=DEFAULT_HEADERS,
                timeout=20.0,
                follow_redirects=True,
            )
        return self._http_client

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # -------------------------------------------------------------------------
    # 1. Пошук адреси на сайті voe.com.ua/disconnection/detailed
    # -------------------------------------------------------------------------
    async def search_cities(self, query: str) -> list[AutocompleteItem]:
        """Пошук населеного пункту через autocomplete ендпоїнт VOE."""
        url = f"{self.base_url}/disconnection/detailed/autocomplete/read_city"
        return await self._fetch_autocomplete(url, {"q": query.strip()})

    async def search_streets(self, city_id: str, query: str) -> list[AutocompleteItem]:
        """Пошук вулиці у вибраному населеному пункті."""
        url = f"{self.base_url}/disconnection/detailed/autocomplete/read_street/{city_id}"
        return await self._fetch_autocomplete(url, {"q": query.strip()})

    async def search_houses(self, street_id: str, query: str) -> list[AutocompleteItem]:
        """Пошук номера будинку на вибраній вулиці."""
        url = f"{self.base_url}/disconnection/detailed/autocomplete/read_house/{street_id}"
        return await self._fetch_autocomplete(url, {"q": query.strip()})

    async def _fetch_autocomplete(
        self, url: str, params: dict[str, str]
    ) -> list[AutocompleteItem]:
        try:
            client = self._get_client()
            resp = await client.get(url, params=params, timeout=15.0)
            resp.raise_for_status()
            return self._parse_autocomplete_response(resp)
        except Exception as exc:
            logger.warning("Autocomplete request failed (%s): %s", url, exc)
            return []

    @staticmethod
    def _parse_autocomplete_response(resp: httpx.Response) -> list[AutocompleteItem]:
        items: list[AutocompleteItem] = []
        content_type = resp.headers.get("content-type", "")

        # Варіант 1: JSON масив (стандартний Drupal autocomplete [{"value": "...", "label": "..."}])
        if "json" in content_type or resp.text.strip().startswith(("[", "{")):
            try:
                data = resp.json()
                if isinstance(data, list):
                    for entry in data:
                        if isinstance(entry, dict):
                            raw_val = str(entry.get("value") or entry.get("id") or "")
                            raw_lbl = str(entry.get("label") or entry.get("name") or raw_val)
                            # Якщо label містить HTML (наприклад <div data-id="123">Вінниця</div>)
                            if "<" in raw_lbl:
                                soup = BeautifulSoup(raw_lbl, "html.parser")
                                tag = soup.find(attrs={"data-id": True})
                                item_id = str(tag["data-id"]) if tag else raw_val
                                label_txt = soup.get_text(strip=True)
                                items.append(AutocompleteItem(id=item_id, label=label_txt))
                            else:
                                items.append(AutocompleteItem(id=raw_val, label=raw_lbl))
                elif isinstance(data, dict):
                    for k, v in data.items():
                        items.append(AutocompleteItem(id=str(k), label=str(v)))
                if items:
                    return items[:25]
            except Exception:
                pass

        # Варіант 2: HTML фрагмент з елементами data-id
        soup = BeautifulSoup(resp.text, "html.parser")
        for el in soup.find_all(attrs={"data-id": True}):
            item_id = str(el.get("data-id", "")).strip()
            label_txt = el.get_text(strip=True)
            if item_id and label_txt:
                items.append(AutocompleteItem(id=item_id, label=label_txt))

        return items[:25]

    async def fetch_by_address(
        self,
        city_id: str,
        street_id: str,
        house_id: str,
        city_name: str = "",
        street_name: str = "",
        house_num: str = "",
    ) -> Optional[AddressScheduleResult]:
        """
        Виконує GET + POST запит до форми https://www.voe.com.ua/disconnection/detailed,
        витягує номер черги (наприклад '3.2') та погодинну таблицю відключень.
        """
        form_url = f"{self.base_url}/disconnection/detailed"
        try:
            client = self._get_client()
            get_resp = await client.get(form_url, timeout=20.0)
            get_resp.raise_for_status()
            soup = BeautifulSoup(get_resp.text, "html.parser")

            form_build_id_el = soup.find("input", {"name": "form_build_id"})
            form_id_el = soup.find("input", {"name": "form_id"})
            form_build_id = form_build_id_el["value"] if form_build_id_el else ""
            form_id = (
                form_id_el["value"]
                if form_id_el
                else "disconnection_detailed_search_form"
            )

            payload = {
                "search_type": "0",
                "city": city_name,
                "city_id": city_id,
                "street": street_name,
                "street_id": street_id,
                "house": house_num,
                "house_id": house_id,
                "form_build_id": form_build_id,
                "form_id": form_id,
                "op": "Пошук",
            }
            post_resp = await client.post(form_url, data=payload, timeout=20.0)
            post_resp.raise_for_status()

            return self.parse_detailed_html(post_resp.text)
        except Exception as exc:
            logger.warning("Failed to fetch schedule by address from VOE website: %s", exc)
            return None

    @classmethod
    def parse_detailed_html(cls, html_text: str) -> Optional[AddressScheduleResult]:
        """
        Парсить HTML-відповідь сторінки /disconnection/detailed (у т.ч. Drupal AJAX JSON обгортку, якщо є).
        Визначає чергу (напр. 3.2) та розклад за датами.
        """
        # Якщо відповідь прийшла як Drupal AJAX JSON масив команд [{"command": "insert", "data": "..."}]
        if html_text.strip().startswith("["):
            try:
                import json

                cmds = json.loads(html_text)
                html_parts = [
                    c.get("data", "")
                    for c in cmds
                    if isinstance(c, dict) and isinstance(c.get("data"), str)
                ]
                if html_parts:
                    html_text = "\n".join(html_parts)
            except Exception:
                pass

        soup = BeautifulSoup(html_text, "html.parser")
        full_text = soup.get_text(" ", strip=True)

        # Шукаємо чергу, наприклад "3.2 черга" або "Черга 3.2" або "Підчерга: 3.2"
        queue_match = re.search(
            r"(?:черга|підчерга|група)\s*[:№-]?\s*([1-6][.,][1-2])|([1-6][.,][1-2])\s*(?:черга|підчерга)",
            full_text,
            re.IGNORECASE,
        )
        queue = "1.1"
        if queue_match:
            raw_q = queue_match.group(1) or queue_match.group(2)
            queue = raw_q.replace(",", ".")

        schedules: dict[date, DaySchedule] = {}
        today = datetime.now(settings.tz).date()

        # Шукаємо таблиці або рядки з розкладом
        tables = soup.find_all("table")
        for table in tables:
            for tr in table.find_all("tr"):
                cells = tr.find_all(["td", "th"])
                if len(cells) < 24:
                    continue

                # Перевіряємо, чи є в першій клітинці дата (напр. 04.11.2026 або 04.11)
                row_date = today
                first_txt = cells[0].get_text(strip=True)
                date_match = re.search(r"(\d{2})\.(\d{2})(?:\.(\d{4}))?", first_txt)
                hour_cells = cells
                if date_match:
                    day_val = int(date_match.group(1))
                    month_val = int(date_match.group(2))
                    year_val = int(date_match.group(3)) if date_match.group(3) else today.year
                    try:
                        row_date = date(year_val, month_val, day_val)
                    except ValueError:
                        row_date = today
                    hour_cells = cells[1:]

                if len(hour_cells) >= 48:
                    slots = [cls._classify_html_cell(c) for c in hour_cells[:48]]
                    schedules[row_date] = DaySchedule(
                        queue=queue,
                        schedule_date=row_date,
                        slots=slots,
                        source="voe_website",
                    )
                elif len(hour_cells) >= 24:
                    hourly = [cls._classify_html_cell(c) for c in hour_cells[:24]]
                    schedules[row_date] = DaySchedule.from_hourly(
                        queue=queue,
                        schedule_date=row_date,
                        hourly_states=hourly,
                        source="voe_website",
                    )

        if not queue_match and not schedules:
            return None

        return AddressScheduleResult(queue=queue, schedules=schedules)

    @staticmethod
    def _classify_html_cell(cell: Any) -> SlotState:
        classes = " ".join(cell.get("class", [])).lower()
        style = str(cell.get("style", "")).lower()
        title = str(cell.get("title", "")).lower()
        text = cell.get_text(strip=True).lower()
        combined = f"{classes} {style} {title} {text}"

        if any(
            kw in combined
            for kw in (
                "possible",
                "maybe",
                "заплановане",
                "можлив",
                "yellow",
                "#ffc000",
                "rgb(255, 192",
            )
        ):
            return SlotState.POSSIBLE_OFF
        if any(
            kw in combined
            for kw in (
                "has_disconnection",
                "disconnection",
                "outage",
                "off",
                "відключення",
                "немає",
                "blue",
                "#01b0f1",
                "red",
                "active",
                "blackout",
            )
        ) and "no_disconnection" not in combined:
            return SlotState.OFF
        return SlotState.ON

    # -------------------------------------------------------------------------
    # 2. GitHub JSON Mirror (vn-progr/gpv-voe-vinnytsia) для всіх 12 черг
    # -------------------------------------------------------------------------
    async def fetch_all_queues_from_github(
        self, ttl_seconds: float = 30.0
    ) -> dict[tuple[str, date], DaySchedule]:
        """
        Завантажує актуальний JSON графіків усіх черг (1.1 .. 6.2) з репозиторію
        vn-progr/gpv-voe-vinnytsia (data/Vinnytsiaoblenerho.json).
        Підтримує як формат Yasno/SvitloYeah (`fact.data.<timestamp>.GPV1.1`),
        так і прямий словниковий формат черг.
        """
        now_mono = time.monotonic()
        if (
            self._github_cache_data
            and (now_mono - self._github_cache_ts) < ttl_seconds
        ):
            return dict(self._github_cache_data)

        try:
            client = self._get_client()
            resp = await client.get(self.github_json_url, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
            parsed = self.parse_github_json(data)
            if parsed:
                self._github_cache_ts = now_mono
                self._github_cache_data = dict(parsed)
            return parsed
        except Exception as exc:
            logger.warning("GitHub JSON mirror fetch failed: %s", exc)
            return {}

    @classmethod
    def parse_github_json(
        cls, data: dict[str, Any]
    ) -> dict[tuple[str, date], DaySchedule]:
        result: dict[tuple[str, date], DaySchedule] = {}

        today = datetime.now(settings.tz).date()

        # Формат 1: Yasno / ha-svitlo-yeah / outage-data-ua структура:
        # {"fact": {"data": {"1762207200": {"GPV1.1": {"1": "yes", "2": "no", ...}}}}}
        fact_data = data.get("fact", {}).get("data")
        if isinstance(fact_data, dict):
            for ts_str, queues_dict in fact_data.items():
                if not isinstance(queues_dict, dict):
                    continue
                try:
                    ts_int = int(ts_str)
                    sched_date = datetime.fromtimestamp(ts_int, tz=settings.tz).date()
                except Exception:
                    try:
                        sched_date = date.fromisoformat(str(ts_str)[:10])
                    except Exception:
                        continue

                day_schedules: list[DaySchedule] = []
                for raw_q_key, hours_map in queues_dict.items():
                    if not isinstance(hours_map, dict):
                        continue
                    q_norm = raw_q_key.upper().replace("GPV", "").strip()
                    if q_norm not in ALL_QUEUES:
                        continue
                    slots = cls._parse_24h_map_to_48_slots(hours_map)
                    day_schedules.append(
                        DaySchedule(
                            queue=q_norm,
                            schedule_date=sched_date,
                            slots=slots,
                            source="voe_json",
                        )
                    )

                # Якщо для майбутньої дати (завтра) всі черги мають 0 хв відключень,
                # це порожній нічний шаблон-заглушка після 00:00 (графік ще не опубліковано)
                if sched_date > today and all(
                    s.total_outage_minutes == 0 for s in day_schedules
                ):
                    continue

                for s in day_schedules:
                    result[(s.queue, s.schedule_date)] = s

        # Формат 2: Прямий словник за датами або чергами
        if not result and isinstance(data, dict):
            for key, val in data.items():
                q_norm = str(key).upper().replace("GPV", "").strip()
                if q_norm in ALL_QUEUES and isinstance(val, dict):
                    for date_str, hours_map in val.items():
                        try:
                            sched_date = date.fromisoformat(str(date_str)[:10])
                        except Exception:
                            continue
                        if isinstance(hours_map, dict):
                            slots = cls._parse_24h_map_to_48_slots(hours_map)
                            result[(q_norm, sched_date)] = DaySchedule(
                                queue=q_norm,
                                schedule_date=sched_date,
                                slots=slots,
                                source="voe_json",
                            )

        return result

    @staticmethod
    def _parse_24h_map_to_48_slots(hours_map: dict[str, Any]) -> list[SlotState]:
        """
        Конвертує словник годин ("1".."24" або "00-01".."23-24") у 48 півгодинних слотів.
        Підтримує значення:
        - "yes" / "on" / True -> світло є (ON, ON)
        - "no" / "off" / False -> відключення (OFF, OFF)
        - "maybe" / "possible" -> можливе відключення (POSSIBLE_OFF, POSSIBLE_OFF)
        - "first" -> перші 30 хв OFF, другі 30 хв ON
        - "second" -> перші 30 хв ON, другі 30 хв OFF
        - "mfirst" -> перші 30 хв POSSIBLE_OFF, другі 30 хв ON
        - "msecond" -> перші 30 хв ON, другі 30 хв POSSIBLE_OFF
        """
        slots: list[SlotState] = []
        for h in range(24):
            val = (
                hours_map.get(str(h + 1))
                or hours_map.get(f"{h:02d}-{h+1:02d}")
                or hours_map.get(f"{h:02d}:00")
                or hours_map.get(str(h))
                or "yes"
            )
            val_str = str(val).strip().lower()
            if val_str in ("no", "off", "false", "0", "outage", "blue", "red"):
                slots.extend([SlotState.OFF, SlotState.OFF])
            elif val_str in ("maybe", "possible", "yellow", "planned", "prob"):
                slots.extend([SlotState.POSSIBLE_OFF, SlotState.POSSIBLE_OFF])
            elif val_str == "first":
                slots.extend([SlotState.OFF, SlotState.ON])
            elif val_str == "second":
                slots.extend([SlotState.ON, SlotState.OFF])
            elif val_str == "mfirst":
                slots.extend([SlotState.POSSIBLE_OFF, SlotState.ON])
            elif val_str == "msecond":
                slots.extend([SlotState.ON, SlotState.POSSIBLE_OFF])
            else:
                slots.extend([SlotState.ON, SlotState.ON])
        return slots

    # -------------------------------------------------------------------------
    # 3. Fallback: Офіційний Telegram-бот @pat_voe_bot через Telethon + Image Parser
    # -------------------------------------------------------------------------
    async def fetch_from_official_bot(
        self, queue: str, target_date: date
    ) -> Optional[DaySchedule]:
        """
        Опціональний fallback: якщо TELETHON_ENABLED=true, звертається до @pat_voe_bot,
        отримує картинку графіка для вказаної черги/адреси і розпізнає її через parse_voe_schedule_image.
        """
        if not settings.telethon_enabled:
            return None
        if not (
            settings.telethon_api_id
            and settings.telethon_api_hash
            and settings.telethon_session_string
        ):
            logger.warning("Telethon enabled but credentials are missing in .env")
            return None

        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            async with TelegramClient(
                StringSession(settings.telethon_session_string),
                settings.telethon_api_id,
                settings.telethon_api_hash,
            ) as client:
                bot_entity = await client.get_entity(settings.official_voe_bot_username)
                # Шукаємо останні повідомлення з фото в діалозі з ботом або запитуємо графік
                messages = await client.get_messages(bot_entity, limit=10)
                date_str = target_date.strftime("%d.%m.%Y")
                for msg in messages:
                    if msg.photo:
                        img_bytes = await client.download_media(msg.photo, file=bytes)
                        if img_bytes:
                            parsed = parse_voe_schedule_image(
                                img_bytes, queue=queue, schedule_date=target_date
                            )
                            return parsed
        except Exception as exc:
            logger.warning("Telethon fallback error for queue %s: %s", queue, exc)
        return None
