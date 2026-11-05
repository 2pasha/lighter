from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import ALL_QUEUES
from app.database import UserLocation, UserProfile
from app.services.voe_client import AutocompleteItem

BTN_TODAY = "📅 Графік на сьогодні"
BTN_TOMORROW = "🌅 Графік на завтра"
BTN_LOCATIONS = "📍 Мої локації"
BTN_SETTINGS = "⚙️ Налаштування"


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
            [KeyboardButton(text=BTN_LOCATIONS), KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def add_location_method_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔢 Я знаю свою чергу (1.1 – 6.2)",
                    callback_data="addloc:method:queue",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Знайти за адресою (Місто, Вулиця, Будинок)",
                    callback_data="addloc:method:address",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📸 Надіслати картинку з @pat_voe_bot",
                    callback_data="addloc:method:image",
                )
            ],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="addloc:cancel")],
        ]
    )


def queue_picker_kb(
    prefix: str = "addloc:queue",
    allowed_queues: list[str] | None = None,
) -> InlineKeyboardMarkup:
    queues = [q for q in ALL_QUEUES if not allowed_queues or q in allowed_queues]
    if not queues:
        queues = list(ALL_QUEUES)
    rows: list[list[InlineKeyboardButton]] = []
    step = 4 if len(queues) > 4 else 2
    for i in range(0, len(queues), step):
        chunk = queues[i : i + step]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"⚡ Черга {q}" if len(queues) <= 4 else f"⚡ {q}",
                    callback_data=f"{prefix}:{q}",
                )
                for q in chunk
            ]
        )
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="addloc:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def popular_cities_kb(
    items: list[AutocompleteItem], step_prefix: str = "addloc:city"
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(items[:10]), 2):
        chunk = items[i : i + 2]
        row = []
        for offset, item in enumerate(chunk):
            idx = i + offset
            icon = "🏙" if item.label.startswith("м.") else "🏘"
            row.append(
                InlineKeyboardButton(
                    text=f"{icon} {item.label}"[:40],
                    callback_data=f"{step_prefix}:{idx}",
                )
            )
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="addloc:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def label_presets_kb() -> InlineKeyboardMarkup:
    presets = ["🏠 Дім", "🏢 Робота", "👨‍👩‍👧 Батьки", "🏡 Дача"]
    rows = [
        [
            InlineKeyboardButton(text=p, callback_data=f"addloc:label:{p}")
            for p in presets[:2]
        ],
        [
            InlineKeyboardButton(text=p, callback_data=f"addloc:label:{p}")
            for p in presets[2:]
        ],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="addloc:cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def autocomplete_items_kb(
    items: list[AutocompleteItem], step_prefix: str
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(items[:10]):
        rows.append(
            [
                InlineKeyboardButton(
                    text=item.label[:60],
                    callback_data=f"{step_prefix}:{idx}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="addloc:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def locations_list_kb(locations: list[UserLocation]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for loc in locations:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📍 {loc.label} (Черга {loc.queue})",
                    callback_data=f"loc:view:{loc.id}",
                ),
                InlineKeyboardButton(
                    text="🗑 Видалити",
                    callback_data=f"loc:del:{loc.id}",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Додати нову локацію", callback_data="loc:add"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_view_actions_kb(loc_id: int, day_tag: str = "today") -> InlineKeyboardMarkup:
    other_tag = "tomorrow" if day_tag == "today" else "today"
    other_label = "🌅 Переглянути на завтра" if day_tag == "today" else "📅 Переглянути на сьогодні"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Оновити зараз",
                    callback_data=f"sched:refresh:{loc_id}:{day_tag}",
                ),
                InlineKeyboardButton(
                    text=other_label,
                    callback_data=f"sched:show:{loc_id}:{other_tag}",
                ),
            ]
        ]
    )


def settings_kb(user: UserProfile) -> InlineKeyboardMarkup:
    def flag(val: bool) -> str:
        return "✅ Увімк." if val else "❌ Вимк."

    qh_status = (
        f"✅ {user.quiet_start}–{user.quiet_end}"
        if user.quiet_hours_enabled
        else "❌ Вимк."
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🌅 Графік на завтра: {flag(user.notify_tomorrow)}",
                    callback_data="set:toggle:notify_tomorrow",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🔄 Зміни в графіку: {flag(user.notify_changes)}",
                    callback_data="set:toggle:notify_changes",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🔌 За 15 хв до вимкнення: {flag(user.notify_15m_off)}",
                    callback_data="set:toggle:notify_15m_off",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"💡 За 15 хв до увімкнення: {flag(user.notify_15m_on)}",
                    callback_data="set:toggle:notify_15m_on",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🟡 Попереджати про можливі (жовті) слоти: {flag(user.notify_possible)}",
                    callback_data="set:toggle:notify_possible",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🌙 Тихі години (не турбувати): {qh_status}",
                    callback_data="set:toggle:quiet_hours_enabled",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🕒 Змінити час тихих годин (напр. 00:00-07:00)",
                    callback_data="set:quiet_range",
                )
            ],
        ]
    )
