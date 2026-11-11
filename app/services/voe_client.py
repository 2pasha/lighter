from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
from app.services.address_index import AddressIndex, download_and_build_voe_index
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
    queue: Optional[str] = None


@dataclass
class AddressScheduleResult:
    queue: str
    schedules: dict[date, DaySchedule]


class VOEDataClient:
    """
    Багаторівневий клієнт для отримання графіків відключень світла (АТ «Вінницяобленерго»):
    1. Локальний індекс офіційного довідника ГПВ АТ «Вінницяобленерго» (data/voe_addresses.json)
       + сайт voe.com.ua/disconnection/detailed (пошук за адресою: місто -> вулиця -> будинок).
    2. GitHub JSON mirror (vn-progr/gpv-voe-vinnytsia) — усі 12 черг (1.1..6.2) на сьогодні та завтра.
    3. Офіційний Telegram-бот @pat_voe_bot через Telethon + парсинг зображення графіка.
    """

    def __init__(self) -> None:
        self.base_url = settings.voe_base_url.rstrip("/")
        self.github_json_url = settings.voe_github_json_url
        self._http_client: Optional[httpx.AsyncClient] = None
        self._github_cache_ts: float = 0.0
        self._github_cache_data: dict[tuple[str, date], DaySchedule] = {}
        self.address_index = AddressIndex()
        self._voe_live_blocked_until: float = 0.0

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

    async def refresh_address_index_from_pdf(self) -> int:
        """Оновлює локальний довідник адрес із офіційного PDF ГПВ на сайті voe.com.ua."""
        data = await download_and_build_voe_index(self.address_index.index_path)
        self.address_index.load()
        return len(data.get("cities", []))

    def get_popular_cities(self, limit: int = 10) -> list[AutocompleteItem]:
        """Повертає список популярних населених пунктів для швидкого вибору."""
        items: list[AutocompleteItem] = []
        for c in self.address_index.get_popular_cities(limit=limit):
            q_list = c.get("queues", [])
            q_single = q_list[0] if len(q_list) == 1 and not c.get("has_streets") else None
            items.append(AutocompleteItem(id=c["id"], label=c["name"], queue=q_single))
        return items

    def search_address_freeform(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Швидкий пошук адреси одним рядком (напр. 'Вінниця Соборна 24' або 'Келецька 61')."""
        return self.address_index.parse_and_search_freeform(query, limit=limit)

    # -------------------------------------------------------------------------
    # 1. Пошук адреси (локальний індекс ГПВ + voe.com.ua/disconnection/detailed)
    # -------------------------------------------------------------------------
    async def search_cities(self, query: str) -> list[AutocompleteItem]:
        """Пошук населеного пункту через локальний індекс ГПВ та autocomplete VOE."""
        local_matches = self.address_index.search_cities(query.strip(), limit=15)
        local_items = [
            AutocompleteItem(
                id=c["id"],
                label=c["name"],
                queue=c["queues"][0] if len(c.get("queues", [])) == 1 and not c.get("has_streets") else None,
            )
            for c in local_matches
        ]
        if local_items:
            return local_items

        if time.monotonic() < self._voe_live_blocked_until:
            return []

        url = f"{self.base_url}/disconnection/detailed/autocomplete/read_city"
        return await self._fetch_autocomplete(url, {"q": query.strip()})

    async def search_streets(self, city_id: str, query: str) -> list[AutocompleteItem]:
        """Пошук вулиці у вибраному населеному пункті."""
        if str(city_id).startswith("c_"):
            local_streets = self.address_index.search_streets(city_id, query.strip(), limit=15)
            return [
                AutocompleteItem(
                    id=st["id"],
                    label=st["name"],
                    queue=st["queues"][0] if len(st.get("queues", [])) == 1 else None,
                )
                for st in local_streets
            ]

        if time.monotonic() >= self._voe_live_blocked_until:
            url = f"{self.base_url}/disconnection/detailed/autocomplete/read_street/{city_id}"
            live_items = await self._fetch_autocomplete(url, {"q": query.strip()})
            if live_items:
                return live_items

        return []

    async def search_houses(self, street_id: str, query: str) -> list[AutocompleteItem]:
        """Пошук номера будинку на вибраній вулиці."""
        if str(street_id).startswith("c_"):
            local_houses = self.address_index.search_houses(street_id, query.strip())
            return [
                AutocompleteItem(
                    id=h["id"],
                    label=h["name"],
                    queue=h.get("queue"),
                )
                for h in local_houses
            ]

        if time.monotonic() >= self._voe_live_blocked_until:
            url = f"{self.base_url}/disconnection/detailed/autocomplete/read_house/{street_id}"
            live_items = await self._fetch_autocomplete(url, {"q": query.strip()})
            if live_items:
                return live_items

        return []

    async def _fetch_autocomplete(
        self, url: str, params: dict[str, str]
    ) -> list[AutocompleteItem]:
        try:
            client = self._get_client()
            resp = await client.get(url, params=params, timeout=4.0)
            if resp.status_code in (403, 404) or "disconnection.voe.com.ua" in str(resp.url):
                self._voe_live_blocked_until = time.monotonic() + 600.0
                return []
            resp.raise_for_status()
            if "Just a moment..." in resp.text or "cf-browser-verification" in resp.text:
                self._voe_live_blocked_until = time.monotonic() + 600.0
                return []
            return self._parse_autocomplete_response(resp)
        except Exception as exc:
            self._voe_live_blocked_until = time.monotonic() + 300.0
            logger.debug("Autocomplete request fallback (%s): %s", url, exc)
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
                            # Якщо label містить HTML (наприклад <div class="change_api_autocomplit" data-id="123">Вінниця</div>)
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
        Визначає чергу ГПВ та розклад за адресою:
        1. Перевіряє локальний індекс ГПВ (якщо передані локальні ID або назви адреси).
        2. Якщо це числові ID сайту VOE і сайт не заблокований Cloudflare — виконує запит до /disconnection/detailed.
        3. Підтягує актуальний розклад для знайденої черги з GitHub JSON mirror.
        """
        resolved_queue: Optional[str] = None

        # 1. Якщо house_id вже містить код черги з локального індексу (формат c_0_s_10_h_24_1.1)
        m_q = re.search(r"_h_[^_]+_([1-6]\.[12])$", str(house_id))
        if m_q:
            resolved_queue = m_q.group(1)

        # 2. Якщо street_id з локального індексу
        if not resolved_queue and str(street_id).startswith("c_"):
            h_matches = self.address_index.search_houses(street_id, house_num or "")
            if len(h_matches) == 1:
                resolved_queue = h_matches[0]["queue"]
            elif h_matches and h_matches[0].get("score", 0) >= 3:
                resolved_queue = h_matches[0]["queue"]

        # 3. Якщо city_id з локального індексу і це населений пункт без вулиць з однією чергою
        if not resolved_queue and str(city_id).startswith("c_") and not street_id:
            c_obj = self.address_index.get_city(city_id)
            if c_obj and len(c_obj.get("queues", [])) == 1:
                resolved_queue = c_obj["queues"][0]

        # 4. Спроба живого запиту до сайту VOE (для числових ID, якщо немає Cloudflare блокування)
        if not resolved_queue and time.monotonic() >= self._voe_live_blocked_until and str(city_id).isdigit():
            form_url = f"{self.base_url}/disconnection/detailed"
            try:
                client = self._get_client()
                get_resp = await client.get(form_url, timeout=6.0)
                if "disconnection.voe.com.ua" in str(get_resp.url) or "Just a moment..." in get_resp.text:
                    self._voe_live_blocked_until = time.monotonic() + 600.0
                else:
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
                        "search": "Показати",
                        "op": "Пошук",
                    }
                    post_resp = await client.post(form_url, data=payload, timeout=8.0)
                    post_resp.raise_for_status()
                    live_res = self.parse_detailed_html(post_resp.text)
                    if live_res:
                        return live_res
            except Exception as exc:
                self._voe_live_blocked_until = time.monotonic() + 300.0
                logger.debug("Live VOE address lookup fallback: %s", exc)

        # 5. Текстовий пошук у локальному індексі ГПВ за (city_name, street_name, house_num)
        if not resolved_queue and (city_name or street_name):
            query_str = ", ".join(p for p in (city_name, street_name, house_num) if p)
            free_matches = self.address_index.parse_and_search_freeform(query_str, limit=3)
            if len(free_matches) == 1 or (free_matches and free_matches[0].get("score", 0) >= 2):
                resolved_queue = free_matches[0]["queue"]

        if not resolved_queue:
            return None

        # Завантажуємо актуальні графіки для знайденої черги з GitHub JSON mirror
        schedules: dict[date, DaySchedule] = {}
        gh_all = await self.fetch_all_queues_from_github()
        for (q_code, s_date), sched in gh_all.items():
            if q_code == resolved_queue:
                schedules[s_date] = sched

        return AddressScheduleResult(queue=resolved_queue, schedules=schedules)

    @classmethod
    def parse_detailed_html(cls, html_text: str) -> Optional[AddressScheduleResult]:
        """
        Парсить HTML-відповідь сторінки /disconnection/detailed (у т.ч. Drupal AJAX JSON обгортку
        та div.disconnection-detailed-table-container).
        Визначає чергу (напр. 3.2) та розклад за датами.
        """
        if not html_text or "Just a moment..." in html_text or "cf-browser-verification" in html_text:
            return None

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

        # Формат 1 (реальна структура Drupal VOE): div.disconnection-detailed-table-container
        container = soup.find("div", class_="disconnection-detailed-table-container") or soup.find(
            "div", class_="disconnection-detailed-table"
        )
        if container:
            cells = [
                div
                for div in container.find_all("div", class_="disconnection-detailed-table-cell")
                if "legend" not in (div.get("class") or []) and "head" not in (div.get("class") or [])
            ]
            idx = 0
            while idx < len(cells):
                cell = cells[idx]
                c_classes = cell.get("class") or []
                if "day_col" in c_classes:
                    day_txt = cell.get_text(" ", strip=True)
                    dm = re.search(r"(\d{2})\.(\d{2})(?:\.(\d{4}))?", day_txt)
                    row_date = today
                    if dm:
                        try:
                            d_val, m_val = int(dm.group(1)), int(dm.group(2))
                            y_val = int(dm.group(3)) if dm.group(3) else today.year
                            row_date = date(y_val, m_val, d_val)
                        except ValueError:
                            row_date = today
                    hour_divs = cells[idx + 1 : idx + 25]
                    if len(hour_divs) == 24:
                        slots_48: list[SlotState] = []
                        for h_div in hour_divs:
                            s1, s2 = cls._classify_voe_div_hour_cell(h_div)
                            slots_48.extend([s1, s2])
                        schedules[row_date] = DaySchedule(
                            queue=queue,
                            schedule_date=row_date,
                            slots=slots_48,
                            source="voe_website",
                        )
                    idx += 25
                else:
                    idx += 1

        # Формат 2: Класичні HTML <table>
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
    def _classify_voe_div_hour_cell(cell: Any) -> tuple[SlotState, SlotState]:
        """
        Класифікує одну годинну комірку div.disconnection-detailed-table-cell
        у два 30-хвилинних слоти (перша та друга половина години).
        Підтримує класи has_disconnection, full_hour, confirm_1, confirm_0 та div.fill зі змінними --start/--size.
        """
        classes = [c.lower() for c in (cell.get("class") or [])]
        if "has_disconnection" not in classes:
            return (SlotState.ON, SlotState.ON)

        fill_div = cell.find("div", class_="fill")
        fill_classes = [c.lower() for c in (fill_div.get("class") or [])] if fill_div else []
        all_classes = set(classes + fill_classes)

        outage_state = SlotState.POSSIBLE_OFF if "confirm_0" in all_classes else SlotState.OFF

        if "full_hour" in all_classes:
            return (outage_state, outage_state)

        if fill_div:
            style = str(fill_div.get("style") or "")
            start_m = re.search(r"--start\s*:\s*(\d+)", style)
            size_m = re.search(r"--size\s*:\s*(\d+)", style)
            start_val = int(start_m.group(1)) if start_m else 0
            size_val = int(size_m.group(1)) if size_m else 60
            if start_val == 0 and size_val <= 30:
                return (outage_state, SlotState.ON)
            if start_val >= 30:
                return (SlotState.ON, outage_state)

        if "first_half" in all_classes:
            return (outage_state, SlotState.ON)
        if "second_half" in all_classes:
            return (SlotState.ON, outage_state)

        return (outage_state, outage_state)

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
    # 2. GitHub JSON Mirror (vn-progr/gpv-voe-vinnytsia) + Прямий парсер bezsvitla.com.ua
    # -------------------------------------------------------------------------
    async def fetch_all_queues_from_github(
        self, ttl_seconds: float = 30.0
    ) -> dict[tuple[str, date], DaySchedule]:
        """
        Завантажує актуальний графік усіх черг (1.1 .. 6.2) з репозиторію
        vn-progr/gpv-voe-vinnytsia (data/Vinnytsiaoblenerho.json) ТА напряму з
        bezsvitla.com.ua (із перевіркою часу оновлення кожної підчерги та
        узгодженням парних підчерг N.1/N.2, якщо одна з них зависла у кеші).
        """
        now_mono = time.monotonic()
        if (
            self._github_cache_data
            and (now_mono - self._github_cache_ts) < ttl_seconds
        ):
            return dict(self._github_cache_data)

        async def _fetch_github() -> dict[tuple[str, date], DaySchedule]:
            try:
                client = self._get_client()
                resp = await client.get(self.github_json_url, timeout=15.0)
                resp.raise_for_status()
                data = resp.json()
                return self.parse_github_json(data)
            except Exception as exc:
                logger.warning("GitHub JSON mirror fetch failed: %s", exc)
                return {}

        async def _fetch_bezsvitla() -> dict[tuple[str, date], DaySchedule]:
            try:
                return await self.fetch_all_queues_from_bezsvitla()
            except Exception as exc:
                logger.warning("Direct bezsvitla.com.ua fetch failed: %s", exc)
                return {}

        github_parsed, bezsvitla_parsed = await asyncio.gather(
            _fetch_github(), _fetch_bezsvitla()
        )

        merged: dict[tuple[str, date], DaySchedule] = dict(github_parsed)
        # Прямий скрейпер bezsvitla.com.ua має свіжіші дані (без затримки GitHub Actions у 10+ хв)
        # та містить узгодження завислих підчерг за timestamp Оновлено
        for key, sched in bezsvitla_parsed.items():
            merged[key] = sched

        if merged:
            self._github_cache_ts = now_mono
            self._github_cache_data = dict(merged)
        return merged

    async def fetch_all_queues_from_bezsvitla(
        self,
    ) -> dict[tuple[str, date], DaySchedule]:
        """
        Напряму опитує сторінки черг https://bezsvitla.com.ua/vinnytska-oblast/cherha-{slug}
        для всіх 12 підчерг (1.1 .. 6.2), витягує точні інтервали та індивідуальний час
        оновлення кожної підчерги (`dateModified` / `Оновлено`), і усуває розсинхрон
        між парними підчергами (наприклад, коли 3.1 оновилась сьогодні вранці, а 3.2
        залишилась із вчорашнім штампом часу).
        """
        client = self._get_client()
        today = datetime.now(settings.tz).date()

        async def _fetch_one(
            q: str,
        ) -> tuple[
            str,
            Optional[DaySchedule],
            Optional[datetime],
            Optional[DaySchedule],
        ]:
            slug = q.replace(".", "-")
            url = f"https://bezsvitla.com.ua/vinnytska-oblast/cherha-{slug}"
            try:
                resp = await client.get(url, timeout=12.0)
                if resp.status_code != 200:
                    return q, None, None, None
                today_sched, updated_dt, tomorrow_sched = (
                    self.parse_bezsvitla_queue_html(resp.text, q, today)
                )
                return q, today_sched, updated_dt, tomorrow_sched
            except Exception as exc:
                logger.debug("bezsvitla fetch failed for queue %s: %s", q, exc)
                return q, None, None, None

        tasks = [_fetch_one(q) for q in ALL_QUEUES]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        schedules: dict[tuple[str, date], DaySchedule] = {}
        updated_timestamps: dict[str, datetime] = {}

        for q, today_sched, updated_dt, tomorrow_sched in results:
            if today_sched is not None:
                schedules[(q, today_sched.schedule_date)] = today_sched
            if updated_dt is not None:
                updated_timestamps[q] = updated_dt
            if (
                tomorrow_sched is not None
                and tomorrow_sched.total_outage_minutes > 0
            ):
                schedules[(q, tomorrow_sched.schedule_date)] = tomorrow_sched

        self._reconcile_stale_sibling_queues(
            schedules, updated_timestamps, today
        )
        return schedules

    @classmethod
    def parse_bezsvitla_queue_html(
        cls, html_text: str, default_queue: str, default_date: date
    ) -> tuple[Optional[DaySchedule], Optional[datetime], Optional[DaySchedule]]:
        """
        Парсить HTML сторінки черги bezsvitla.com.ua.
        Повертає (today_schedule, updated_at_datetime, tomorrow_schedule).
        """
        if not html_text:
            return None, None, None

        updated_dt: Optional[datetime] = None

        # 1. Витягуємо час оновлення з <time datetime="..."> або JSON-LD "dateModified"
        iso_match = re.search(
            r'bz-schedule-hero__stamp--updated[\s\S]{0,400}?datetime=["\']([^"\']+)["\']',
            html_text,
            re.IGNORECASE,
        )
        if not iso_match:
            iso_match = re.search(
                r'"dateModified"\s*:\s*"([^"]+)"', html_text, re.IGNORECASE
            )
        if iso_match:
            try:
                updated_dt = datetime.fromisoformat(iso_match.group(1).strip())
            except Exception:
                updated_dt = None

        if updated_dt is None:
            upd_text_match = re.search(
                r'bz-schedule-card__updated[^>]*>\s*(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})\s*<',
                html_text,
                re.IGNORECASE,
            )
            if upd_text_match:
                try:
                    naive_dt = datetime.strptime(
                        upd_text_match.group(1).strip(), "%d.%m.%Y %H:%M"
                    )
                    updated_dt = naive_dt.replace(tzinfo=settings.tz)
                except Exception:
                    updated_dt = None

        # 2. Витягуємо data-schedule-date, data-schedule-intervals та data-schedule-tomorrow
        sched_date = default_date
        date_attr_match = re.search(
            r'data-schedule-date=["\'](\d{4}-\d{2}-\d{2})["\']', html_text
        )
        if date_attr_match:
            try:
                sched_date = date.fromisoformat(date_attr_match.group(1))
            except Exception:
                sched_date = default_date

        today_schedule: Optional[DaySchedule] = None
        intervals_match = re.search(
            r'data-schedule-intervals=["\']([^"\']+)["\']', html_text
        )
        if intervals_match:
            raw_json = html.unescape(intervals_match.group(1))
            slots = cls._parse_bezsvitla_intervals_json(raw_json)
            if slots:
                today_schedule = DaySchedule(
                    queue=default_queue,
                    schedule_date=sched_date,
                    slots=slots,
                    source="voe_json",
                )

        tomorrow_schedule: Optional[DaySchedule] = None
        tomorrow_match = re.search(
            r'data-schedule-tomorrow=["\']([^"\']+)["\']', html_text
        )
        if tomorrow_match:
            raw_tom_json = html.unescape(tomorrow_match.group(1))
            tom_slots = cls._parse_bezsvitla_intervals_json(raw_tom_json)
            if tom_slots and any(s.is_outage for s in tom_slots):
                tomorrow_schedule = DaySchedule(
                    queue=default_queue,
                    schedule_date=sched_date + timedelta(days=1),
                    slots=tom_slots,
                    source="voe_json",
                )

        # Fallback: парсинг карток .bz-schedule-slot, якщо атрибут data-schedule-intervals відсутній
        if today_schedule is None and BeautifulSoup is not Any:
            soup = BeautifulSoup(html_text, "html.parser")
            slot_Els = soup.select(".bz-schedule-slot")
            if slot_Els:
                slots = [SlotState.ON] * 48
                found_any = False
                for el in slot_Els:
                    classes = " ".join(el.get("class", [])).lower()
                    time_el = el.select_one(".bz-schedule-slot__time")
                    if not time_el:
                        continue
                    t_text = time_el.get_text(" ", strip=True)
                    m = re.search(
                        r"(\d{1,2}):(\d{2})\s*[–—\-]\s*(\d{1,2}):(\d{2})",
                        t_text,
                    )
                    if not m:
                        continue
                    found_any = True
                    sh, sm, eh, em = (
                        int(m.group(1)),
                        int(m.group(2)),
                        int(m.group(3)),
                        int(m.group(4)),
                    )
                    start_idx = max(0, min(48, sh * 2 + (1 if sm >= 30 else 0)))
                    end_idx = (
                        48
                        if (eh == 24 or (eh == 0 and em == 0 and sh > 0))
                        else max(0, min(48, eh * 2 + (1 if em >= 30 else 0)))
                    )
                    if "possible" in classes or "maybe" in classes:
                        st = SlotState.POSSIBLE_OFF
                    elif "off" in classes:
                        st = SlotState.OFF
                    else:
                        st = SlotState.ON
                    for idx in range(start_idx, end_idx):
                        slots[idx] = st
                if found_any:
                    today_schedule = DaySchedule(
                        queue=default_queue,
                        schedule_date=sched_date,
                        slots=slots,
                        source="voe_json",
                    )

        return today_schedule, updated_dt, tomorrow_schedule

    @staticmethod
    def _parse_bezsvitla_intervals_json(
        raw_json: str,
    ) -> Optional[list[SlotState]]:
        try:
            items = json.loads(raw_json)
        except Exception:
            return None
        if not isinstance(items, list) or not items:
            return None

        slots = [SlotState.ON] * 48
        for item in items:
            if not isinstance(item, dict):
                continue
            from_str = str(item.get("from", "")).strip()
            to_str = str(item.get("to", "")).strip()
            state_str = str(item.get("state", "ON")).strip().upper()
            m1 = re.match(r"^(\d{1,2}):(\d{2})$", from_str)
            m2 = re.match(r"^(\d{1,2}):(\d{2})$", to_str)
            if not (m1 and m2):
                continue
            sh, sm = int(m1.group(1)), int(m1.group(2))
            eh, em = int(m2.group(1)), int(m2.group(2))
            start_idx = max(0, min(48, sh * 2 + (1 if sm >= 30 else 0)))
            end_idx = (
                48
                if (eh == 24 or (eh == 0 and em == 0 and sh > 0))
                else max(0, min(48, eh * 2 + (1 if em >= 30 else 0)))
            )
            if state_str in ("POSSIBLE", "MAYBE", "POSSIBLE_OFF", "YELLOW"):
                st = SlotState.POSSIBLE_OFF
            elif state_str in ("OFF", "NO", "OUTAGE"):
                st = SlotState.OFF
            else:
                st = SlotState.ON
            for idx in range(start_idx, end_idx):
                slots[idx] = st
        return slots

    @staticmethod
    def _reconcile_stale_sibling_queues(
        schedules: dict[tuple[str, date], DaySchedule],
        updated_timestamps: dict[str, datetime],
        target_date: date,
    ) -> None:
        """
        Усуває затримку оновлення підчерги на bezsvitla.com.ua:
        Якщо в парі підчерг (наприклад, 3.1 та 3.2) одна підчерга (3.1) оновилась
        сьогодні (`updated_at.date() >= target_date`), а інша (3.2) досі має вчорашній
        timestamp (`updated_at.date() < target_date`), і всі слоти відключень свіжої
        підчерги є підмножиною слотів застарілої підчерги (тобто частину відключень
        було скасовано за командою НЕК «Укренерго», наприклад, 10:00–12:00),
        застосовуємо актуальний розклад свіжої підчерги до застарілої.
        """
        for major in range(1, 7):
            q1 = f"{major}.1"
            q2 = f"{major}.2"
            s1 = schedules.get((q1, target_date))
            s2 = schedules.get((q2, target_date))
            t1 = updated_timestamps.get(q1)
            t2 = updated_timestamps.get(q2)
            if not (s1 and s2 and t1 and t2):
                continue

            d1 = t1.astimezone(settings.tz).date() if t1.tzinfo else t1.date()
            d2 = t2.astimezone(settings.tz).date() if t2.tzinfo else t2.date()

            fresh_q, fresh_s, stale_q, stale_s = None, None, None, None
            if d1 >= target_date and d2 < target_date:
                fresh_q, fresh_s, stale_q, stale_s = q1, s1, q2, s2
            elif d2 >= target_date and d1 < target_date:
                fresh_q, fresh_s, stale_q, stale_s = q2, s2, q1, s1

            if fresh_s is None or stale_s is None or stale_q is None:
                continue

            # Перевіряємо, що всі відключення свіжої підчерги входять у застарілу підчергу,
            # а застаріла підчерга має додаткові (вже скасовані) слоти відключень
            fresh_outages = {
                idx for idx, st in enumerate(fresh_s.slots) if st.is_outage
            }
            stale_outages = {
                idx for idx, st in enumerate(stale_s.slots) if st.is_outage
            }
            if fresh_outages and fresh_outages.issubset(stale_outages) and fresh_outages != stale_outages:
                logger.info(
                    "Reconciling stale sub-queue %s (updated %s) with fresh sibling %s (updated %s) for %s",
                    stale_q,
                    updated_timestamps.get(stale_q),
                    fresh_q,
                    updated_timestamps.get(fresh_q),
                    target_date,
                )
                schedules[(stale_q, target_date)] = DaySchedule(
                    queue=stale_q,
                    schedule_date=target_date,
                    slots=list(fresh_s.slots),
                    source=fresh_s.source,
                )

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
                messages = await client.get_messages(bot_entity, limit=15)
                date_str = target_date.strftime("%d.%m.%Y")
                q_norm = queue.strip()
                for msg in messages:
                    if msg.photo:
                        caption = (getattr(msg, "message", "") or "").strip()
                        if caption:
                            if q_norm not in caption and f"{q_norm} черга" not in caption:
                                continue
                            if date_str not in caption:
                                continue
                        img_bytes = await client.download_media(msg.photo, file=bytes)
                        if img_bytes:
                            parsed = parse_voe_schedule_image(
                                img_bytes, queue=queue, schedule_date=target_date
                            )
                            return parsed
        except Exception as exc:
            logger.warning("Telethon fallback error for queue %s: %s", queue, exc)
        return None
