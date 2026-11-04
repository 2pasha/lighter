from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from app.models import DaySchedule, OutageInterval, SlotState

MONTHS_UA = {
    1: "січня",
    2: "лютого",
    3: "березня",
    4: "квітня",
    5: "травня",
    6: "червня",
    7: "липня",
    8: "серпня",
    9: "вересня",
    10: "жовтня",
    11: "листопада",
    12: "грудня",
}

WEEKDAYS_UA = {
    0: "Понеділок",
    1: "Вівторок",
    2: "Середа",
    3: "Четвер",
    4: "П'ятниця",
    5: "Субота",
    6: "Неділя",
}


def format_date_ua(d: date) -> str:
    weekday = WEEKDAYS_UA.get(d.weekday(), "")
    month = MONTHS_UA.get(d.month, "")
    return f"{d.day} {month} ({weekday}, {d.strftime('%d.%m.%Y')})"


def format_minutes_human(total_minutes: int) -> str:
    hours, mins = divmod(max(0, total_minutes), 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours} год")
    if mins > 0:
        parts.append(f"{mins} хв")
    return " ".join(parts) if parts else "0 хв"


def format_schedule_message(
    schedule: DaySchedule,
    location_label: Optional[str] = None,
    address_str: Optional[str] = None,
    now: Optional[datetime] = None,
    header_prefix: Optional[str] = None,
) -> str:
    """
    Формує детальне, легко читабельне повідомлення з графіком відключень українською мовою.
    """
    lines: list[str] = []

    if header_prefix:
        lines.append(header_prefix)
        lines.append("")

    loc_title = f"📍 <b>{location_label}</b>" if location_label else f"⚡ <b>Черга {schedule.queue}</b>"
    if address_str and address_str != f"Черга {schedule.queue}":
        loc_title += f" ({address_str})"
    lines.append(loc_title)
    lines.append(f"🔢 Черга: <b>{schedule.queue}</b>")
    lines.append(f"📅 Дата: <b>{format_date_ua(schedule.schedule_date)}</b>")
    lines.append("")

    # Якщо це графік на сьогодні, показуємо поточний статус світла просто зараз
    if now and now.date() == schedule.schedule_date:
        cur_min = now.hour * 60 + now.minute
        cur_state = schedule.state_at_minute(cur_min)
        next_change_str = _describe_next_transition(schedule, cur_min)
        lines.append(f"⏱ <b>Статус зараз ({now.strftime('%H:%M')}):</b> {cur_state.emoji} {cur_state.label_ua}")
        if next_change_str:
            lines.append(f"   └ {next_change_str}")
        lines.append("")

    if schedule.total_outage_minutes == 0:
        lines.append("🎉 <b>Відключень не заплановано! Світло має бути цілу добу.</b>")
        lines.append("")

    total_off = format_minutes_human(schedule.total_outage_minutes)
    conf_off = format_minutes_human(schedule.confirmed_outage_minutes)
    poss_off = format_minutes_human(schedule.possible_outage_minutes)
    lines.append(
        f"📊 <b>Всього без світла:</b> {total_off}\n"
        f"🔴 підтверджених: {conf_off}\n"
        f"🟡 можливих: {poss_off}"
    )
    return "\n".join(lines)


def has_meaningful_outage_changes(
    old_sched: DaySchedule,
    new_sched: DaySchedule,
    now: Optional[datetime] = None,
) -> bool:
    """
    Перевіряє, чи змінилися фактичні години відключень (🟢 ↔ 🔴/🟡)
    у поточному або майбутніх слотах (ігноруючи минулі години сьогоднішнього дня
    та переходи лише між 🟡 і 🔴).
    """
    start_slot = _get_comparison_start_slot(new_sched.schedule_date, now)
    for idx in range(start_slot, 48):
        if old_sched.slots[idx].is_outage != new_sched.slots[idx].is_outage:
            return True
    return False


def format_schedule_diff(
    old_sched: DaySchedule,
    new_sched: DaySchedule,
    now: Optional[datetime] = None,
) -> str:
    """
    Формує зрозумілий опис змін у розкладі (лише актуальні/майбутні години,
    чітко показуючи додані та скасовані інтервали відключень).
    """
    start_slot = _get_comparison_start_slot(new_sched.schedule_date, now)

    added_slots = [False] * 48
    removed_slots = [False] * 48
    added_mins = 0
    removed_mins = 0

    for idx in range(start_slot, 48):
        was_off = old_sched.slots[idx].is_outage
        now_off = new_sched.slots[idx].is_outage
        if not was_off and now_off:
            added_slots[idx] = True
            added_mins += 30
        elif was_off and not now_off:
            removed_slots[idx] = True
            removed_mins += 30

    added_ranges = _bool_slots_to_ranges(added_slots)
    removed_ranges = _bool_slots_to_ranges(removed_slots)

    diff_lines: list[str] = []
    if removed_ranges:
        diff_lines.append(
            "🟢 <b>Скасовано відключення (буде світло):</b> " + ", ".join(removed_ranges)
        )
    if added_ranges:
        diff_lines.append(
            "🔴 <b>Додано відключення:</b> " + ", ".join(added_ranges)
        )

    delta = added_mins - removed_mins
    if delta > 0:
        diff_lines.append(
            f"📈 Час без світла збільшився на <b>{format_minutes_human(delta)}</b>"
        )
    elif delta < 0:
        diff_lines.append(
            f"📉 Час без світла зменшився на <b>{format_minutes_human(-delta)}</b> 🎉"
        )

    return "\n".join(diff_lines)


def _get_comparison_start_slot(
    schedule_date: date, now: Optional[datetime]
) -> int:
    if now and now.date() == schedule_date:
        cur_min = now.hour * 60 + now.minute
        return max(0, min(47, cur_min // 30))
    return 0


def _bool_slots_to_ranges(flags: list[bool]) -> list[str]:
    ranges: list[str] = []
    start_idx: Optional[int] = None
    for idx, active in enumerate(flags):
        if active and start_idx is None:
            start_idx = idx
        elif not active and start_idx is not None:
            ranges.append(_slot_range_str(start_idx, idx))
            start_idx = None
    if start_idx is not None:
        ranges.append(_slot_range_str(start_idx, 48))
    return ranges


def _slot_range_str(start_idx: int, end_idx: int) -> str:
    sh, sm = divmod(start_idx * 30, 60)
    if end_idx == 48:
        end_s = "24:00"
    else:
        eh, em = divmod(end_idx * 30, 60)
        end_s = f"{eh:02d}:{em:02d}"
    dur = format_minutes_human((end_idx - start_idx) * 30)
    return f"<b>{sh:02d}:{sm:02d}–{end_s}</b> ({dur})"


def _describe_next_transition(schedule: DaySchedule, cur_minute: int) -> str:
    cur_slot_idx = max(0, min(47, cur_minute // 30))
    cur_is_outage = schedule.slots[cur_slot_idx].is_outage

    for idx in range(cur_slot_idx + 1, 48):
        slot_state = schedule.slots[idx]
        if slot_state.is_outage != cur_is_outage:
            target_min = idx * 30
            diff = target_min - cur_minute
            h, m = divmod(target_min, 60)
            time_str = f"{h:02d}:{m:02d}"
            if cur_is_outage:
                return f"💡 Очікується відновлення о <b>{time_str}</b> (через {format_minutes_human(diff)})"
            else:
                return f"⚠️ Наступне відключення о <b>{time_str}</b> ({slot_state.emoji}, через {format_minutes_human(diff)})"

    if cur_is_outage:
        diff = 1440 - cur_minute
        return f"💡 До кінця доби відключення триває (ще {format_minutes_human(diff)})"
    return "✅ До кінця доби відключень більше немає"
