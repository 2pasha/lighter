from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Optional


class SlotState(str, Enum):
    """Статус наявності електроенергії у конкретний часовий слот."""

    ON = "on"  # 🟢 Світло є (біла клітинка)
    OFF = "off"  # 🔴 Відключення (синя клітинка на зображенні / підтверджене)
    POSSIBLE_OFF = "possible_off"  # 🟡 Заплановане / можливе відключення (жовта клітинка)

    @property
    def is_outage(self) -> bool:
        """Чи вважається слот відключенням (підтвердженим або можливим)."""
        return self in (SlotState.OFF, SlotState.POSSIBLE_OFF)

    @property
    def emoji(self) -> str:
        if self == SlotState.ON:
            return "🟢"
        if self == SlotState.OFF:
            return "🔴"
        return "🟡"

    @property
    def label_ua(self) -> str:
        if self == SlotState.ON:
            return "Світло є"
        if self == SlotState.OFF:
            return "Відключення"
        return "Заплановане (можливе) відключення"


@dataclass(frozen=True)
class OutageInterval:
    """Неперервний інтервал відключення або наявності світла протягом доби."""

    start_minute: int  # 0 .. 1410 (кратно 30 хв, наприклад 480 = 08:00)
    end_minute: int    # 30 .. 1440 (наприклад 720 = 12:00)
    state: SlotState   # OFF або POSSIBLE_OFF (або ON)
    has_confirmed: bool = False
    has_possible: bool = False

    @property
    def start_str(self) -> str:
        h, m = divmod(self.start_minute, 60)
        return f"{h:02d}:{m:02d}"

    @property
    def end_str(self) -> str:
        if self.end_minute == 1440:
            return "24:00"
        h, m = divmod(self.end_minute, 60)
        return f"{h:02d}:{m:02d}"

    @property
    def duration_minutes(self) -> int:
        return self.end_minute - self.start_minute

    @property
    def duration_str(self) -> str:
        hours, mins = divmod(self.duration_minutes, 60)
        parts = []
        if hours:
            parts.append(f"{hours} год")
        if mins:
            parts.append(f"{mins} хв")
        return " ".join(parts) if parts else "0 хв"

    @property
    def badge(self) -> str:
        if self.has_confirmed and self.has_possible:
            return "🔴/🟡 Відключення + заплановане"
        if self.state == SlotState.OFF:
            return "🔴 Відключення"
        if self.state == SlotState.POSSIBLE_OFF:
            return "🟡 Заплановане відключення"
        return "🟢 Світло є"


@dataclass
class DaySchedule:
    """Графік погодинних відключень для однієї черги на одну добу (48 півгодинних слотів)."""

    queue: str
    schedule_date: date
    # 48 слотів по 30 хвилин: індекс 0 = 00:00..00:30, індекс 47 = 23:30..24:00
    slots: list[SlotState] = field(default_factory=lambda: [SlotState.ON] * 48)
    source: str = "website"
    fetched_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if len(self.slots) == 24:
            # Якщо передано 24 годинні слоти, дублюємо кожен на 2 півгодинні
            expanded: list[SlotState] = []
            for s in self.slots:
                expanded.extend([s, s])
            self.slots = expanded
        elif len(self.slots) != 48:
            raise ValueError(f"DaySchedule expects 24 or 48 slots, got {len(self.slots)}")

    @classmethod
    def from_hourly(
        cls,
        queue: str,
        schedule_date: date,
        hourly_states: list[SlotState],
        source: str = "website",
    ) -> DaySchedule:
        if len(hourly_states) != 24:
            raise ValueError(f"Expected 24 hourly states, got {len(hourly_states)}")
        slots: list[SlotState] = []
        for state in hourly_states:
            slots.extend([state, state])
        return cls(queue=queue, schedule_date=schedule_date, slots=slots, source=source)

    @property
    def content_hash(self) -> str:
        """SHA-256 хеш станів слотів для миттєвого виявлення змін у графіку."""
        raw = ",".join(s.value for s in self.slots)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps([s.value for s in self.slots])

    @classmethod
    def from_json(
        cls,
        queue: str,
        schedule_date: date,
        slots_json: str,
        source: str = "db",
        fetched_at: Optional[datetime] = None,
    ) -> DaySchedule:
        raw_list = json.loads(slots_json)
        slots = [SlotState(val) for val in raw_list]
        return cls(
            queue=queue,
            schedule_date=schedule_date,
            slots=slots,
            source=source,
            fetched_at=fetched_at,
        )

    def hour_state(self, hour: int) -> tuple[SlotState, SlotState]:
        """Повертає стан першої (00..30) та другої (30..60) половини години (0..23)."""
        return self.slots[hour * 2], self.slots[hour * 2 + 1]

    def state_at_minute(self, minute_of_day: int) -> SlotState:
        """Стан у конкретну хвилину доби (0..1439)."""
        idx = max(0, min(47, minute_of_day // 30))
        return self.slots[idx]

    def get_outage_intervals(self, merge_confirmed_and_possible: bool = False) -> list[OutageInterval]:
        """
        Групує послідовні слоти відключень у зручні інтервали.
        Якщо merge_confirmed_and_possible=True, об'єднує суміжні 🔴 та 🟡 у єдиний безперервний блок
        відключення (корисно для розрахунку точного часу вимкнення та відновлення світла).
        """
        intervals: list[OutageInterval] = []
        current_start: Optional[int] = None
        current_state: Optional[SlotState] = None
        has_conf = False
        has_poss = False

        for idx, state in enumerate(self.slots):
            minute_start = idx * 30
            if not state.is_outage:
                if current_start is not None and current_state is not None:
                    intervals.append(
                        OutageInterval(
                            start_minute=current_start,
                            end_minute=minute_start,
                            state=current_state,
                            has_confirmed=has_conf,
                            has_possible=has_poss,
                        )
                    )
                    current_start = None
                    current_state = None
                    has_conf = False
                    has_poss = False
                continue

            if current_start is None:
                current_start = minute_start
                current_state = state
                has_conf = state == SlotState.OFF
                has_poss = state == SlotState.POSSIBLE_OFF
            else:
                if merge_confirmed_and_possible or state == current_state:
                    has_conf = has_conf or (state == SlotState.OFF)
                    has_poss = has_poss or (state == SlotState.POSSIBLE_OFF)
                    if has_conf:
                        current_state = SlotState.OFF
                else:
                    intervals.append(
                        OutageInterval(
                            start_minute=current_start,
                            end_minute=minute_start,
                            state=current_state,
                            has_confirmed=has_conf,
                            has_possible=has_poss,
                        )
                    )
                    current_start = minute_start
                    current_state = state
                    has_conf = state == SlotState.OFF
                    has_poss = state == SlotState.POSSIBLE_OFF

        if current_start is not None and current_state is not None:
            intervals.append(
                OutageInterval(
                    start_minute=current_start,
                    end_minute=1440,
                    state=current_state,
                    has_confirmed=has_conf,
                    has_possible=has_poss,
                )
            )
        return intervals

    @property
    def total_outage_minutes(self) -> int:
        return sum(30 for s in self.slots if s.is_outage)

    @property
    def confirmed_outage_minutes(self) -> int:
        return sum(30 for s in self.slots if s == SlotState.OFF)

    @property
    def possible_outage_minutes(self) -> int:
        return sum(30 for s in self.slots if s == SlotState.POSSIBLE_OFF)
