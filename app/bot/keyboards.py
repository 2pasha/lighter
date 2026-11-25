from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import ALL_QUEUES, REGION_IF, REGION_VOE, split_queue_key
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


def choose_region_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏔 Івано-Франківська область",
                    callback_data=f"addloc:reg:{REGION_IF}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌾 Вінницька область",
                    callback_data=f"addloc:reg:{REGION_VOE}",
                )
            ],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="addloc:cancel")],
        ]
    )


def add_location_method_kb(region: str = REGION_VOE) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
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
    ]
    if region == REGION_VOE:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📸 Розпізнати за картинкою графіка",
                    callback_data="addloc:method:image",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Змінити область",
                callback_data="addloc:back_reg",
            ),
            InlineKeyboardButton(text="❌ Скасувати", callback_data="addloc:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    active_count = sum(1 for l in locations if l.is_active)
    total_count = len(locations)

    for loc in locations:
        status_icon = "🟢" if loc.is_active else "⏸"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status_icon} {loc.label} ({loc.region_short_label} · Черга {loc.queue_code})",
                    callback_data=f"loc:view:{loc.id}",
                ),
            ]
        )

        status_row: list[InlineKeyboardButton] = [
            InlineKeyboardButton(
                text="🔕 На паузу" if loc.is_active else "🔔 Увімкнути",
                callback_data=f"loc:toggle:{loc.id}",
            )
        ]
        if total_count > 1 and not (loc.is_active and active_count == 1):
            status_row.append(
                InlineKeyboardButton(
                    text="🎯 Тільки ця",
                    callback_data=f"loc:only:{loc.id}",
                )
            )
        rows.append(status_row)

        rows.append(
            [
                InlineKeyboardButton(
                    text="🔢 Змінити чергу",
                    callback_data=f"loc:chq:{loc.id}",
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


def make_only_active_kb(loc_id: int, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🎯 Тільки «{label}» (призупинити інші)",
                    callback_data=f"loc:only_new:{loc_id}",
                )
            ]
        ]
    )


def resume_paused_locations_kb(
    locations: list[UserLocation], day_tag: str = "today"
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for loc in locations:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔔 Увімкнути «{loc.label}» ({loc.region_short_label} · {loc.queue_code})",
                    callback_data=f"loc:resume_show:{loc.id}:{day_tag}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="📍 Керувати локаціями",
                callback_data="loc:list",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def change_location_queue_kb(loc: UserLocation) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if loc.city_name and (loc.region != REGION_IF or (loc.street_name and loc.house_num)):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔍 Перевірити актуальну чергу за адресою",
                    callback_data=f"loc:verify:{loc.id}",
                )
            ]
        )
    for i in range(0, len(ALL_QUEUES), 4):
        chunk = ALL_QUEUES[i : i + 4]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ {q}" if q == loc.queue_code else f"⚡ {q}",
                    callback_data=f"loc:setq:{loc.id}:{q}",
                )
                for q in chunk
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад до локацій", callback_data="loc:list"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def switch_queue_suggestion_kb(loc: UserLocation, new_queue: str) -> InlineKeyboardMarkup:
    _, new_code = split_queue_key(new_queue)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🔄 Перемкнути «{loc.label}» з {loc.queue_code} на {new_code}",
                    callback_data=f"loc:setq:{loc.id}:{new_code}",
                )
            ]
        ]
    )


def schedule_view_actions_kb(
    loc_id: int, day_tag: str = "today", is_paused: bool = False
) -> InlineKeyboardMarkup:
    other_tag = "tomorrow" if day_tag == "today" else "today"
    other_label = "🌅 Переглянути на завтра" if day_tag == "today" else "📅 Переглянути на сьогодні"
    rows: list[list[InlineKeyboardButton]] = [
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
    if is_paused:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎯 Зробити єдиною активною",
                    callback_data=f"loc:only_new:{loc_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def broadcast_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="broadcast:cancel")]
        ]
    )


def broadcast_confirm_kb(user_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ Надіслати ({user_count} корист.)",
                    callback_data="broadcast:send",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Змінити повідомлення",
                    callback_data="broadcast:edit",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Скасувати",
                    callback_data="broadcast:cancel",
                )
            ],
        ]
    )
