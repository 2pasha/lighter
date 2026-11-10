from __future__ import annotations

import io
import logging
import struct
import zlib
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Optional

from app.config import settings
from app.models import DaySchedule, SlotState
from app.services.formatter import MONTHS_UA, WEEKDAYS_UA, format_minutes_human

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageDraw, ImageFont

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
AVATAR_PATH = ASSETS_DIR / "bot_avatar.png"
AVATAR_POSSIBLE_PATH = ASSETS_DIR / "bot_avatar_possible.png"
AVATAR_OUTAGE_PATH = ASSETS_DIR / "bot_avatar_outage.png"

_FONT_CACHE: dict[tuple[str, int, str], Any] = {}
_AVATAR_CACHE: dict[tuple[int, str], tuple[Any, Any]] = {}
_NOTIFICATION_IMAGE_CACHE: dict[tuple[str, Optional[str], int], bytes] = {}
_SCHEDULE_IMAGE_CACHE: dict[tuple[str, str, str, Optional[int], str, int], bytes] = {}
_SCHEDULE_CACHE_MAX_SIZE = 64


def classify_rgb(r: int, g: int, b: int) -> SlotState:
    """
    Класифікує колір пікселя клітинки офіційного зображення @pat_voe_bot:
    - Блакитний / синій ~ RGB(1, 176, 241) або RGB(20, 86, 216): підтверджене відключення (SlotState.OFF)
    - Жовтий / помаранчевий ~ RGB(255, 192, 0) або RGB(255, 202, 58): заплановане відключення (SlotState.POSSIBLE_OFF)
    - Білий / сірий ~ RGB(255, 255, 255) або RGB(241, 244, 248): світло є (SlotState.ON)
    """
    if b > 170 and r < 100 and g > 60:
        return SlotState.OFF
    if r > 200 and 120 < g < 235 and b < 90:
        return SlotState.POSSIBLE_OFF
    if r > 200 and g < 90 and b < 90:
        return SlotState.OFF
    return SlotState.ON


def _decode_png_pure_python(
    data: bytes,
) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """Чистий Python декодер PNG (працює навіть без встановленого Pillow)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Not a valid PNG file")

    pos = 8
    chunks: list[tuple[bytes, bytes]] = []
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        cdata = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        chunks.append((ctype, cdata))

    ihdr = [c for t, c in chunks if t == b"IHDR"][0]
    w, h, bit_depth, color_type, comp, filt, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    if color_type not in (2, 6):
        raise ValueError(f"Unsupported PNG color_type={color_type}")

    idat = b"".join(c for t, c in chunks if t == b"IDAT")
    raw = zlib.decompress(idat)
    bpp = 3 if color_type == 2 else 4
    stride = w * bpp
    pixels: list[list[tuple[int, int, int]]] = [[(255, 255, 255)] * w for _ in range(h)]
    prev_row = [0] * stride
    idx = 0

    for y in range(h):
        ftype = raw[idx]
        idx += 1
        row = list(raw[idx : idx + stride])
        idx += stride
        if ftype == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                row[i] = (row[i] + prev_row[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b_val = prev_row[i]
                row[i] = (row[i] + ((a + b_val) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b_val = prev_row[i]
                c = prev_row[i - bpp] if i >= bpp else 0
                p = a + b_val - c
                pa, pb, pc = abs(p - a), abs(p - b_val), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b_val if pb <= pc else c)
                row[i] = (row[i] + pr) & 0xFF
        prev_row = row
        for x in range(w):
            pixels[y][x] = (row[x * bpp], row[x * bpp + 1], row[x * bpp + 2])

    return w, h, pixels


def parse_voe_schedule_image(
    image_bytes: bytes,
    queue: str = "1.1",
    schedule_date: Optional[date] = None,
) -> DaySchedule:
    """
    Розпізнає зображення графіка з офіційного бота @pat_voe_bot (сітка 6 рядків × 4 стовпчики = 24 години).
    Підтримує як цілі годинні клітинки, так і поділені навпіл (по 30 хвилин).
    """
    target_date = schedule_date or date.today()

    if HAS_PIL:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = im.size

        def get_px(x: int, y: int) -> tuple[int, int, int]:
            return im.getpixel((x, y))  # type: ignore
    else:
        w, h, px_grid = _decode_png_pure_python(image_bytes)

        def get_px(x: int, y: int) -> tuple[int, int, int]:
            return px_grid[y][x]

    grid_x0 = int(w * (29.0 / 540.0))
    grid_x1 = int(w * (510.0 / 540.0))
    grid_y0 = int(h * (140.0 / 714.0))
    grid_y1 = int(h * (557.0 / 714.0))

    col_w = (grid_x1 - grid_x0) / 4.0
    row_h = (grid_y1 - grid_y0) / 6.0

    slots: list[SlotState] = []

    for row_idx in range(6):
        sample_y = int(grid_y0 + row_idx * row_h + row_h * 0.16)

        for col_idx in range(4):
            cell_x0 = grid_x0 + col_idx * col_w
            sample_x_left = int(cell_x0 + col_w * 0.20)
            sample_x_right = int(cell_x0 + col_w * 0.80)

            r_l, g_l, b_l = get_px(sample_x_left, sample_y)
            r_r, g_r, b_r = get_px(sample_x_right, sample_y)

            state_first_half = classify_rgb(r_l, g_l, b_l)
            state_second_half = classify_rgb(r_r, g_r, b_r)

            slots.append(state_first_half)
            slots.append(state_second_half)

    return DaySchedule(
        queue=queue,
        schedule_date=target_date,
        slots=slots,
        source="telegram_bot_image",
    )


def render_schedule_image(
    schedule: DaySchedule,
    scale: int = 2,
    now: Optional[datetime] = None,
) -> Optional[bytes]:
    """
    Генерує картку графіка відключень за дизайном «Прототип · Графік з аватаром» (pen.dev Node ID: f4OWkd).
    Рендерить у підвищеній роздільній здатності (scale=2 -> 1200x1762) для ідеальної чіткості у Telegram.
    Поточна година (якщо дата графіка = сьогодні) виділяється кораловою рамкою #FF8D7D (strokeWidth=3).
    """
    if not HAS_PIL:
        return None

    if now is None:
        now = datetime.now(settings.tz)
    is_today = now.date() == schedule.schedule_date
    current_hour: Optional[int] = now.hour if is_today else None
    current_slot_state: SlotState = (
        schedule.state_at_minute(now.hour * 60 + now.minute)
        if is_today
        else SlotState.ON
    )

    cache_key = (
        schedule.queue,
        schedule.schedule_date.isoformat(),
        schedule.content_hash,
        current_hour,
        current_slot_state.value,
        scale,
    )
    cached = _SCHEDULE_IMAGE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    s = scale
    w, h = 600 * s, 881 * s
    im = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(im)

    # Кольорова палітра з дизайну f4OWkd / pCEiy
    c_text_primary = (32, 33, 36)       # #202124
    c_text_muted = (101, 114, 134)      # #657286
    c_cell_text_on = (83, 97, 118)      # #536176
    c_cell_text_poss = (61, 48, 0)      # #3D3000
    c_white = (255, 255, 255)           # #FFFFFF

    c_blue_primary = (20, 86, 216)      # #1456D8 (Відключення & Картка підсумку)
    c_badge_bg = (237, 243, 255)        # #EDF3FF (Фон бейджа черги)
    c_yellow_poss = (255, 202, 58)      # #FFCA3A (Заплановане відключення)
    c_gray_on = (241, 244, 248)         # #F1F4F8 (Зі світлом)
    c_coral_border = (255, 141, 125)    # #FF8D7D (Поточна година)

    # Шрифти з точними вагами з дизайну f4OWkd
    font_category = _get_font("Inter", 12 * s, "SemiBold")
    font_queue_badge = _get_font("Inter", 14 * s, "SemiBold")
    font_date_title = _get_font("Manrope", 44 * s, "Bold")
    font_day_year = _get_font("Inter", 17 * s, "Regular")
    font_summary_lbl = _get_font("Inter", 16 * s, "Medium")
    font_summary_val = _get_font("Manrope", 26 * s, "Bold")
    font_sec_title = _get_font("Manrope", 21 * s, "Bold")
    font_sec_hint = _get_font("Inter", 13 * s, "Regular")
    font_cell_on = _get_font("GeistMono", 18 * s, "Regular")
    font_cell_off = _get_font("GeistMono", 18 * s, "SemiBold")
    font_legend = _get_font("Inter", 14 * s, "Regular")

    # -------------------------------------------------------------------------
    # 1. Верхній рядок: Аватар бота (48×48) + "ГРАФІК ВІДКЛЮЧЕНЬ" та бейдж "Черга X.Y"
    # -------------------------------------------------------------------------
    _paste_circular_avatar(
        im, x=36 * s, y=36 * s, size=48 * s, state=current_slot_state
    )

    _draw_tracked_text(
        draw,
        (96 * s, int(52.5 * s)),
        "ГРАФІК ВІДКЛЮЧЕНЬ",
        font_category,
        c_text_primary,
        letter_spacing=1.0 * s,
    )

    queue_text = f"Черга {schedule.queue}"
    q_bbox = draw.textbbox((0, 0), queue_text, font=font_queue_badge)
    q_tw = q_bbox[2] - q_bbox[0]
    badge_w = q_tw + 24 * s
    badge_h = 33 * s
    badge_x0 = (600 - 36) * s - badge_w
    badge_y0 = int(43.5 * s)
    draw.rounded_rectangle(
        [badge_x0, badge_y0, badge_x0 + badge_w, badge_y0 + badge_h],
        radius=8 * s,
        fill=c_badge_bg,
    )
    _draw_centered_text(
        draw,
        (badge_x0, badge_y0, badge_x0 + badge_w, badge_y0 + badge_h),
        queue_text,
        font_queue_badge,
        c_blue_primary,
    )

    # -------------------------------------------------------------------------
    # 2. Заголовок дати: "05 листопада" та "Четвер, 2026"
    # -------------------------------------------------------------------------
    d = schedule.schedule_date
    month_name = MONTHS_UA.get(d.month, "")
    weekday_name = WEEKDAYS_UA.get(d.weekday(), "")
    date_title_str = f"{d.day:02d} {month_name}"
    day_year_str = f"{weekday_name}, {d.year}"

    _draw_tracked_text(
        draw,
        (36 * s, 112 * s),
        date_title_str,
        font_date_title,
        c_text_primary,
        letter_spacing=-1.8 * s,
    )
    draw.text(
        (36 * s, 180 * s),
        day_year_str,
        fill=c_text_muted,
        font=font_day_year,
    )

    # -------------------------------------------------------------------------
    # 3. Синя картка підсумку: "Без світла протягом дня" -> "10 год"
    # -------------------------------------------------------------------------
    card_x0, card_y0 = 36 * s, 229 * s
    card_x1, card_y1 = (36 + 528) * s, (229 + 84) * s
    draw.rounded_rectangle(
        [card_x0, card_y0, card_x1, card_y1],
        radius=16 * s,
        fill=c_blue_primary,
    )

    # Текст зліва всередині синьої картки
    summary_lbl = "Без світла протягом дня"
    lbl_bbox = draw.textbbox((0, 0), summary_lbl, font=font_summary_lbl)
    lbl_h = lbl_bbox[3] - lbl_bbox[1]
    lbl_y = card_y0 + (84 * s - lbl_h) / 2 - lbl_bbox[1]
    draw.text((60 * s, lbl_y), summary_lbl, fill=c_white, font=font_summary_lbl)

    # Сумарна тривалість справа всередині синьої картки
    total_str = format_minutes_human(schedule.total_outage_minutes)
    val_bbox = draw.textbbox((0, 0), total_str, font=font_summary_val)
    val_w = val_bbox[2] - val_bbox[0]
    val_h = val_bbox[3] - val_bbox[1]
    val_x = (36 + 528 - 24) * s - val_w - val_bbox[0]
    val_y = card_y0 + (84 * s - val_h) / 2 - val_bbox[1]
    draw.text((val_x, val_y), total_str, fill=c_white, font=font_summary_val)

    # -------------------------------------------------------------------------
    # 4. Секція "Розклад на добу" + "1 блок = 1 година"
    # -------------------------------------------------------------------------
    draw.text(
        (36 * s, 341 * s),
        "Розклад на добу",
        fill=c_text_primary,
        font=font_sec_title,
    )
    hint_str = "1 блок = 1 година"
    h_bbox = draw.textbbox((0, 0), hint_str, font=font_sec_hint)
    h_tw = h_bbox[2] - h_bbox[0]
    draw.text(
        ((36 + 528) * s - h_tw, int(347.5 * s)),
        hint_str,
        fill=c_text_muted,
        font=font_sec_hint,
    )

    # -------------------------------------------------------------------------
    # 5. Сітка 24 годин (6 рядків × 4 стовпчики, клітинка 126×52, gap 8, radius 8)
    # -------------------------------------------------------------------------
    grid_origin_x = 36 * s
    grid_origin_y = 386 * s
    cell_w = 126 * s
    cell_h = 52 * s
    cell_gap = 8 * s
    cell_radius = 8 * s

    def state_fill(st: SlotState) -> tuple[int, int, int]:
        if st == SlotState.OFF:
            return c_blue_primary
        if st == SlotState.POSSIBLE_OFF:
            return c_yellow_poss
        return c_gray_on

    for row_idx in range(6):
        cy0 = grid_origin_y + row_idx * (cell_h + cell_gap)
        cy1 = cy0 + cell_h
        for col_idx in range(4):
            cx0 = grid_origin_x + col_idx * (cell_w + cell_gap)
            cx1 = cx0 + cell_w
            hour = row_idx * 4 + col_idx
            s1, s2 = schedule.hour_state(hour)

            is_current = current_hour == hour
            border_color = c_coral_border if is_current else None
            border_width = 3 * s if is_current else 0

            if s1 == s2:
                draw.rounded_rectangle(
                    [cx0, cy0, cx1, cy1],
                    radius=cell_radius,
                    fill=state_fill(s1),
                    outline=border_color,
                    width=border_width,
                )
            else:
                # Якщо півгодини мають різний статус — малюємо заокруглену клітинку з двома половинами
                _draw_split_rounded_cell(
                    im,
                    (cx0, cy0, cx1, cy1),
                    cell_radius,
                    state_fill(s1),
                    state_fill(s2),
                )
                if is_current:
                    draw.rounded_rectangle(
                        [cx0, cy0, cx1, cy1],
                        radius=cell_radius,
                        outline=c_coral_border,
                        width=border_width,
                    )

            # Визначаємо колір і жирність тексту години
            if s1 == SlotState.OFF or s2 == SlotState.OFF:
                t_color = c_white if s1 == s2 else c_text_primary
                t_font = font_cell_off
            elif s1 == SlotState.POSSIBLE_OFF or s2 == SlotState.POSSIBLE_OFF:
                t_color = c_cell_text_poss
                t_font = font_cell_off
            else:
                t_color = c_cell_text_on
                t_font = font_cell_on

            _draw_centered_text(
                draw, (cx0, cy0, cx1, cy1), f"{hour:02d}:00", t_font, t_color
            )

    # -------------------------------------------------------------------------
    # 6. Умовні позначення (Легенда знизу, Y = 766)
    # -------------------------------------------------------------------------
    legend_items = [
        (c_blue_primary, "Відключення"),
        (c_yellow_poss, "Заплановане відключення"),
        (c_gray_on, "Зі світлом"),
    ]
    legend_start_y = 766 * s
    legend_row_step = 31 * s

    for idx, (swatch_color, label_text) in enumerate(legend_items):
        ly = legend_start_y + idx * legend_row_step
        sw_y0 = ly + int(1.5 * s)
        sw_x0 = 36 * s
        draw.rounded_rectangle(
            [sw_x0, sw_y0, sw_x0 + 14 * s, sw_y0 + 14 * s],
            radius=4 * s,
            fill=swatch_color,
        )
        draw.text(
            ((36 + 24) * s, ly),
            label_text,
            fill=c_cell_text_on,
            font=font_legend,
        )

    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    result_bytes = buf.getvalue()
    if len(_SCHEDULE_IMAGE_CACHE) >= _SCHEDULE_CACHE_MAX_SIZE:
        oldest_key = next(iter(_SCHEDULE_IMAGE_CACHE))
        _SCHEDULE_IMAGE_CACHE.pop(oldest_key, None)
    _SCHEDULE_IMAGE_CACHE[cache_key] = result_bytes
    return result_bytes


NotificationKind = Literal["light_on", "outage_soon", "schedule_updated"]


def render_notification_image(
    kind: NotificationKind,
    slot_state: Optional[SlotState] = None,
    scale: int = 2,
) -> Optional[bytes]:
    """
    Генерує банер-картку сповіщення за дизайном «Прототипи · Повідомлення та аватар» (pen.dev Node ID: TGo7x).
    Підтримує:
    - 'light_on': синя картка (#1456D8), білий бейдж "ЗІ СВІТЛОМ", заголовок "Світло повернулось"
    - 'outage_soon': біла картка (#FFFFFF), динамічний бейдж ("ЗАПЛАНОВАНЕ ВІДКЛЮЧЕННЯ" на #FFCA3A або
      "ПІДТВЕРДЖЕНЕ ВІДКЛЮЧЕННЯ" на #1456D8), заголовок "Скоро відключення"
    - 'schedule_updated': біла картка (#FFFFFF), бейдж "ОНОВЛЕННЯ" на #EDF3FF, заголовок "Графік оновлено"
    """
    if not HAS_PIL:
        return None

    notif_cache_key = (kind, slot_state.value if slot_state else None, scale)
    cached_notif = _NOTIFICATION_IMAGE_CACHE.get(notif_cache_key)
    if cached_notif is not None:
        return cached_notif

    s = scale
    w, h = 600 * s, 208 * s

    c_blue_primary = (20, 86, 216)      # #1456D8
    c_white = (255, 255, 255)           # #FFFFFF
    c_text_dark = (32, 33, 36)          # #202124
    c_text_muted = (101, 114, 134)      # #657286
    c_text_blue_sub = (220, 231, 255)   # #DCE7FF
    c_yellow_poss = (255, 202, 58)      # #FFCA3A
    c_text_poss = (61, 48, 0)           # #3D3000
    c_badge_light_blue = (237, 243, 255)  # #EDF3FF

    if kind == "light_on":
        card_bg = c_blue_primary
        badge_bg = c_white
        badge_fg = c_blue_primary
        badge_text = "ЗІ СВІТЛОМ"
        title_text = "Світло повернулось"
        title_fg = c_white
        desc_text = "Можна повертатися до своїх справ."
        desc_fg = c_text_blue_sub
    elif kind == "outage_soon":
        card_bg = c_white
        if slot_state == SlotState.OFF:
            badge_bg = c_blue_primary
            badge_fg = c_white
            badge_text = "ПІДТВЕРДЖЕНЕ ВІДКЛЮЧЕННЯ"
        else:
            badge_bg = c_yellow_poss
            badge_fg = c_text_poss
            badge_text = "ЗАПЛАНОВАНЕ ВІДКЛЮЧЕННЯ"
        title_text = "Скоро відключення"
        title_fg = c_text_dark
        desc_text = "Збережіть роботу та зарядіть пристрої."
        desc_fg = c_text_muted
    else:
        card_bg = c_white
        badge_bg = c_badge_light_blue
        badge_fg = c_blue_primary
        badge_text = "ОНОВЛЕННЯ"
        title_text = "Графік оновлено"
        title_fg = c_text_dark
        desc_text = "Перевірте розклад для своєї черги."
        desc_fg = c_text_muted

    im = Image.new("RGB", (w, h), card_bg)
    draw = ImageDraw.Draw(im)

    font_badge = _get_font("Inter", 10 * s, "Bold")
    font_title = _get_font("Manrope", 30 * s, "Bold")
    font_desc = _get_font("Inter", 16 * s, "Regular")

    # 1. Круглий аватар бота зліва зверху (64×64, x=28, y=28) відповідно до стану
    if kind == "outage_soon":
        notif_avatar_state = (
            SlotState.OFF if slot_state == SlotState.OFF else SlotState.POSSIBLE_OFF
        )
    else:
        notif_avatar_state = SlotState.ON
    _paste_circular_avatar(
        im, x=28 * s, y=28 * s, size=64 * s, state=notif_avatar_state
    )

    # 2. Бейдж типу повідомлення справа зверху (y=47, height=26, radius=6, padding=[7, 10])
    badge_letter_spacing = 0.4 * s
    badge_tw = _measure_tracked_text_width(draw, badge_text, font_badge, badge_letter_spacing)
    badge_w = int(round(badge_tw + 20 * s))
    badge_h = 26 * s
    badge_x0 = (600 - 28) * s - badge_w
    badge_y0 = 47 * s
    draw.rounded_rectangle(
        [badge_x0, badge_y0, badge_x0 + badge_w, badge_y0 + badge_h],
        radius=6 * s,
        fill=badge_bg,
    )
    b_bbox = draw.textbbox((0, 0), badge_text, font=font_badge)
    b_th = b_bbox[3] - b_bbox[1]
    b_ty = badge_y0 + (badge_h - b_th) / 2 - b_bbox[1]
    b_tx = badge_x0 + (badge_w - badge_tw) / 2
    _draw_tracked_text(
        draw,
        (int(round(b_tx)), int(round(b_ty))),
        badge_text,
        font_badge,
        badge_fg,
        letter_spacing=badge_letter_spacing,
    )

    # 3. Заголовок повідомлення (x=28, y=112, Manrope 30 Bold, letterSpacing=-0.8)
    _draw_tracked_text(
        draw,
        (28 * s, 112 * s),
        title_text,
        font_title,
        title_fg,
        letter_spacing=-0.8 * s,
    )

    # 4. Опис повідомлення (x=28, y=161, Inter 16 Regular)
    draw.text(
        (28 * s, 161 * s),
        desc_text,
        fill=desc_fg,
        font=font_desc,
    )

    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    result_bytes = buf.getvalue()
    _NOTIFICATION_IMAGE_CACHE[notif_cache_key] = result_bytes
    return result_bytes


def _get_avatar_path_for_state(state: SlotState) -> Path:
    if state == SlotState.OFF and AVATAR_OUTAGE_PATH.exists():
        return AVATAR_OUTAGE_PATH
    if state == SlotState.POSSIBLE_OFF and AVATAR_POSSIBLE_PATH.exists():
        return AVATAR_POSSIBLE_PATH
    return AVATAR_PATH


def _paste_circular_avatar(
    base_im: Image.Image,
    x: int,
    y: int,
    size: int,
    state: SlotState = SlotState.ON,
) -> None:
    """Малює круглий аватар бота у заданій позиції (x, y) з розміром size×size залежно від стану."""
    cache_key = (size, state.value)
    cached_pair = _AVATAR_CACHE.get(cache_key)
    if cached_pair is not None:
        white_bg, mask = cached_pair
        base_im.paste(white_bg, (x, y), mask)
        return

    resample = getattr(Image, "Resampling", Image).LANCZOS
    target_path = _get_avatar_path_for_state(state)
    if target_path.exists():
        try:
            av = Image.open(target_path).convert("RGBA")
            av = av.resize((size, size), resample=resample)
        except Exception as exc:
            logger.debug("Could not load avatar %s: %s", target_path, exc)
            av = _draw_fallback_avatar(size)
    else:
        av = _draw_fallback_avatar(size)

    # Кругла антиаліасна маска (supersampled 4x)
    ss = 4
    mask_large = Image.new("L", (size * ss, size * ss), 0)
    mask_draw = ImageDraw.Draw(mask_large)
    mask_draw.ellipse([0, 0, size * ss - 1, size * ss - 1], fill=255)
    mask = mask_large.resize((size, size), resample=resample)

    # Накладаємо на білу підкладку перед вставкою з круглою маскою
    white_bg = Image.new("RGB", (size, size), (255, 255, 255))
    white_bg.paste(av.convert("RGB"), (0, 0), av.split()[3] if av.mode == "RGBA" else None)
    _AVATAR_CACHE[cache_key] = (white_bg, mask)
    base_im.paste(white_bg, (x, y), mask)


def _draw_fallback_avatar(size: int) -> Image.Image:
    """Резервне векторне малювання маскота бота на випадок відсутності PNG-файлу."""
    im = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    draw = ImageDraw.Draw(im)
    black = (0, 0, 0, 255)
    lw = max(2, int(size * 0.045))
    # Очі
    draw.ellipse(
        [int(size * 0.08), int(size * 0.28), int(size * 0.42), int(size * 0.62)],
        outline=black,
        width=lw,
    )
    draw.ellipse(
        [int(size * 0.58), int(size * 0.28), int(size * 0.92), int(size * 0.62)],
        outline=black,
        width=lw,
    )
    # Зіниці
    draw.ellipse(
        [int(size * 0.17), int(size * 0.40), int(size * 0.26), int(size * 0.49)],
        fill=black,
    )
    draw.ellipse(
        [int(size * 0.75), int(size * 0.40), int(size * 0.84), int(size * 0.49)],
        fill=black,
    )
    # Посмішка
    draw.arc(
        [int(size * 0.43), int(size * 0.62), int(size * 0.57), int(size * 0.72)],
        start=15,
        end=165,
        fill=black,
        width=lw,
    )
    return im


def _measure_tracked_text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    letter_spacing: float = 0.0,
) -> float:
    if not text:
        return 0.0
    total = 0.0
    for idx, ch in enumerate(text):
        total += draw.textlength(ch, font=font)
        if idx < len(text) - 1:
            total += letter_spacing
    return total


def _draw_split_rounded_cell(
    base_im: Image.Image,
    box: tuple[int, int, int, int],
    radius: int,
    left_color: tuple[int, int, int],
    right_color: tuple[int, int, int],
) -> None:
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    cell_img = Image.new("RGB", (w, h), left_color)
    cell_draw = ImageDraw.Draw(cell_img)
    cell_draw.rectangle([w // 2, 0, w, h], fill=right_color)

    mask = Image.new("L", (w, h), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    base_im.paste(cell_img, (x0, y0), mask)


def _get_font(
    family: str, size: int, weight_name: str = "Regular"
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    cache_key = (family, size, weight_name)
    cached_font = _FONT_CACHE.get(cache_key)
    if cached_font is not None:
        return cached_font

    file_map = {
        "Manrope": FONTS_DIR / "Manrope-Variable.ttf",
        "Inter": FONTS_DIR / "Inter-Variable.ttf",
        "GeistMono": FONTS_DIR / "GeistMono-Variable.ttf",
    }
    ttf_path = file_map.get(family)
    if ttf_path and ttf_path.exists():
        try:
            f = ImageFont.truetype(str(ttf_path), size=size)
            try:
                f.set_variation_by_name(weight_name)
            except Exception:
                weight_num = {
                    "Regular": 400,
                    "Medium": 500,
                    "SemiBold": 600,
                    "Bold": 700,
                }.get(weight_name, 400)
                try:
                    axes = f.get_variation_axes()
                    if len(axes) == 2:
                        f.set_variation_by_axes([14, weight_num])
                    elif len(axes) == 1:
                        f.set_variation_by_axes([weight_num])
                except Exception:
                    pass
            _FONT_CACHE[cache_key] = f
            return f
        except Exception as exc:
            logger.debug("Could not load bundled font %s: %s", ttf_path, exc)

    # Fallback на системні шрифти
    is_bold = weight_name in ("SemiBold", "Bold")
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if is_bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if is_bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            f = ImageFont.truetype(path, size=size)
            _FONT_CACHE[cache_key] = f
            return f
        except Exception:
            continue
    default_f = ImageFont.load_default()
    _FONT_CACHE[cache_key] = default_f
    return default_f


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    x0, y0, x1, y1 = box
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = x0 + (x1 - x0 - tw) / 2 - bbox[0]
    ty = y0 + (y1 - y0 - th) / 2 - bbox[1]
    draw.text((tx, ty), text, fill=fill, font=font)


def _draw_tracked_text(
    draw: ImageDraw.ImageDraw,
    pos: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int],
    letter_spacing: float = 0.0,
) -> None:
    if abs(letter_spacing) < 0.05:
        draw.text(pos, text, fill=fill, font=font)
        return
    x, y = float(pos[0]), float(pos[1])
    for ch in text:
        draw.text((x, y), ch, fill=fill, font=font)
        adv = draw.textlength(ch, font=font)
        x += adv + letter_spacing
