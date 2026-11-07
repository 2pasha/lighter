from __future__ import annotations

import json
import logging
import os
import re
import zlib
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_INDEX_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "voe_addresses.json"
VOE_GPV_INFO_URL = "https://www.voe.com.ua/informatsiya-pro-cherhy-hrafika-pohodynnykh-vidklyuchen-hpv-1"
VOE_BASE_URL = "https://www.voe.com.ua"

POPULAR_CITY_NAMES = [
    "м. Вінниця",
    "с. Агрономічне",
    "с. Зарванці",
    "с. Якушинці",
    "смт. Стрижавка",
    "с. Вінницькі Хутори",
    "с. Бохоники",
    "м. Жмеринка",
    "м. Гайсин",
    "м. Могилів-Подільський",
    "м. Хмільник",
    "м. Козятин",
    "м. Калинівка",
    "м. Ладижин",
]

# Known spelling normalizations in VOE's PDF
CITY_NAME_FIXES = {
    "с. Богхоники": "с. Бохоники",
    "с. Пирогова": "с. Пирогове (Садиба Пирогова)",
    "с. Вінницькі Хутора": "с. Вінницькі Хутори",
    "с. В.Хутори": "с. Вінницькі Хутори",
    "с. В. Хутори": "с. Вінницькі Хутори",
    "с. Л.Мелешківська": "с. Лука-Мелешківська",
    "с. Л. Мелешківська": "с. Лука-Мелешківська",
    "с. М.Крушлинці": "с. Малі Крушлинці",
    "с. М. Крушлинці": "с. Малі Крушлинці",
    "с. В.Крушлинці": "с. Великі Крушлинці",
    "с. В. Крушлинці": "с. Великі Крушлинці",
    "с. Міз.Хутори": "с. Мізяківські Хутори",
    "смт. Вороновиц": "смт. Вороновиця",
    "смт Крижопіль": "смт. Крижопіль",
    "селище Брацлав": "смт. Брацлав",
    "с-ще. Брацлав": "смт. Брацлав",
    "с-ще. Теплик": "смт. Теплик",
    "с-ще. Чечельник": "смт. Чечельник",
    "селище Піщанка": "смт. Піщанка",
    "селище Рудниця": "смт. Рудниця",
    "м. Могилів-": "м. Могилів-Подільський",
}

STREET_SYNONYMS = {
    "хм шосе": "хмельницьке шосе",
    "хм.шосе": "хмельницьке шосе",
    "хм. шосе": "хмельницьке шосе",
    "корiатовичiв": "коріатовичів",
    "600 річчя": "600-річчя",
    "600-рiччя": "600-річчя",
}

# Verified address overrides from official portal disconnection.voe.com.ua
# where VOE's published GPV PDF has gaps, exceptions or omitted house numbers
KNOWN_ADDRESS_OVERRIDES: dict[tuple[str, str, str], str] = {
    ("вінниця", "порика", "48"): "3.2",
    ("вінниця", "василя порика", "48"): "3.2",
}

FIRST_NAMES = {
    "василя", "миколи", "івана", "дмитра", "олександра", "академіка", "генерала",
    "архітектора", "гетьмана", "петра", "данила", "сергія", "лева", "левка",
    "марка", "ігоря", "матроса", "павла", "костя", "максима", "тараса", "лесі",
    "михайла", "григорія", "андрія", "богдана", "володимира", "ярослава", "олега",
    "костянтина", "степана", "князів", "зої", "юрія", "ак", "в", "м", "і", "д",
    "о", "а", "г", "п", "с", "л", "к", "б", "ю", "т", "я",
}


def _normalize_text(text: str) -> str:
    """Lowercase, normalize apostrophes and whitespace for fuzzy matching."""
    if not text:
        return ""
    s = text.lower().strip()
    s = re.sub(r"[\x27\u2019\u2018`ʼ']", "'", s)
    # Replace Latin lookalikes sometimes found in VOE PDFs
    s = s.replace("i", "і").replace("c", "с").replace("o", "о").replace("a", "а").replace("e", "е")
    s = re.sub(r"\s+", " ", s)
    return s


def _strip_city_prefix(name: str) -> str:
    s = _normalize_text(name)
    s = re.sub(r"^(?:м\.|смт\.?|селище|с-ще\.?|с\.|х\.)\s*", "", s)
    return s.strip()


def _strip_street_prefix(name: str) -> str:
    s = _normalize_text(name)
    s = re.sub(
        r"^(?:[1-9](?:-[йії])?\s+)?(?:вул\.?|вулиця|в\.|пров\.?|провулок|провул\.?|пр-т\.?|пр\.?|просп\.?|проспект|пл\.?|площа|туп\.?|тупик|тупік|проїзд|б-р|бульвар|шосе)\s*",
        "",
        s,
    )
    # Also strip leading initials like "м. оводова" -> "оводова" only for secondary comparison
    return s.strip(" .,;:")


def _strip_street_initials(name: str) -> str:
    s = _strip_street_prefix(name)
    s = re.sub(r"^(?:[а-яіїєґ]{1,3}\.\s*)+", "", s)
    return s.strip()


def get_canonical_street_key(name: str) -> str:
    """
    Produce a canonical key for street grouping, merging name variations like
    'вул. Порика' and 'вул. Василя Порика', or 'вул. Артинова' and 'вул. Архітектора Артинова'.
    Preserves numbered lane types ('1-й пров' vs '2-й пров') and suburb prefixes.
    """
    s = _normalize_text(name)
    s = s.replace("первозваного", "первозванного")
    suburb_prefix = ""
    if "," in s and s.startswith(("с.", "смт.")):
        suburb_prefix, s = s.split(",", 1)
        suburb_prefix = suburb_prefix.strip() + ":"

    st_type = "вул"
    for t in [
        "1-й пров", "2-й пров", "3-й пров", "4-й пров",
        "1-й тупик", "2-й тупик", "проїзд", "тупик", "пров",
        "просп", "площа", "шосе", "бульвар",
    ]:
        if t in s:
            st_type = t
            break

    s = re.sub(
        r"^(?:[1-9](?:-[йії])?\s+)?(?:вул\.?|вулиця|в\.|пров\.?|провулок|провул\.?|пр-т\.?|пр\.?|просп\.?|проспект|пл\.?|площа|туп\.?|тупик|тупік|проїзд|б-р|бульвар|шосе)\s*",
        "",
        s,
    )
    words = [w.strip(" .,;:") for w in re.split(r"[\s.]+", s) if w.strip(" .,;:")]
    remaining = [w for w in words if w not in FIRST_NAMES]
    base = " ".join(remaining) if remaining else " ".join(words)
    return f"{suburb_prefix}{st_type}:{base}"


def score_house_proximity(u_num: int, spec: str) -> tuple[int, str]:
    """
    Check physical proximity of user house number to known numbers in a spec string.
    Returns (proximity_score, description):
      3 = immediate adjacent neighbor on same parity (distance == 2, e.g. 48 vs 50 or 15 vs 13)
      2 = near neighbor on same parity (distance <= 4, e.g. 46 vs 50)
      1 = close neighbor (distance <= 8 on same parity, or distance == 1 on opposite parity)
      0 = far or no comparison possible
    """
    if not spec:
        return 0, ""
    best_score = 0
    best_desc = ""
    tokens = [t.strip() for t in re.split(r"[,;]+", spec) if t.strip()]
    for tok in tokens:
        tok_norm = _normalize_text(tok)
        rm = re.match(r"^(\d+)([а-яіїєґa-z]?)-(\d+)([а-яіїєґa-z]?)$", tok_norm)
        if rm:
            s_n, e_n = int(rm.group(1)), int(rm.group(3))
            for ep in (s_n, e_n):
                if ep % 2 == u_num % 2:
                    d = abs(ep - u_num)
                    if d == 2 and best_score < 3:
                        best_score, best_desc = 3, f"adjacent {ep}"
                    elif d <= 4 and best_score < 2:
                        best_score, best_desc = 2, f"near {ep}"
                    elif d <= 8 and best_score < 1:
                        best_score, best_desc = 1, f"near {ep}"
                elif abs(ep - u_num) == 1 and best_score < 1:
                    best_score, best_desc = 1, f"opposite {ep}"
        else:
            t_num, _ = _parse_house_token(tok_norm)
            if t_num is not None:
                if t_num % 2 == u_num % 2:
                    d = abs(t_num - u_num)
                    if d == 2 and best_score < 3:
                        best_score, best_desc = 3, f"adjacent {t_num}"
                    elif d <= 4 and best_score < 2:
                        best_score, best_desc = 2, f"near {t_num}"
                    elif d <= 8 and best_score < 1:
                        best_score, best_desc = 1, f"near {t_num}"
                elif abs(t_num - u_num) == 1 and best_score < 1:
                    best_score, best_desc = 1, f"opposite {t_num}"
    return best_score, best_desc


# ---------------------------------------------------------------------------
# Pure-Python PDF Extractor for VOE's GPV PDF
# ---------------------------------------------------------------------------

def _parse_cmap(cmap_bytes: bytes) -> dict[int, str]:
    mapping: dict[int, str] = {}
    in_char = False
    in_range = False
    for line in cmap_bytes.decode("latin1", errors="ignore").splitlines():
        line = line.strip()
        if "beginbfchar" in line:
            in_char = True
            continue
        if "endbfchar" in line:
            in_char = False
            continue
        if "beginbfrange" in line:
            in_range = True
            continue
        if "endbfrange" in line:
            in_range = False
            continue
        if in_char:
            m = re.match(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", line)
            if m:
                hx = m.group(2)
                mapping[int(m.group(1), 16)] = "".join(
                    chr(int(hx[i : i + 4], 16)) for i in range(0, len(hx), 4)
                )
        elif in_range:
            m = re.match(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", line)
            if m:
                start, end, dst = int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16)
                for code in range(start, end + 1):
                    mapping[code] = chr(dst + (code - start))
    return mapping


def extract_rows_from_gpv_pdf(pdf_bytes: bytes) -> list[tuple[str, str, str]]:
    """
    Extract (branch_name, queue_code, row_text) tuples from VOE's official GPV PDF
    using pure Python stdlib (zlib + re).
    """
    objs: dict[int, bytes] = {}
    streams: dict[int, bytes] = {}

    for m in re.finditer(rb"(\d+)\s+0\s+obj\b(.*?)endobj", pdf_bytes, re.S):
        oid, body = int(m.group(1)), m.group(2)
        objs[oid] = body
        sm = re.search(rb"stream\r?\n(.*?)\r?\nendstream", body, re.S)
        if sm and b"/FlateDecode" in body[: body.find(b"stream")]:
            try:
                streams[oid] = zlib.decompress(sm.group(1))
            except Exception:
                pass

    # Unpack compressed Object Streams (/ObjStm)
    for oid, body in list(objs.items()):
        if b"/ObjStm" in body and oid in streams:
            data = streams[oid]
            first_m = re.search(rb"/First\s+(\d+)", body)
            if first_m:
                first = int(first_m.group(1))
                toks = data[:first].split()
                pairs = [
                    (int(toks[i]), int(toks[i + 1]))
                    for i in range(0, len(toks) - 1, 2)
                ]
                for idx, (sub_oid, off) in enumerate(pairs):
                    end_off = pairs[idx + 1][1] if idx + 1 < len(pairs) else len(data) - first
                    objs[sub_oid] = data[first + off : first + end_off]

    font_cmaps: dict[int, dict[int, str]] = {}
    font_is_two_byte: dict[int, bool] = {}
    font_is_bold: dict[int, bool] = {}
    for oid, body in objs.items():
        tu_m = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body)
        if tu_m and int(tu_m.group(1)) in streams:
            font_cmaps[oid] = _parse_cmap(streams[int(tu_m.group(1))])
            font_is_two_byte[oid] = b"/Identity-H" in body or b"/Type0" in body
            font_is_bold[oid] = b"Bold" in body

    # Find root /Pages object
    root_pages_oid = None
    for oid, body in objs.items():
        if b"/Type/Pages" in body and b"/Parent" not in body:
            root_pages_oid = oid
            break
    if root_pages_oid is None:
        root_pages_oid = 6508

    def _get_pages(oid: int) -> list[int]:
        body = objs.get(oid, b"")
        if re.search(rb"/Type\s*/Page\b", body):
            return [oid]
        kids_m = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
        res: list[int] = []
        if kids_m:
            for km in re.finditer(rb"(\d+)\s+0\s+R", kids_m.group(1)):
                res.extend(_get_pages(int(km.group(1))))
        return res

    page_oids = _get_pages(root_pages_oid)

    current_branch: str | None = None
    current_queue: str | None = None
    row_buf: list[str] = []
    rows: list[tuple[str, str, str]] = []

    def _flush_row() -> None:
        nonlocal row_buf
        if row_buf and current_branch and current_queue:
            joined = " ".join(row_buf)
            # Reconnect hyphenated house ranges split across lines
            joined = re.sub(r"(\d)-\s+(\d)", r"\1-\2", joined)
            rows.append((current_branch, current_queue, joined))
        row_buf = []

    for page_oid in page_oids:
        body = objs.get(page_oid, b"")
        fdict_m = re.search(rb"/Font\s*<<(.*?)>>", body, re.S)
        font_map: dict[str, int] = {}
        if fdict_m:
            for fm in re.finditer(rb"/(\w+)\s+(\d+)\s+0\s+R", fdict_m.group(1)):
                font_map[fm.group(1).decode("latin1")] = int(fm.group(2))

        cm_arr = re.search(rb"/Contents\s*\[(.*?)\]", body, re.S)
        if cm_arr:
            c_oids = [int(x) for x in re.findall(rb"(\d+)\s+0\s+R", cm_arr.group(1))]
        else:
            cm_single = re.search(rb"/Contents\s+(\d+)\s+0\s+R", body)
            c_oids = [int(cm_single.group(1))] if cm_single else []

        content = b"\n".join(streams[c] for c in c_oids if c in streams).decode("latin1", errors="ignore")
        current_font_oid: int | None = None
        font_stack: list[int | None] = []

        for line in content.splitlines():
            ls = line.strip()
            if ls == "q":
                font_stack.append(current_font_oid)
            elif ls == "Q":
                if font_stack:
                    current_font_oid = font_stack.pop()
            fm = re.search(r"/(\w+)\s+[\d.]+\s+Tf", ls)
            if fm:
                current_font_oid = font_map.get(fm.group(1))
            if ls.endswith("Tj") or ls.endswith("TJ"):
                cmap = font_cmaps.get(current_font_oid or -1, {})
                two_byte = font_is_two_byte.get(current_font_oid or -1, True)
                out: list[str] = []
                for tok in re.finditer(r"<([0-9A-Fa-f]+)>|\(([^()]*)\)", ls):
                    if tok.group(1) is not None:
                        hx = tok.group(1)
                        step = 4 if two_byte else 2
                        for i in range(0, len(hx), step):
                            code = int(hx[i : i + step], 16)
                            out.append(cmap.get(code, chr(code) if code < 128 else ""))
                    elif tok.group(2) is not None:
                        for ch in tok.group(2):
                            out.append(cmap.get(ord(ch), ch))
                txt = "".join(out).strip()
                if not txt:
                    continue
                if font_is_bold.get(current_font_oid or -1, False):
                    if "дільниця" in txt or "СО " in txt:
                        _flush_row()
                        current_branch = re.sub(r"\s+", " ", txt).strip()
                elif re.fullmatch(r"[1-6]\.[12]", txt):
                    _flush_row()
                    current_queue = txt
                elif (
                    not re.fullmatch(r"\d+|[1-6]\s*черга|Назва населених пунктів, вулиць", txt)
                    and "Графік погодинного" not in txt
                    and "по Вінницькій" not in txt
                    and "з 1 жовтня" not in txt
                ):
                    row_buf.append(txt)

    _flush_row()
    return rows


# ---------------------------------------------------------------------------
# Row-to-City/Street/House Parser
# ---------------------------------------------------------------------------

HOUSE_TOK = r"\d+[а-яіїєґА-ЯІЇЄҐa-zA-Z]?(?:[/-]\d+[а-яіїєґА-ЯІЇЄҐa-zA-Z]?)*"
HOUSE_LIST_RE = rf"{HOUSE_TOK}(?:\s*[,;]\s*{HOUSE_TOK})*"

SETTLEMENT_MATCH_RE = re.compile(
    r"(?:^|(?<=[\s,;:.()]))"
    r"(м\.|смт\.?|селище|с-ще\.?|с\.|х\.)\s*"
    r"([А-ЯІЇЄҐ]\.\s*[А-ЯІЇЄҐ][А-ЯІЇЄҐа-яіїєґ\x27\-]+|[А-ЯІЇЄҐ][А-ЯІЇЄҐа-яіїєґ\x27\-]+(?:\s+[А-ЯІЇЄҐ][А-ЯІЇЄҐа-яіїєґ\x27\-]+)?)"
)


def _canonical_city_name(prefix: str, raw_name: str) -> str:
    p = prefix.strip().lower()
    if p in ("смт", "смт.", "селище", "с-ще", "с-ще."):
        norm_p = "смт."
    elif p == "м.":
        norm_p = "м."
    elif p == "х.":
        norm_p = "х."
    else:
        norm_p = "с."
    clean_name = re.sub(r"\s+", " ", raw_name.strip(" .,;:()"))
    # Avoid trailing street words captured into 2-word settlement names
    clean_name = re.sub(r"\s+(?:вул|пров|школа|лікарня|ферма|дитячий|завод|масив|р-н|м-р).*$", "", clean_name, flags=re.I)
    cand = f"{norm_p} {clean_name}"
    return CITY_NAME_FIXES.get(cand, CITY_NAME_FIXES.get(f"{prefix.strip()} {clean_name}", cand))


def _clean_street_display(raw_street: str) -> str:
    s = re.sub(r"\s+", " ", raw_street.strip(" .,;:()"))
    if not s:
        return ""
    s = re.sub(r"^провулок\s+", "пров. ", s, flags=re.I)
    s = re.sub(r"^провул\.?\s+", "пров. ", s, flags=re.I)
    s = re.sub(r"^вулиця\s+", "вул. ", s, flags=re.I)
    s = re.sub(r"^проспект\s+", "просп. ", s, flags=re.I)
    s = re.sub(r"^тупік\s+", "тупик ", s, flags=re.I)
    # Normalize "вул.Батозька" -> "вул. Батозька", "вул П.Запорожця" -> "вул. П.Запорожця"
    s = re.sub(r"^(вул|пров|просп|пр|туп)(?:\.|\b)\s*", lambda m: f"{m.group(1).lower()}. ", s, flags=re.I)
    s = re.sub(r"^в\.\s*", "вул. ", s, flags=re.I)
    # If it's a bare street name (starts with uppercase letter and doesn't have a type prefix)
    if not re.match(
        r"^(?:[1-9](?:-[йії])?\s+)?(?:вул\.|пров\.|пр\.|просп\.|пл\.|площа|тупик|туп\.|проїзд|шосе|хм\.?\s*шосе|м-р|м-н|р-н|масив|бульвар|б-р|набережна|жк|трц|зош|садиба|пвнз|кп|тов|прат|ринок)",
        s,
        re.I,
    ):
        s = f"вул. {s}"
    return s


def _parse_street_segments(text: str) -> list[tuple[str, str]]:
    """
    Parse a text block of streets & house numbers into (street_name, houses_spec) pairs.
    """
    # Insert missing space between street letter and house digit (e.g. "Хуторянська1-29", "М.Амосова6", "пр.Юності32")
    cleaned = re.sub(r"([а-яіїєґА-ЯІЇЄҐ]{3,})(\d)", r"\1 \2", text)
    # Fix PDF typos like "11- ,18" -> "11-15,18" or "11,18"
    cleaned = re.sub(r"(\d+)-\s*,", r"\1,", cleaned)
    # Collapse multi-space between street name and its house numbers
    cleaned = re.sub(
        r"([А-ЯІЇЄҐа-яіїєґ.])\s{2,}(?=\d+(?:[-/,]\d+|[а-яіїєґА-ЯІЇЄҐ]|\s*,\s*\d+|\s*$))",
        r"\1 ",
        cleaned,
    )
    # Also normalize "будинки №79,80" or "буд. 1-5" -> "79,80"
    cleaned = re.sub(r"\b(?:будинки|будинок|буд\.?)\s*№?\s*(?=\d)", "", cleaned, flags=re.I)

    # Split on 2+ spaces, semicolons, or before street prefixes
    chunks = re.split(
        r"\s{2,}|;\s*|\s+(?=(?:[1-9](?:-[йії])?(?:-[0-9](?:-[йії])?)?\s+(?:пров\.?|провулок|провул\.?|проїзд|тупик|тупік)|(?:вул|Вул|просп|пр-т|пр|площа|пл|м-р|р-н|масив)(?:\.|\b)|(?<![1-3]\s)(?<![1-3]-[йії]\s)(?:пров|Пров|провулок|провул|тупик|тупік|проїзд)(?:\.|\b)))",
        cleaned,
    )

    results: list[tuple[str, str]] = []
    for ch in chunks:
        ch = ch.strip(" ,;.")
        if not ch:
            continue
        # Split on comma when followed by a street name + house numbers (or comma-separated street names)
        subchunks = re.split(
            r",\s*(?=(?:[1-9](?:-[йії])?\s+)?(?:вул|пров|провулок|просп|пр|пл|площа|тупик|проїзд)(?:\.|\b)|(?:[А-ЯІЇЄҐ]\.\s*)?[А-ЯІЇЄҐ][а-яіїєґ\x27\-]+(?:\s+[А-ЯІЇЄҐа-яіїєґ\x27\-]+)?[,\s]+\d)",
            ch,
        )
        for sc in subchunks:
            sc = sc.strip(" ,;.")
            if not sc:
                continue
            m = re.match(rf"^(.+?)[,\s]+({HOUSE_LIST_RE})$", sc)
            if m:
                st_raw, houses_raw = m.group(1).strip(" ,:."), m.group(2).strip(" ,.")
                # Skip noise if st_raw is purely numeric
                if re.fullmatch(r"[\d\s,/-]+", st_raw):
                    if results:
                        prev_st, prev_h = results[-1]
                        results[-1] = (prev_st, f"{prev_h},{st_raw},{houses_raw}".strip(","))
                    continue
                results.append((_clean_street_display(st_raw), re.sub(r"\s+", "", houses_raw)))
            else:
                # Check if sc is a comma-separated list of bare street names (common in district towns/villages)
                has_house_digits = bool(re.search(r"(?:^|[\s,])\d+(?![\s\-]*[йії]\b)(?!\s*(?:травн|річчя|березн|жовтн|січн|грудн))", sc, re.I))
                if "," in sc and not has_house_digits:
                    for bare_st in sc.split(","):
                        bare_st = bare_st.strip(" ,;:.()")
                        if len(bare_st) >= 3 and not re.fullmatch(r"(?:школа|садок|ферма|лікарня|клуб|сільрада|млин|олійня|вишка|пташник)", bare_st, re.I):
                            results.append((_clean_street_display(bare_st), ""))
                else:
                    if re.fullmatch(r"[\d\s,/-]+", sc) and results:
                        prev_st, prev_h = results[-1]
                        results[-1] = (prev_st, f"{prev_h},{sc}".strip(","))
                    elif len(sc) >= 3:
                        results.append((_clean_street_display(sc), ""))
    return results


def merge_city_streets(city_id: str, streets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Merge duplicate street variants in a city (e.g. 'вул. Порика' and 'вул. Василя Порика',
    or 'вул. Артинова' and 'вул. Архітектора Артинова') into a single canonical street object.
    Preserves aliases and merges (houses, queue) records without duplicates.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for st in streets:
        k = get_canonical_street_key(st["name"])
        groups.setdefault(k, []).append(st)

    merged: list[dict[str, Any]] = []
    for s_idx, (k, st_list) in enumerate(groups.items()):
        new_id = f"{city_id}_s_{s_idx}"
        if len(st_list) == 1:
            st = dict(st_list[0])
            st["id"] = new_id
            if "aliases" not in st:
                st["aliases"] = []
            merged.append(st)
        else:
            sorted_by_pref = sorted(
                st_list,
                key=lambda s: (
                    -len(re.findall(r"\b[А-ЯІЇЄҐа-яіїєґ]{4,}\b", s["name"])),
                    -len(s["name"]),
                ),
            )
            primary = sorted_by_pref[0]
            aliases = sorted(
                {s["name"] for s in st_list if s["name"] != primary["name"]}
                | {a for s in st_list for a in s.get("aliases", []) if a != primary["name"]}
            )
            all_records: list[dict[str, str]] = []
            for s in st_list:
                for r in s.get("records", []):
                    rec = {"houses": r.get("houses", ""), "queue": r.get("queue", "")}
                    if rec not in all_records:
                        all_records.append(rec)
            merged.append(
                {
                    "id": new_id,
                    "name": primary["name"],
                    "aliases": aliases,
                    "records": all_records,
                }
            )
    return merged


def migrate_voe_addresses(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate an entire voe_addresses index to have deduplicated/canonicalized streets with aliases."""
    new_cities: list[dict[str, Any]] = []
    for city in data.get("cities", []):
        c_id = city["id"]
        old_streets = city.get("streets", [])
        new_streets = merge_city_streets(c_id, old_streets)
        new_city = dict(city)
        new_city["streets"] = new_streets
        new_cities.append(new_city)
    return {
        "version": data.get("version", "2026-2027"),
        "source_url": data.get("source_url", ""),
        "cities": new_cities,
    }


def build_index_from_rows(rows: list[tuple[str, str, str]]) -> dict[str, Any]:
    """
    Build the structured cities -> streets -> house records index from extracted PDF rows.
    """
    # city_name -> {"branch": str, "queues": set, "streets": {norm_st: {"name": str, "records": list}}}
    cities_map: dict[str, dict[str, Any]] = {}

    def _ensure_city(city_name: str, branch: str) -> dict[str, Any]:
        if city_name not in cities_map:
            cities_map[city_name] = {
                "name": city_name,
                "branch": branch,
                "queues": set(),
                "streets": {},
            }
        return cities_map[city_name]

    def _add_street_record(city_name: str, branch: str, street_name: str, houses: str, queue: str) -> None:
        c_obj = _ensure_city(city_name, branch)
        c_obj["queues"].add(queue)
        if not street_name:
            return
        norm_st = _strip_street_prefix(street_name)
        if not norm_st or len(norm_st) < 2:
            return
        st_entry = c_obj["streets"].get(norm_st)
        if st_entry is None:
            st_entry = {
                "name": street_name,
                "records": [],
            }
            c_obj["streets"][norm_st] = st_entry
        # Avoid duplicate (houses, queue) record
        rec = {"houses": houses, "queue": queue}
        if rec not in st_entry["records"]:
            st_entry["records"].append(rec)

    for branch, queue, text in rows:
        if branch == 'СО "ВМЕМ"':
            # Default city is м. Вінниця, but check if row contains suburb markers (e.g., с.Агрономічне, с.Бохоники)
            matches = list(SETTLEMENT_MATCH_RE.finditer(text))
            if not matches:
                for st_name, houses in _parse_street_segments(text):
                    _add_street_record("м. Вінниця", branch, st_name, houses, queue)
            else:
                # Text before the first settlement marker belongs to м. Вінниця
                first_start = matches[0].start()
                if first_start > 0:
                    prefix_text = text[:first_start].strip(" ,;:")
                    for st_name, houses in _parse_street_segments(prefix_text):
                        _add_street_record("м. Вінниця", branch, st_name, houses, queue)
                for idx, sm in enumerate(matches):
                    c_name = _canonical_city_name(sm.group(1), sm.group(2))
                    seg_start = sm.end()
                    seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
                    seg_text = text[seg_start:seg_end].strip(" ,;:()")
                    _ensure_city(c_name, branch)["queues"].add(queue)
                    for st_name, houses in _parse_street_segments(seg_text):
                        _add_street_record(c_name, branch, st_name, houses, queue)
                        # Also register under м. Вінниця with suburb prefix for single-city lookup convenience
                        _add_street_record("м. Вінниця", branch, f"{c_name}, {st_name}", houses, queue)
        else:
            # District branch row
            matches = list(SETTLEMENT_MATCH_RE.finditer(text))
            if not matches:
                continue
            for idx, sm in enumerate(matches):
                c_name = _canonical_city_name(sm.group(1), sm.group(2))
                seg_start = sm.end()
                seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
                seg_text = text[seg_start:seg_end].strip()
                c_obj = _ensure_city(c_name, branch)
                c_obj["queues"].add(queue)

                # Extract parenthetical street lists if present: e.g. "(вул. Київська, Соборна, ...)"
                paren_matches = list(re.finditer(r"\(([^()]+)\)", seg_text))
                if paren_matches:
                    for pm in paren_matches:
                        inner = pm.group(1).strip()
                        inner = re.sub(r"^вул\s*:\s*", "вул. ", inner, flags=re.I)
                        for st_name, houses in _parse_street_segments(inner):
                            _add_street_record(c_name, branch, st_name, houses, queue)
                else:
                    # Check if seg_text has streets after comma/colon/semicolon
                    cleaned_seg = seg_text.strip(" ,;:.")
                    if cleaned_seg and re.search(r"\b(?:вул|пров|в\.|просп|площа|тупик)\b|;", cleaned_seg, re.I):
                        for st_name, houses in _parse_street_segments(cleaned_seg):
                            _add_street_record(c_name, branch, st_name, houses, queue)

    # Sort cities: popular cities first, then м., then смт., then с.
    def _city_sort_key(c_name: str) -> tuple[int, int, str]:
        if c_name in POPULAR_CITY_NAMES:
            pop_idx = POPULAR_CITY_NAMES.index(c_name)
        else:
            pop_idx = 999
        if c_name.startswith("м."):
            type_rank = 0
        elif c_name.startswith("смт."):
            type_rank = 1
        else:
            type_rank = 2
        return (pop_idx, type_rank, c_name)

    sorted_city_names = sorted(cities_map.keys(), key=_city_sort_key)
    cities_list: list[dict[str, Any]] = []

    for c_idx, c_name in enumerate(sorted_city_names):
        c_data = cities_map[c_name]
        city_id = f"c_{c_idx}"
        raw_streets: list[dict[str, Any]] = []
        for s_idx, (_, s_data) in enumerate(sorted(c_data["streets"].items(), key=lambda x: x[1]["name"])):
            raw_streets.append(
                {
                    "id": f"{city_id}_s_{s_idx}",
                    "name": s_data["name"],
                    "records": s_data["records"],
                }
            )
        streets_list = merge_city_streets(city_id, raw_streets)
        cities_list.append(
            {
                "id": city_id,
                "name": c_name,
                "branch": c_data["branch"],
                "queues": sorted(c_data["queues"]),
                "streets": streets_list,
            }
        )

    return {
        "version": "2026-2027",
        "source_url": f"{VOE_BASE_URL}/sites/default/files/2026-09/gpv-2026-2027-na-sayt.pdf",
        "cities": cities_list,
    }


# ---------------------------------------------------------------------------
# House Number & Range Matcher
# ---------------------------------------------------------------------------

def _parse_house_token(h: str) -> tuple[int | None, str]:
    """Extract (numeric_part, letter_or_slash_suffix) from a house number like '15а' or '5/22'."""
    s = _normalize_text(h).strip()
    s = re.sub(r"^(?:буд\.?|будинок|№)\s*", "", s)
    m = re.match(r"^(\d+)(.*)$", s)
    if not m:
        return None, s
    return int(m.group(1)), m.group(2).strip()


def match_house_in_spec(user_house: str, spec: str) -> int:
    """
    Match a user's house number against a PDF house specification string (e.g. '1,6-17,15а,19-27').
    Returns a specificity score:
      4 = exact match with suffix (e.g. '15а' == '15а' or '10' == '10')
      3 = exact base number match (e.g. '15а' vs '15', or '8/1' vs '8')
      2 = range match with matching parity on both endpoints (e.g. '25' in '19-27' or '8' in '2-14')
      1 = general range match (e.g. '25' in '24-93')
      0 = no match
    """
    if not user_house or not spec:
        return 0
    u_num, u_suf = _parse_house_token(user_house)
    if u_num is None:
        return 0
    u_raw = f"{u_num}{u_suf}"

    best_score = 0
    tokens = [t.strip() for t in re.split(r"[,;]+", spec) if t.strip()]
    for tok in tokens:
        tok_norm = _normalize_text(tok)
        # Check if range (start-end)
        rm = re.match(r"^(\d+)([а-яіїєґa-z]?)-(\d+)([а-яіїєґa-z]?)$", tok_norm)
        if rm:
            start_n, start_s, end_n, end_s = int(rm.group(1)), rm.group(2), int(rm.group(3)), rm.group(4)
            if start_n == u_num and start_s == u_suf:
                best_score = max(best_score, 4)
            elif end_n == u_num and end_s == u_suf:
                best_score = max(best_score, 4)
            elif start_n <= u_num <= end_n:
                if (start_n % 2 == end_n % 2) and (u_num % 2 == start_n % 2):
                    best_score = max(best_score, 2)
                else:
                    best_score = max(best_score, 1)
        else:
            t_num, t_suf = _parse_house_token(tok_norm)
            if t_num is not None:
                if f"{t_num}{t_suf}" == u_raw:
                    best_score = max(best_score, 4)
                elif t_num == u_num:
                    best_score = max(best_score, 3)

    return best_score


# ---------------------------------------------------------------------------
# AddressIndex Service
# ---------------------------------------------------------------------------

class AddressIndex:
    """Fast local/in-memory Vinnytsia & Vinnytsia Oblast address-to-queue index."""

    def __init__(self, index_path: Path | str = DEFAULT_INDEX_PATH) -> None:
        self.index_path = Path(index_path)
        self.data: dict[str, Any] = {"cities": []}
        self._cities_by_id: dict[str, dict[str, Any]] = {}
        self._streets_by_id: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.load()

    def load(self) -> bool:
        if not self.index_path.exists():
            return False
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            self._rebuild_lookups()
            return True
        except Exception as exc:
            logger.warning("Failed to load address index from %s: %s", self.index_path, exc)
            return False

    def _rebuild_lookups(self) -> None:
        self._cities_by_id.clear()
        self._streets_by_id.clear()
        for city in self.data.get("cities", []):
            cid = city["id"]
            self._cities_by_id[cid] = city
            for st in city.get("streets", []):
                self._streets_by_id[st["id"]] = (city, st)

    def get_popular_cities(self, limit: int = 10) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for city in self.data.get("cities", []):
            if city["name"] in POPULAR_CITY_NAMES:
                results.append(
                    {
                        "id": city["id"],
                        "name": city["name"],
                        "queues": city.get("queues", []),
                        "has_streets": bool(city.get("streets")),
                    }
                )
            if len(results) >= limit:
                break
        return results

    def get_city(self, city_id: str) -> dict[str, Any] | None:
        return self._cities_by_id.get(str(city_id))

    def get_street(self, street_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return self._streets_by_id.get(str(street_id), (None, None))

    def search_cities(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        q_clean = _strip_city_prefix(query)
        if not q_clean:
            return self.get_popular_cities(limit=limit)

        scored: list[tuple[int, int, dict[str, Any]]] = []
        for idx, city in enumerate(self.data.get("cities", [])):
            c_bare = _strip_city_prefix(city["name"])
            c_full = _normalize_text(city["name"])
            if c_bare == q_clean or c_full == _normalize_text(query):
                score = 100
            elif c_bare.startswith(q_clean):
                score = 80
            elif q_clean in c_bare or q_clean in c_full:
                score = 60
            else:
                continue
            scored.append((-score, idx, city))

        scored.sort(key=lambda x: (x[0], x[1]))
        return [
            {
                "id": c["id"],
                "name": c["name"],
                "queues": c.get("queues", []),
                "has_streets": bool(c.get("streets")),
            }
            for _, _, c in scored[:limit]
        ]

    def search_streets(self, city_id: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
        city = self.get_city(city_id)
        if not city:
            return []

        q_clean = _strip_street_prefix(query)
        q_clean = STREET_SYNONYMS.get(q_clean, q_clean)
        q_no_init = _strip_street_initials(q_clean)
        q_canon = get_canonical_street_key(query)

        streets = city.get("streets", [])
        if not q_clean:
            return [
                {
                    "id": st["id"],
                    "name": st["name"],
                    "queues": sorted({r["queue"] for r in st.get("records", [])}),
                }
                for st in streets[:limit]
            ]

        scored: list[tuple[int, str, dict[str, Any]]] = []
        for st in streets:
            s_clean = _strip_street_prefix(st["name"])
            s_clean = STREET_SYNONYMS.get(s_clean, s_clean)
            s_no_init = _strip_street_initials(s_clean)
            s_canon = get_canonical_street_key(st["name"])
            aliases_clean = [_strip_street_prefix(a) for a in st.get("aliases", [])]

            # Avoid matching suburb-prefixed entries in м. Вінниця when direct city street matches exist
            is_suburb_alias = "," in st["name"] and st["name"].startswith(("с.", "смт."))
            penalty = 15 if is_suburb_alias else 0

            if q_canon and s_canon == q_canon:
                score = 100 - penalty
            elif s_clean == q_clean or s_no_init == q_no_init or any(a == q_clean for a in aliases_clean):
                score = 100 - penalty
            elif s_no_init.startswith(q_no_init) or s_clean.startswith(q_clean) or any(a.startswith(q_clean) for a in aliases_clean):
                score = 85 - penalty
            elif re.search(rf"(?:^|[\s.\-]){re.escape(q_no_init)}", s_clean) or any(re.search(rf"(?:^|[\s.\-]){re.escape(q_no_init)}", a) for a in aliases_clean):
                score = 75 - penalty
            elif q_no_init in s_clean or any(q_clean in a or q_no_init in a for a in aliases_clean):
                score = 60 - penalty
            else:
                continue

            scored.append((-score, st["name"], st))

        scored.sort(key=lambda x: (x[0], x[1]))
        return [
            {
                "id": st["id"],
                "name": st["name"],
                "queues": sorted({r["queue"] for r in st.get("records", [])}),
            }
            for _, _, st in scored[:limit]
        ]

    def search_houses(self, street_id: str, query: str) -> list[dict[str, Any]]:
        """
        Resolve house number against a street's records and return matching options with queue codes.
        Prefers exact/range matches, then adjacent parity neighbors, and excludes disjoint-only queues.
        """
        city, st = self.get_street(street_id)
        if not st:
            return []

        q_house = query.strip()
        records = st.get("records", [])
        u_num, u_suf = _parse_house_token(q_house)

        # 0. Check verified address overrides from disconnection.voe.com.ua
        if city and u_num is not None:
            c_bare = _strip_city_prefix(city["name"])
            st_bare = _strip_street_prefix(st["name"])
            norm_h = f"{u_num}{u_suf}"
            for (ov_c, ov_st, ov_h), ov_q in KNOWN_ADDRESS_OVERRIDES.items():
                if ov_c == c_bare and ov_h == norm_h:
                    if ov_st in (st_bare, _strip_street_initials(st_bare)) or any(
                        ov_st == _strip_street_prefix(a) for a in st.get("aliases", [])
                    ):
                        return [
                            {
                                "id": f"{street_id}_h_{q_house}_{ov_q}",
                                "name": f"буд. {q_house} (черга {ov_q})",
                                "house": q_house,
                                "queue": ov_q,
                                "score": 4,
                                "matched_ranges": ["disconnection.voe.com.ua"],
                            }
                        ]

        # 1. Direct match in explicit house ranges (score 1..4)
        scored_queues: dict[str, tuple[int, list[str]]] = {}
        for rec in records:
            h_spec = rec.get("houses", "")
            q_code = rec.get("queue", "")
            if not q_code or not h_spec:
                continue
            score = match_house_in_spec(q_house, h_spec)
            if score > 0:
                prev_score, prev_specs = scored_queues.get(q_code, (0, []))
                if h_spec not in prev_specs:
                    prev_specs.append(h_spec)
                scored_queues[q_code] = (max(prev_score, score), prev_specs)

        # If high-confidence direct match exists (score >= 3, exact number match), return only them!
        if scored_queues:
            max_score = max(s for s, _ in scored_queues.values())
            if max_score >= 3:
                filtered = {q: v for q, v in scored_queues.items() if v[0] >= 3}
            else:
                filtered = scored_queues

            sorted_q = sorted(filtered.items(), key=lambda x: (-x[1][0], x[0]))
            return [
                {
                    "id": f"{street_id}_h_{q_house}_{q_code}",
                    "name": f"буд. {q_house} (черга {q_code})",
                    "house": q_house,
                    "queue": q_code,
                    "score": sc,
                    "matched_ranges": specs,
                }
                for q_code, (sc, specs) in sorted_q
            ]

        # 2. No direct range match: evaluate physical proximity and general coverage
        general_queues = sorted({r["queue"] for r in records if not r.get("houses") and r.get("queue")})
        has_any_houses = any(bool(r.get("houses")) for r in records)

        prox_scores: dict[str, tuple[int, list[str]]] = {}
        if u_num is not None:
            for rec in records:
                h_spec = rec.get("houses", "")
                q_code = rec.get("queue", "")
                if not q_code or not h_spec:
                    continue
                ps, p_desc = score_house_proximity(u_num, h_spec)
                if ps > 0:
                    prev_ps, prev_descs = prox_scores.get(q_code, (0, []))
                    if p_desc and p_desc not in prev_descs:
                        prev_descs.append(p_desc)
                    prox_scores[q_code] = (max(prev_ps, ps), prev_descs)

        # High proximity match (score >= 3: adjacent neighbor on same parity):
        # Only use single-winner proximity when the street is NOT split across multiple general queues.
        # If a street has multiple general coverage queues (e.g. both 3.2 and 4.2), an isolated house number
        # on another substation must not silently suppress the other valid general queue!
        if len(general_queues) <= 1 and any(s >= 3 for s, _ in prox_scores.values()):
            top_prox = {q: v for q, v in prox_scores.items() if v[0] >= 3}
            sorted_p = sorted(top_prox.items(), key=lambda x: (-x[1][0], x[0]))
            return [
                {
                    "id": f"{street_id}_h_{q_house}_{q_code}",
                    "name": f"буд. {q_house} (черга {q_code})",
                    "house": q_house,
                    "queue": q_code,
                    "score": sc,
                    "matched_ranges": descs,
                }
                for q_code, (sc, descs) in sorted_p
            ]

        # Eligible fallback queues:
        # If street has general whole-street records, only those queues are fallback eligible!
        # Queues with strictly disjoint house lists (e.g. 6.2 on Poryka) are EXCLUDED.
        if general_queues:
            fallback_queues = general_queues
        elif not has_any_houses:
            fallback_queues = sorted({r["queue"] for r in records if r.get("queue")})
        else:
            # All queues had explicit house lists but none matched; include queues with proximity >= 2
            fallback_queues = sorted([q for q, (s, _) in prox_scores.items() if s >= 2])
            if not fallback_queues:
                fallback_queues = sorted({r["queue"] for r in records if r.get("queue")})

        if len(fallback_queues) == 1:
            q_code = fallback_queues[0]
            return [
                {
                    "id": f"{street_id}_h_{q_house}_{q_code}",
                    "name": f"буд. {q_house} (черга {q_code})",
                    "house": q_house,
                    "queue": q_code,
                    "score": 1,
                    "matched_ranges": [],
                }
            ]

        return [
            {
                "id": f"{street_id}_h_{q_house}_{q_code}",
                "name": f"буд. {q_house} — черга {q_code}" + (
                    f" ({', '.join(r['houses'] for r in records if r['queue'] == q_code and r.get('houses'))[:35]})"
                    if any(r.get("houses") for r in records if r["queue"] == q_code)
                    else ""
                ),
                "house": q_house,
                "queue": q_code,
                "score": prox_scores.get(q_code, (0, []))[0],
                "matched_ranges": [r["houses"] for r in records if r["queue"] == q_code and r.get("houses")],
            }
            for q_code in fallback_queues
        ]

    def parse_and_search_freeform(self, text: str, limit: int = 8) -> list[dict[str, Any]]:
        """
        Parse a single-line free-form address query such as:
          - 'Вінниця, Соборна 24'
          - 'Соборна 81'
          - 'Келецька 120'
          - 'Агрономічне Мічуріна 5'
          - 'Жмеринка Київська'
        and return matching resolved address candidates.
        """
        raw = text.strip(" ,;.")
        if not raw:
            return []

        # Extract trailing house number if present (e.g. 'Соборна 24' or 'Соборна, буд. 24а' or '600-річчя 18')
        house_part = ""
        body_part = raw
        hm = re.search(
            r"(?:[,\s]+(?:буд\.?|будинок|№)?\s*|\s+)(\d+[а-яіїєґА-ЯІЇЄҐa-zA-Z]?(?:/\d+[а-яіїєґА-ЯІЇЄҐa-zA-Z]?)?)$",
            raw,
        )
        if hm:
            # Make sure we didn't match "600-річчя" or similar street name without a house number
            prefix_before_num = raw[: hm.start()].strip(" ,;.")
            if prefix_before_num:
                house_part = hm.group(1).strip()
                body_part = prefix_before_num

        # Try splitting body_part into (city, street)
        candidates: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

        if "," in body_part:
            parts = [p.strip() for p in body_part.split(",", 1)]
            city_matches = self.search_cities(parts[0], limit=3)
            street_query = parts[1]
            for cm in city_matches:
                for sm in self.search_streets(cm["id"], street_query, limit=4):
                    _, st_obj = self.get_street(sm["id"])
                    c_obj = self.get_city(cm["id"])
                    if c_obj and st_obj:
                        candidates.append((c_obj, st_obj))

        if not candidates:
            # Try matching first word(s) as city name + rest as street name
            words = body_part.split()
            if len(words) >= 2:
                for split_at in (1, 2):
                    if len(words) <= split_at:
                        continue
                    c_query = " ".join(words[:split_at])
                    s_query = " ".join(words[split_at:])
                    if _normalize_text(c_query) in (
                        "вул", "вул.", "вулиця", "пров", "пров.", "провулок",
                        "просп", "просп.", "пр", "пр.", "м", "м.", "с", "с.",
                        "смт", "смт.", "селище", "с-ще", "с-ще.", "х", "х.",
                    ):
                        continue
                    c_matches = [
                        cm for cm in self.search_cities(c_query, limit=3)
                        if _strip_city_prefix(cm["name"]).startswith(_strip_city_prefix(c_query))
                    ]
                    for cm in c_matches:
                        for sm in self.search_streets(cm["id"], s_query, limit=4):
                            _, st_obj = self.get_street(sm["id"])
                            c_obj = self.get_city(cm["id"])
                            if c_obj and st_obj:
                                candidates.append((c_obj, st_obj))
                    if candidates:
                        break

        if not candidates and not body_part.lower().startswith(("с.", "смт.", "м.", "селище", "х.")):
            # Search as a street in м. Вінниця first (most common single-line search like "Соборна 24" or "Келецька 61")
            vin_cities = self.search_cities("Вінниця", limit=1)
            if vin_cities:
                vin_id = vin_cities[0]["id"]
                for sm in self.search_streets(vin_id, body_part, limit=5):
                    _, st_obj = self.get_street(sm["id"])
                    c_obj = self.get_city(vin_id)
                    if c_obj and st_obj:
                        candidates.append((c_obj, st_obj))

        if not candidates and not house_part:
            # Maybe the user typed just a settlement name (e.g., "с. Побережне")
            for cm in self.search_cities(body_part, limit=5):
                c_obj = self.get_city(cm["id"])
                if c_obj and c_obj.get("queues"):
                    candidates.append((c_obj, None))

        results: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, str, str]] = set()

        for c_obj, st_obj in candidates:
            if st_obj is None:
                for q_code in c_obj.get("queues", []):
                    key = (c_obj["id"], "", "", q_code)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        results.append(
                            {
                                "city_id": c_obj["id"],
                                "city_name": c_obj["name"],
                                "street_id": None,
                                "street_name": None,
                                "house": None,
                                "queue": q_code,
                                "score": 2 if len(c_obj.get("queues", [])) == 1 else 0,
                                "display": c_obj["name"],
                            }
                        )
                continue

            if house_part:
                h_matches = self.search_houses(st_obj["id"], house_part)
                for hm_item in h_matches:
                    q_code = hm_item["queue"]
                    key = (c_obj["id"], st_obj["id"], house_part, q_code)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        results.append(
                            {
                                "city_id": c_obj["id"],
                                "city_name": c_obj["name"],
                                "street_id": st_obj["id"],
                                "street_name": st_obj["name"],
                                "house": house_part,
                                "queue": q_code,
                                "score": hm_item.get("score", 0),
                                "display": f"{c_obj['name']}, {st_obj['name']}, {house_part}",
                            }
                        )
            else:
                st_queues = sorted({r["queue"] for r in st_obj.get("records", []) if r.get("queue")})
                for q_code in st_queues:
                    key = (c_obj["id"], st_obj["id"], "", q_code)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        results.append(
                            {
                                "city_id": c_obj["id"],
                                "city_name": c_obj["name"],
                                "street_id": st_obj["id"],
                                "street_name": st_obj["name"],
                                "house": None,
                                "queue": q_code,
                                "score": 1 if len(st_queues) == 1 else 0,
                                "display": f"{c_obj['name']}, {st_obj['name']}",
                            }
                        )

            if len(results) >= limit:
                break

        if results:
            results.sort(key=lambda r: (-r.get("score", 0), r.get("display", "")))
            max_score = results[0].get("score", 0)
            if max_score >= 3:
                results = [r for r in results if r.get("score", 0) >= 3]

        return results[:limit]


async def download_and_build_voe_index(output_path: Path | str = DEFAULT_INDEX_PATH) -> dict[str, Any]:
    """
    Fetch the latest official GPV PDF from voe.com.ua (no Cloudflare challenge on main site),
    parse all 34+ pages, and write data/voe_addresses.json.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(VOE_GPV_INFO_URL)
        resp.raise_for_status()
        m = re.search(r'href="([^"]+\.pdf)"', resp.text, re.I)
        if m:
            pdf_path = m.group(1)
            pdf_url = pdf_path if pdf_path.startswith("http") else f"{VOE_BASE_URL}{pdf_path}"
        else:
            pdf_url = f"{VOE_BASE_URL}/sites/default/files/2026-09/gpv-2026-2027-na-sayt.pdf"

        pdf_resp = await client.get(pdf_url)
        pdf_resp.raise_for_status()
        rows = extract_rows_from_gpv_pdf(pdf_resp.content)
        index_data = build_index_from_rows(rows)
        index_data["source_url"] = pdf_url

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    return index_data


def build_voe_index_from_pdf_file(
    pdf_file: Path | str, output_path: Path | str = DEFAULT_INDEX_PATH
) -> dict[str, Any]:
    with open(pdf_file, "rb") as f:
        pdf_bytes = f.read()
    rows = extract_rows_from_gpv_pdf(pdf_bytes)
    index_data = build_index_from_rows(rows)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    return index_data


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        data = build_voe_index_from_pdf_file(sys.argv[1])
        print(f"Built index with {len(data['cities'])} cities -> {DEFAULT_INDEX_PATH}")
    else:
        import asyncio

        data = asyncio.run(download_and_build_voe_index())
        print(f"Downloaded and built index with {len(data['cities'])} cities -> {DEFAULT_INDEX_PATH}")
