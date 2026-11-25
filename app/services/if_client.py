from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

try:
    import httpx
except ImportError:
    httpx = Any  # type: ignore

from app.config import ALL_QUEUES, REGION_IF, make_queue_key, settings

from app.models import DaySchedule, SlotState

logger = logging.getLogger(__name__)

IF_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8",
    "Origin": "https://svitlo.oe.if.ua",
    "Referer": "https://svitlo.oe.if.ua/",
}

# Популярні міста Івано-Франківської області (Коломия перша для швидкого доступу)
IF_POPULAR_CITIES: list[str] = [
    "м. Коломия",
    "м. Івано-Франківськ",
    "м. Калуш",
    "м. Надвірна",
    "м. Долина",
    "м. Яремче",
    "м. Бурштин",
    "м. Снятин",
    "м. Городенка",
    "м. Косів",
]

# Розширений перелік відомих населених пунктів області (включно з громадами Коломийського району)
IF_KNOWN_SETTLEMENTS: list[str] = [
    *IF_POPULAR_CITIES,
    "м. Болехів",
    "м. Галич",
    "м. Рогатин",
    "м. Тисмениця",
    "м. Тлумач",
    "смт. Печеніжин",
    "смт. Отинія",
    "смт. Гвіздець",
    "смт. Заболотів",
    "смт. Кути",
    "смт. Верховина",
    "смт. Ворохта",
    "смт. Делятин",
    "смт. Ланчин",
    "смт. Богородчани",
    "смт. Обертин",
    "смт. Чернелиця",
    "смт. Перегінське",
    "смт. Рожнятів",
    "смт. Брошнів-Осада",
    "смт. Вигода",
    "с. Шепарівці",
    "с. Нижній Вербіж",
    "с. Верхній Вербіж",
    "с. Сопів",
    "с. Воскресинці",
    "с. Королівка",
    "с. Корнич",
    "с. П'ядики",
    "с. Іванівці",
    "с. Раківчик",
    "с. Товмачик",
    "с. Матеївці",
    "с. Підгайчики",
    "с. Ценява",
    "с. Княждвір",
    "с. Мишин",
    "с. Спас",
    "с. Ковалівка",
    "с. Кийданці",
    "с. Крихівці",
    "с. Вовчинець",
    "с. Угорники",
    "с. Микитинці",
    "с. Хриплин",
    "с. Чукалівка",
    "с. Драгомирчани",
    "с. Ямниця",
    "с. Паляниця",
    "с. Татарів",
    "с. Микуличин",
]

STREET_FIRST_NAMES = {
    "івана", "василя", "степана", "тараса", "богдана", "в'ячеслава", "вячеслава",
    "михайла", "лесі", "андрія", "євгена", "романа", "ольги", "данила", "петра",
    "павла", "юрія", "миколи", "олександра", "григорія", "марка", "симона",
    "гетьмана", "князя", "княгині", "митрополита", "академіка", "генерала",
    "полковника", "патріарха", "влад.", "ім.", "імені",
}


def strip_if_city_prefix(city: str) -> str:
    s = (city or "").strip()
    s = re.sub(r"\([^)]*\)", "", s).strip()
    s = re.sub(
        r"^(?:місто|м\.|смт\.?|селище|с-ще\.?|село|с\.|х\.)\s*",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", s).strip(" ,;.")


def format_if_city_display(city: str) -> str:
    raw = (city or "").strip(" ,;.")
    if not raw:
        return ""
    if re.match(r"^(?:м\.|смт\.?|селище|с-ще\.?|с\.|х\.)\s*", raw, flags=re.IGNORECASE):
        return re.sub(r"\s+", " ", raw)
    bare_low = strip_if_city_prefix(raw).lower()
    for known in IF_KNOWN_SETTLEMENTS:
        if strip_if_city_prefix(known).lower() == bare_low:
            return known
    return f"м. {raw}" if raw[:1].isupper() else raw


def strip_if_street_prefix(street: str) -> str:
    s = (street or "").strip()
    s = re.sub(
        r"^(?:вулиця|вул\.?|в\.|провулок|провул\.?|пров\.?|проспект|просп\.?|пр-т\.?|пр\.?|"
        r"площа|пл\.?|майдан|бульвар|б-р\.?|набережна|наб\.?|тупик|туп\.?|урочище|ур\.?)\s*",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", s).strip(" ,;.")


def format_if_street_display(street: str) -> str:
    raw = (street or "").strip(" ,;.")
    if not raw:
        return ""
    if re.match(
        r"^(?:вул\.|вулиця|пров\.|провулок|просп\.|проспект|пл\.|площа|майдан|б-р|бульвар|наб\.|набережна|тупик|ур\.)\s*",
        raw,
        flags=re.IGNORECASE,
    ):
        s = re.sub(r"^вулиця\s+", "вул. ", raw, flags=re.IGNORECASE)
        s = re.sub(r"^провулок\s+", "пров. ", s, flags=re.IGNORECASE)
        s = re.sub(r"^проспект\s+", "просп. ", s, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", s)
    return f"вул. {raw}"


def strip_if_house_prefix(house: str) -> str:
    s = (house or "").strip()
    s = re.sub(r"^(?:будинок|буд\.?|б\.|№)\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*/\s*", "/", s)
    return s.strip(" ,;.")


def generate_if_street_candidates(street: str) -> list[str]:
    """
    Генерує варіанти написання вулиці для API be-svitlo.oe.if.ua/schedule-by-search,
    оскільки база обленерго очікує чисту назву без префікса 'вул.' (напр. 'Чорновола', 'Січових Стрільців').
    Якщо користувач ввів повне ім'я ('В'ячеслава Чорновола', 'Степана Бандери', 'Івана Мазепи'),
    автоматично пробує і повну назву, і варіант з ініціалом ('В.Чорновола'), і прізвище ('Чорновола').
    """
    bare = strip_if_street_prefix(street)
    if not bare:
        return []

    candidates: list[str] = []

    def _add(val: str) -> None:
        v = val.strip(" ,;.")
        if v and v not in candidates:
            candidates.append(v)
        # Варіанти апострофа в українських назвах (П'ядики, В'ячеслава, Лук'яненка)
        for ap in ("'", "’", "ʼ"):
            v_ap = re.sub(r"['’ʼ`]", ap, v)
            if v_ap and v_ap not in candidates:
                candidates.append(v_ap)

    _add(bare)

    # Якщо є ініціали "В. Чорновола" -> пробуємо також "В.Чорновола" та "Чорновола"
    init_m = re.match(r"^([А-ЯІЇЄҐа-яіїєґ])\.\s*(.+)$", bare)
    if init_m:
        initial = init_m.group(1).upper()
        rest = init_m.group(2).strip()
        _add(f"{initial}.{rest}")
        _add(rest)

    words = [w for w in re.split(r"\s+", bare) if w]
    if len(words) >= 2:
        w0_low = re.sub(r"['’ʼ`]", "'", words[0].lower().strip("."))
        if w0_low in STREET_FIRST_NAMES:
            rest = " ".join(words[1:])
            if len(words[0]) >= 1 and words[0][0].isalpha():
                _add(f"{words[0][0].upper()}.{rest}")
            _add(rest)
        elif len(words) == 2 and not words[0].lower().endswith(("их", "ських", "цьких", "зьких")):
            # На випадок менш поширеного імені (напр. "Олекси Довбуша" -> "Довбуша")
            _add(words[1])

    return candidates[:8]


def parse_freeform_if_address(query: str) -> Optional[tuple[str, str, str]]:
    """
    Розпізнає однорядковий запит адреси для Івано-Франківської області:
      - 'Коломия, Чорновола, 1'
      - 'м. Коломия вул. Січових Стрільців 10а'
      - 'Шепарівці Шевченка 1'
    Повертає (city, street, house) або None, якщо рядок не містить повної адреси.
    """
    raw = (query or "").strip()
    if not raw:
        return None

    # Варіант 1: Через кому (Місто, Вулиця, Будинок)
    comma_parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(comma_parts) >= 3:
        city_p = comma_parts[0]
        street_p = ", ".join(comma_parts[1:-1])
        house_p = comma_parts[-1]
        clean_h = strip_if_house_prefix(house_p)
        if re.match(r"^\d+", clean_h):
            return city_p, street_p, clean_h

    if len(comma_parts) == 2:
        # Напр. "м. Коломия, Чорновола 10"
        city_p, rest_p = comma_parts[0], comma_parts[1]
        hm = re.search(
            r"(?:^|\s+)(?:буд\.?|будинок|№)?\s*(\d+[а-яіїєґА-ЯІЇЄҐa-zA-Z]?(?:/\d+[а-яіїєґА-ЯІЇЄҐa-zA-Z]?)?)$",
            rest_p,
        )
        if hm and rest_p[: hm.start()].strip():
            return city_p, rest_p[: hm.start()].strip(), hm.group(1).strip()

    # Варіант 2: Пробіли без ком (напр. "Коломия Чорновола 1" або "м. Івано-Франківськ Незалежності 10")
    hm = re.search(
        r"\s+(?:буд\.?|будинок|№)?\s*(\d+[а-яіїєґА-ЯІЇЄҐa-zA-Z]?(?:/\d+[а-яіїєґА-ЯІЇЄҐa-zA-Z]?)?)$",
        raw,
    )
    if not hm:
        return None

    house_val = hm.group(1).strip()
    before_house = raw[: hm.start()].strip(" ,;.")
    if not before_house:
        return None

    # Перевіряємо, чи починається рядок з відомого населеного пункту ІФ області
    bh_norm = strip_if_city_prefix(before_house)
    for known in sorted(IF_KNOWN_SETTLEMENTS, key=lambda x: -len(strip_if_city_prefix(x))):
        k_bare = strip_if_city_prefix(known)
        m_known = re.match(
            rf"^(?:(?:м\.|смт\.?|селище|с-ще\.?|с\.)\s*)?{re.escape(k_bare)}\b[\s,]*(.+)$",
            before_house,
            flags=re.IGNORECASE,
        )
        if m_known and m_known.group(1).strip():
            return known, m_known.group(1).strip(), house_val

    # Якщо є явний маркер вулиці ("Коломия вул. Чорновола")
    st_split = re.search(
        r"\s+((?:вул\.?|вулиця|пров\.?|провулок|просп\.?|проспект|пл\.?|площа|б-р|бульвар)\s+.+)$",
        before_house,
        flags=re.IGNORECASE,
    )
    if st_split:
        city_part = before_house[: st_split.start()].strip(" ,;.")
        street_part = st_split.group(1).strip()
        if city_part and street_part:
            return city_part, street_part, house_val

    # Загальний випадок: перше слово (або префікс + слово) = місто, решта = вулиця
    m_gen = re.match(
        r"^((?:(?:м\.|смт\.?|селище|с-ще\.?|с\.)\s*)?[А-ЯІЇЄҐа-яіїєґ'’ʼ\-]+)\s+(.+)$",
        before_house,
    )
    if m_gen:
        c_cand = m_gen.group(1).strip()
        st_cand = m_gen.group(2).strip()
        if not re.match(r"^(?:вул|пров|просп|пл|б-р)\b", c_cand, flags=re.IGNORECASE):
            return c_cand, st_cand, house_val

    return None


@dataclass
class IFAddressLookupResult:
    status: str  # "exact" | "many" | "no_queue" | "not_found" | "error"
    queue_code: Optional[str] = None
    queue_key: Optional[str] = None
    possible_queues: list[str] = field(default_factory=list)
    schedules: dict[date, DaySchedule] = field(default_factory=dict)
    city_display: str = ""
    street_display: str = ""
    house_display: str = ""
    note: str = ""


class IFDataClient:
    """
    Клієнт отримання графіків та пошуку черги за адресою для Івано-Франківської області:
    1. Офіційний JSON API svitlo.oe.if.ua / be-svitlo.oe.if.ua:
       - GET /schedule-by-queue?queue=1.1..6.2
       - POST /schedule-by-search (визначення черги за адресою: місто, вулиця, будинок)
    2. GitHub JSON Mirror (yaroslav2901/OE_OUTAGE_DATA — Prykarpattiaoblenerho.json).
    3. Fallback через bezsvitla.com.ua/ivano-frankivska-oblast/cherha-{slug}.
    """

    def __init__(self) -> None:
        self.api_base_url = settings.if_api_base_url.rstrip("/")
        self.github_json_url = settings.if_github_json_url
        self._http_client: Optional[httpx.AsyncClient] = None
        self._cache_ts: float = 0.0
        self._cache_data: dict[tuple[str, date], DaySchedule] = {}
        self._address_overrides: dict[tuple[str, str, str], str] = {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                headers=IF_API_HEADERS,
                timeout=15.0,
                follow_redirects=True,
            )
        return self._http_client

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def set_address_override(
        self, city_name: str, street_name: str, house_num: str, queue_code: str
    ) -> None:
        c_k = strip_if_city_prefix(city_name).lower()
        s_k = strip_if_street_prefix(street_name).lower()
        h_k = strip_if_house_prefix(house_num).lower()
        if c_k and s_k and h_k and queue_code in ALL_QUEUES:
            self._address_overrides[(c_k, s_k, h_k)] = queue_code

    def get_address_override(
        self, city_name: str, street_name: str, house_num: str
    ) -> Optional[str]:
        c_k = strip_if_city_prefix(city_name).lower()
        s_k = strip_if_street_prefix(street_name).lower()
        h_k = strip_if_house_prefix(house_num).lower()
        return self._address_overrides.get((c_k, s_k, h_k))

    # -------------------------------------------------------------------------
    # Пошук міст та черги за адресою в Івано-Франківській області
    # -------------------------------------------------------------------------
    def get_popular_cities(self, limit: int = 10) -> list[dict[str, str]]:
        return [
            {"id": f"if_city:{strip_if_city_prefix(name)}", "label": name}
            for name in IF_POPULAR_CITIES[:limit]
        ]

    def search_cities(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        q_clean = strip_if_city_prefix(query)
        if not q_clean:
            return self.get_popular_cities(limit=limit)

        q_low = re.sub(r"['’ʼ`]", "'", q_clean.lower())
        exact_matches: list[str] = []
        prefix_matches: list[str] = []
        substr_matches: list[str] = []

        for settlement in IF_KNOWN_SETTLEMENTS:
            s_bare = re.sub(r"['’ʼ`]", "'", strip_if_city_prefix(settlement).lower())
            if s_bare == q_low:
                exact_matches.append(settlement)
            elif s_bare.startswith(q_low):
                prefix_matches.append(settlement)
            elif q_low in s_bare:
                substr_matches.append(settlement)

        ordered = exact_matches + prefix_matches + substr_matches
        results = [
            {"id": f"if_city:{strip_if_city_prefix(name)}", "label": name}
            for name in ordered[:limit]
        ]

        # Дозволяємо будь-яке село чи містечко області, навіть якщо його немає у швидкому списку
        if not exact_matches:
            custom_title = format_if_city_display(query)
            custom_bare = strip_if_city_prefix(custom_title)
            if custom_bare and all(
                strip_if_city_prefix(r["label"]).lower() != custom_bare.lower()
                for r in results
            ):
                results.insert(
                    0,
                    {"id": f"if_city:{custom_bare}", "label": custom_title},
                )

        return results[:limit]

    async def lookup_address(
        self, city_name: str, street_name: str, house_num: str
    ) -> IFAddressLookupResult:
        """
        Визначає чергу ГПВ в Івано-Франківській області через офіційний API
        POST https://be-svitlo.oe.if.ua/schedule-by-search.
        Автоматично очищує префікси ('м.', 'вул.', 'буд.') та перебирає варіанти написання вулиці.
        """
        clean_city = strip_if_city_prefix(city_name)
        clean_house = strip_if_house_prefix(house_num)
        city_disp = format_if_city_display(city_name)
        street_disp = format_if_street_display(street_name)
        house_disp = clean_house or house_num.strip()

        if not clean_city or not street_name.strip() or not clean_house:
            return IFAddressLookupResult(
                status="not_found",
                city_display=city_disp,
                street_display=street_disp,
                house_display=house_disp,
            )

        street_variants = generate_if_street_candidates(street_name)
        city_variants = [clean_city]
        for ap in ("'", "’", "ʼ"):
            c_ap = re.sub(r"['’ʼ`]", ap, clean_city)
            if c_ap not in city_variants:
                city_variants.append(c_ap)

        house_variants = [clean_house]
        if clean_house.lower() != clean_house:
            house_variants.append(clean_house.lower())
        if clean_house.upper() != clean_house:
            house_variants.append(clean_house.upper())

        client = self._get_client()
        url = f"{self.api_base_url}/schedule-by-search"
        saw_network_error = False

        for c_var in city_variants:
            for s_var in street_variants:
                for h_var in house_variants[:2]:
                    addr_param = f"{c_var},{s_var},{h_var}"
                    payload = {
                        "accountNumber": "",
                        "userSearchChoice": "pob",
                        "address": addr_param,
                    }
                    try:
                        resp = await client.post(url, data=payload, timeout=8.0)
                    except Exception as exc:
                        saw_network_error = True
                        logger.warning("IF address lookup HTTP error (%s): %s", addr_param, exc)
                        break

                    if resp.status_code == 204:
                        return IFAddressLookupResult(
                            status="no_queue",
                            city_display=city_disp,
                            street_display=street_disp,
                            house_display=house_disp,
                            note="За цією адресою відключення ГПВ не застосовуються.",
                        )

                    if resp.status_code in (200, 300):
                        try:
                            body = resp.json()
                        except Exception:
                            continue
                        parsed_res = self.parse_search_response(
                            body,
                            city_display=city_disp,
                            street_display=street_disp,
                            house_display=house_disp,
                        )
                        if parsed_res.status in ("exact", "many"):
                            if parsed_res.status == "exact" and parsed_res.queue_code:
                                self.set_address_override(
                                    city_disp, street_disp, house_disp, parsed_res.queue_code
                                )
                            return parsed_res

                if saw_network_error:
                    break
            if saw_network_error:
                break

        ov_code = self.get_address_override(city_disp, street_disp, house_disp)
        if ov_code in ALL_QUEUES:
            q_key = make_queue_key(REGION_IF, ov_code)
            return IFAddressLookupResult(
                status="exact",
                queue_code=ov_code,
                queue_key=q_key,
                city_display=city_disp,
                street_display=street_disp,
                house_display=house_disp,
            )

        return IFAddressLookupResult(
            status="error" if saw_network_error else "not_found",
            city_display=city_disp,
            street_display=street_disp,
            house_display=house_disp,
        )

    @classmethod
    def parse_search_response(
        cls,
        data: dict[str, Any],
        city_display: str = "",
        street_display: str = "",
        house_display: str = "",
    ) -> IFAddressLookupResult:
        current = data.get("current") if isinstance(data, dict) else None
        if not isinstance(current, dict):
            return IFAddressLookupResult(
                status="not_found",
                city_display=city_display,
                street_display=street_display,
                house_display=house_display,
            )

        has_queue = str(current.get("hasQueue") or "").strip().lower()
        note = str(current.get("note") or "").strip()

        if has_queue == "yes":
            q_maj = str(current.get("queue") or "").strip()
            q_sub = str(current.get("subQueue") or "").strip()
            q_code = f"{q_maj}.{q_sub}" if q_sub else q_maj
            if q_code not in ALL_QUEUES:
                m_note = re.search(r"([1-6]\.[12])", note)
                if m_note:
                    q_code = m_note.group(1)

            if q_code in ALL_QUEUES:
                q_key = make_queue_key(REGION_IF, q_code)
                sched_map: dict[date, DaySchedule] = {}
                raw_sched_list = data.get("schedule")
                if isinstance(raw_sched_list, list):
                    for entry in raw_sched_list:
                        for (_, d_val), s_obj in cls.parse_queue_schedule_items(
                            [entry], default_queue_code=q_code
                        ).items():
                            sched_map[d_val] = s_obj

                return IFAddressLookupResult(
                    status="exact",
                    queue_code=q_code,
                    queue_key=q_key,
                    schedules=sched_map,
                    city_display=city_display,
                    street_display=street_display,
                    house_display=house_display,
                    note=note,
                )

        if has_queue == "many" or current.get("possibleQueues"):
            raw_poss = current.get("possibleQueues") or []
            poss = [str(q).strip().replace(",", ".") for q in raw_poss if str(q).strip() in ALL_QUEUES]
            if poss:
                return IFAddressLookupResult(
                    status="many",
                    possible_queues=poss,
                    city_display=city_display,
                    street_display=street_display,
                    house_display=house_display,
                    note=note,
                )

        return IFAddressLookupResult(
            status="not_found",
            city_display=city_display,
            street_display=street_display,
            house_display=house_display,
            note=note,
        )

    async def verify_address_queue(
        self, city_name: str, street_name: str, house_num: str
    ) -> Optional[str]:
        """Повертає актуальний ключ черги ('if:4.1'), якщо адреса однозначно визначається в API."""
        if not city_name or not street_name or not house_num:
            return None
        res = await self.lookup_address(city_name, street_name, house_num)
        if res.status == "exact" and res.queue_key:
            return res.queue_key
        return None

    # -------------------------------------------------------------------------
    # Завантаження графіків усіх 12 підчерг (if:1.1 .. if:6.2)
    # -------------------------------------------------------------------------
    async def fetch_all_queues(
        self, ttl_seconds: float = 30.0
    ) -> dict[tuple[str, date], DaySchedule]:
        now_mono = time.monotonic()
        if self._cache_data and (now_mono - self._cache_ts) < ttl_seconds:
            return dict(self._cache_data)

        # 1. Пріоритет: офіційний REST API be-svitlo.oe.if.ua/schedule-by-queue
        api_schedules = await self._fetch_from_official_api()
        merged: dict[tuple[str, date], DaySchedule] = dict(api_schedules)

        # 2. Якщо офіційний API тимчасово недоступний — fallback на GitHub mirror + bezsvitla
        if not merged:
            gh_schedules, bz_schedules = await asyncio.gather(
                self._fetch_from_github_mirror(),
                self._fetch_from_bezsvitla(),
            )
            merged.update(gh_schedules)
            merged.update(bz_schedules)

        if merged:
            self._cache_ts = now_mono
            self._cache_data = dict(merged)
        return merged

    async def _fetch_from_official_api(self) -> dict[tuple[str, date], DaySchedule]:
        client = self._get_client()

        async def _one(q_code: str) -> dict[tuple[str, date], DaySchedule]:
            url = f"{self.api_base_url}/schedule-by-queue"
            try:
                resp = await client.get(url, params={"queue": q_code}, timeout=10.0)
                if resp.status_code != 200:
                    return {}
                payload = resp.json()
                if not isinstance(payload, list):
                    return {}
                return self.parse_queue_schedule_items(payload, default_queue_code=q_code)
            except Exception as exc:
                logger.debug("IF schedule-by-queue failed for %s: %s", q_code, exc)
                return {}

        results = await asyncio.gather(*(_one(q) for q in ALL_QUEUES))
        combined: dict[tuple[str, date], DaySchedule] = {}
        for part in results:
            combined.update(part)
        return combined

    @classmethod
    def parse_queue_schedule_items(
        cls, items: list[Any], default_queue_code: str
    ) -> dict[tuple[str, date], DaySchedule]:
        """
        Парсить відповідь be-svitlo.oe.if.ua/schedule-by-queue:
        [{"eventDate": "25.11.2026", "queues": {"1.1": [{"from": "07:30", "to": "08:00", "status": 1}, ...]}}]
        """
        out: dict[tuple[str, date], DaySchedule] = {}
        today = datetime.now(settings.tz).date()

        for entry in items:
            if not isinstance(entry, dict):
                continue
            raw_date = str(entry.get("eventDate") or "").strip()
            sched_date = cls._parse_event_date(raw_date)
            if sched_date is None:
                continue

            queues_obj = entry.get("queues")
            if not isinstance(queues_obj, dict):
                continue

            for raw_q, intervals in queues_obj.items():
                q_code = str(raw_q).strip().replace(",", ".")
                if q_code not in ALL_QUEUES:
                    q_code = default_queue_code
                if q_code not in ALL_QUEUES or not isinstance(intervals, list):
                    continue

                slots = cls._intervals_to_48_slots(intervals)
                q_key = make_queue_key(REGION_IF, q_code)
                day_sched = DaySchedule(
                    queue=q_key,
                    schedule_date=sched_date,
                    slots=slots,
                    source="if_api",
                )
                # Порожні майбутні дати не вважаємо опублікованим графіком на завтра
                if sched_date > today and day_sched.total_outage_minutes == 0:
                    continue
                out[(q_key, sched_date)] = day_sched

        return out

    @staticmethod
    def _parse_event_date(raw_date: str) -> Optional[date]:
        if not raw_date:
            return None
        m_dmy = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", raw_date)
        if m_dmy:
            try:
                return date(int(m_dmy.group(3)), int(m_dmy.group(2)), int(m_dmy.group(1)))
            except ValueError:
                return None
        try:
            return date.fromisoformat(raw_date[:10])
        except ValueError:
            return None

    @staticmethod
    def _intervals_to_48_slots(intervals: list[Any]) -> list[SlotState]:
        slots = [SlotState.ON] * 48
        for item in intervals:
            if not isinstance(item, dict):
                continue
            from_str = str(item.get("from") or "").strip()
            to_str = str(item.get("to") or "").strip()
            if not (from_str and to_str):
                sh_hours = str(item.get("shutdownHours") or "").strip()
                if "-" in sh_hours:
                    from_str, to_str = [p.strip() for p in sh_hours.split("-", 1)]

            m1 = re.match(r"^(\d{1,2}):(\d{2})$", from_str)
            m2 = re.match(r"^(\d{1,2}):(\d{2})$", to_str)
            if not (m1 and m2):
                continue

            sh, sm = int(m1.group(1)), int(m1.group(2))
            eh, em = int(m2.group(1)), int(m2.group(2))
            start_idx = max(0, min(48, sh * 2 + (1 if sm >= 30 else 0)))
            end_idx = (
                48
                if (eh == 24 or (eh == 0 and em == 0 and (sh > 0 or sm > 0 or from_str == "00:00" and to_str == "24:00")))
                else max(0, min(48, eh * 2 + (1 if em >= 30 else 0)))
            )
            if from_str == "00:00" and to_str == "00:00":
                end_idx = 48

            raw_status = item.get("status", 1)
            if raw_status in (2, "2", "possible", "maybe"):
                st = SlotState.POSSIBLE_OFF
            elif raw_status in (0, "0", "on", "yes"):
                st = SlotState.ON
            else:
                st = SlotState.OFF

            for idx in range(start_idx, end_idx):
                slots[idx] = st
        return slots

    async def _fetch_from_github_mirror(self) -> dict[tuple[str, date], DaySchedule]:
        from app.services.voe_client import VOEDataClient

        try:
            client = self._get_client()
            resp = await client.get(self.github_json_url, timeout=12.0)
            resp.raise_for_status()
            parsed = VOEDataClient.parse_github_json(resp.json())
            remapped: dict[tuple[str, date], DaySchedule] = {}
            for (q_code, s_date), sched in parsed.items():
                q_key = make_queue_key(REGION_IF, q_code)
                remapped[(q_key, s_date)] = DaySchedule(
                    queue=q_key,
                    schedule_date=s_date,
                    slots=list(sched.slots),
                    source="if_github_json",
                )
            return remapped
        except Exception as exc:
            logger.warning("IF GitHub JSON mirror fetch failed: %s", exc)
            return {}

    async def _fetch_from_bezsvitla(self) -> dict[tuple[str, date], DaySchedule]:
        from app.services.voe_client import VOEDataClient

        client = self._get_client()
        today = datetime.now(settings.tz).date()

        async def _fetch_one(q_code: str) -> list[DaySchedule]:
            slug = q_code.replace(".", "-")
            url = f"https://bezsvitla.com.ua/ivano-frankivska-oblast/cherha-{slug}"
            try:
                resp = await client.get(url, timeout=10.0)
                if resp.status_code != 200:
                    return []
                today_s, _, tomorrow_s = VOEDataClient.parse_bezsvitla_queue_html(
                    resp.text, make_queue_key(REGION_IF, q_code), today
                )
                found: list[DaySchedule] = []
                if today_s is not None:
                    today_s.source = "if_bezsvitla"
                    found.append(today_s)
                if tomorrow_s is not None and tomorrow_s.total_outage_minutes > 0:
                    tomorrow_s.source = "if_bezsvitla"
                    found.append(tomorrow_s)
                return found
            except Exception as exc:
                logger.debug("IF bezsvitla fetch failed for %s: %s", q_code, exc)
                return []

        batches = await asyncio.gather(*(_fetch_one(q) for q in ALL_QUEUES))
        out: dict[tuple[str, date], DaySchedule] = {}
        for batch in batches:
            for s in batch:
                out[(s.queue, s.schedule_date)] = s
        return out
