from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.keyboards import (
    BTN_LOCATIONS,
    BTN_SETTINGS,
    BTN_TODAY,
    BTN_TOMORROW,
    add_location_method_kb,
    autocomplete_items_kb,
    label_presets_kb,
    locations_list_kb,
    main_reply_keyboard,
    queue_picker_kb,
    schedule_view_actions_kb,
    settings_kb,
)
from app.bot.states import AddLocationStates, QuietHoursStates
from app.config import ALL_QUEUES, settings
from app.database import Database, UserLocation
from app.models import DaySchedule
from app.services.formatter import format_schedule_message
from app.services.image_parser import parse_voe_schedule_image, render_schedule_image
from app.services.voe_client import AutocompleteItem, VOEDataClient

logger = logging.getLogger(__name__)


def setup_routers(db: Database, voe_client: VOEDataClient) -> Router:
    router = Router()

    # -------------------------------------------------------------------------
    # /start та /help
    # -------------------------------------------------------------------------
    @router.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not message.from_user:
            return
        user = await db.upsert_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        locations = await db.get_user_locations(user.telegram_id)

        welcome = (
            f"👋 Вітаю, <b>{message.from_user.first_name or 'друже'}</b>!\n\n"
            "Я — бот моніторингу графіків погодинних відключень світла "
            "<b>АТ «Вінницяобленерго»</b> ⚡\n\n"
            "🔔 <b>Що я вмію:</b>\n"
            "1️⃣ Сповіщати, щойно з'явиться графік на <b>завтра</b>\n"
            "2️⃣ Миттєво повідомляти, якщо графік <b>змінився</b> протягом дня\n"
            "3️⃣ Попереджати за <b>15 хвилин до вимкнення</b> світла (🔴 та 🟡)\n"
            "4️⃣ Повідомляти за <b>15 хвилин до увімкнення</b> світла 💡\n"
            "5️⃣ Розпізнавати картинки з <code>@pat_voe_bot</code> — просто перешліть або надішліть мені зображення графіка!"
        )
        await message.answer(welcome, parse_mode="HTML", reply_markup=main_reply_keyboard())

        if not locations:
            await message.answer(
                "📍 <b>У вас ще немає збережених локацій.</b>\n"
                "Оберіть зручний спосіб додавання вашої адреси або черги:",
                parse_mode="HTML",
                reply_markup=add_location_method_kb(),
            )
        else:
            await _send_schedules_for_day(message, user.telegram_id, "today")

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        text = (
            "ℹ️ <b>Довідка по боту Світло Вінниця:</b>\n\n"
            "• <b>📅 Графік на сьогодні</b> — актуальний розклад по всіх ваших локаціях\n"
            "• <b>🌅 Графік на завтра</b> — розклад на наступну добу\n"
            "• <b>📍 Мої локації</b> — керування вашими адресами та чергами (можна додати Дім, Роботу, Батьків)\n"
            "• <b>⚙️ Налаштування</b> — увімкнення/вимкнення типів сповіщень та тихих годин (вночі)\n\n"
            "💡 <i>Лайфхак:</i> Ви можете в будь-який момент надіслати або переслати сюди "
            "картинку графіка з офіційного бота <code>@pat_voe_bot</code> — я автоматично "
            "розпізнаю всі кольорові слоти та оновлю графік!"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=main_reply_keyboard())

    # -------------------------------------------------------------------------
    # Головне меню: Графік на сьогодні / на завтра
    # -------------------------------------------------------------------------
    @router.message(F.text == BTN_TODAY)
    @router.message(Command("today"))
    async def handle_today(message: Message) -> None:
        if not message.from_user:
            return
        await _send_schedules_for_day(message, message.from_user.id, "today")

    @router.message(F.text == BTN_TOMORROW)
    @router.message(Command("tomorrow"))
    async def handle_tomorrow(message: Message) -> None:
        if not message.from_user:
            return
        await _send_schedules_for_day(message, message.from_user.id, "tomorrow")

    async def _send_schedules_for_day(
        message: Message, telegram_id: int, day_tag: str
    ) -> None:
        locations = await db.get_user_locations(telegram_id)
        if not locations:
            await message.answer(
                "📍 Ви ще не додали жодної локації. Додайте чергу або адресу:",
                reply_markup=add_location_method_kb(),
            )
            return

        now = datetime.now(settings.tz)
        target_date = now.date() if day_tag == "today" else now.date() + timedelta(days=1)

        for loc in locations:
            sched = await _get_or_fetch_schedule(loc, target_date, force_refresh=False)
            if not sched:
                day_label = "сьогодні" if day_tag == "today" else "завтра"
                await message.answer(
                    f"📍 <b>{loc.label}</b> (Черга {loc.queue})\n"
                    f"⏳ Графік на <b>{day_label} ({target_date.strftime('%d.%m.%Y')})</b> "
                    f"ще не опубліковано обленерго.\n"
                    f"<i>Щойно він з'явиться, я одразу надішлю вам сповіщення!</i>",
                    parse_mode="HTML",
                    reply_markup=schedule_view_actions_kb(loc.id, day_tag),
                )
                continue

            await _reply_with_schedule_card(message, loc, sched, now, day_tag)

    async def _get_or_fetch_schedule(
        loc: UserLocation, target_date: date, force_refresh: bool = False
    ) -> Optional[DaySchedule]:
        if not force_refresh:
            cached = await db.get_schedule(loc.queue, target_date)
            if cached:
                return cached

        # 1. Спробуємо завантажити з GitHub JSON дзеркала
        all_github = await voe_client.fetch_all_queues_from_github()
        for (_, _), s in all_github.items():
            await db.save_schedule(s)

        if (loc.queue, target_date) in all_github:
            return all_github[(loc.queue, target_date)]

        # 2. Якщо локація має ID адреси — опитуємо сайт voe.com.ua/disconnection/detailed
        if loc.city_id and loc.street_id and loc.house_id:
            addr_res = await voe_client.fetch_by_address(
                city_id=loc.city_id,
                street_id=loc.street_id,
                house_id=loc.house_id,
                city_name=loc.city_name or "",
                street_name=loc.street_name or "",
                house_num=loc.house_num or "",
            )
            if addr_res and addr_res.schedules:
                for d, s in addr_res.schedules.items():
                    s.queue = loc.queue
                    await db.save_schedule(s)
                if target_date in addr_res.schedules:
                    return addr_res.schedules[target_date]

        return await db.get_schedule(loc.queue, target_date)

    async def _reply_with_schedule_card(
        message: Message,
        loc: UserLocation,
        sched: DaySchedule,
        now: datetime,
        day_tag: str,
    ) -> None:
        text = format_schedule_message(
            schedule=sched,
            location_label=loc.label,
            address_str=loc.display_address,
            now=now,
        )
        img_bytes = render_schedule_image(sched, now=now)
        kb = schedule_view_actions_kb(loc.id, day_tag)

        if img_bytes and len(text) <= 1000:
            photo = BufferedInputFile(
                img_bytes, filename=f"voe_{sched.queue}_{sched.schedule_date}.png"
            )
            await message.answer_photo(
                photo=photo, caption=text, parse_mode="HTML", reply_markup=kb
            )
        else:
            if img_bytes:
                photo = BufferedInputFile(
                    img_bytes, filename=f"voe_{sched.queue}_{sched.schedule_date}.png"
                )
                await message.answer_photo(photo=photo)
            await message.answer(text, parse_mode="HTML", reply_markup=kb)

    # -------------------------------------------------------------------------
    # Callback-кнопки оновлення та перемикання сьогодні/завтра
    # -------------------------------------------------------------------------
    @router.callback_query(F.data.startswith("sched:"))
    async def cb_schedule_actions(call: CallbackQuery) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        parts = call.data.split(":")
        if len(parts) < 4:
            return
        action, loc_id_str, day_tag = parts[1], parts[2], parts[3]
        loc = await db.get_location_by_id(int(loc_id_str), call.from_user.id)
        if not loc:
            await call.answer("Локацію не знайдено", show_alert=True)
            return

        now = datetime.now(settings.tz)
        target_date = now.date() if day_tag == "today" else now.date() + timedelta(days=1)
        force = action == "refresh"
        if force:
            await call.answer("🔄 Оновлюю дані з джерел Вінницяобленерго...")
        else:
            await call.answer()

        sched = await _get_or_fetch_schedule(loc, target_date, force_refresh=force)
        if not sched:
            day_label = "сьогодні" if day_tag == "today" else "завтра"
            await call.message.answer(
                f"📍 <b>{loc.label}</b> (Черга {loc.queue})\n"
                f"⏳ Графік на <b>{day_label} ({target_date.strftime('%d.%m.%Y')})</b> ще не з'явився.",
                parse_mode="HTML",
                reply_markup=schedule_view_actions_kb(loc.id, day_tag),
            )
            return

        await _reply_with_schedule_card(call.message, loc, sched, now, day_tag)  # type: ignore

    # -------------------------------------------------------------------------
    # Мої локації: перегляд, додавання, видалення
    # -------------------------------------------------------------------------
    @router.message(F.text == BTN_LOCATIONS)
    @router.message(Command("locations"))
    async def handle_locations(message: Message) -> None:
        if not message.from_user:
            return
        locations = await db.get_user_locations(message.from_user.id)
        if not locations:
            await message.answer(
                "📍 У вас ще немає збережених локацій.\nОберіть спосіб додавання:",
                reply_markup=add_location_method_kb(),
            )
            return

        lines = ["📍 <b>Ваші підключені локації:</b>\n"]
        for loc in locations:
            lines.append(
                f"• <b>{loc.label}</b> — Черга <b>{loc.queue}</b> ({loc.display_address})"
            )
        lines.append("\nНатисніть на локацію для перегляду графіка або додайте нову:")
        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=locations_list_kb(locations),
        )

    @router.callback_query(F.data == "loc:add")
    async def cb_loc_add(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.answer()
        if call.message:
            await call.message.answer(
                "➕ <b>Додавання нової локації</b>\nЯк ви хочете вказати графік?",
                parse_mode="HTML",
                reply_markup=add_location_method_kb(),
            )

    @router.callback_query(F.data.startswith("loc:view:"))
    async def cb_loc_view(call: CallbackQuery) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        loc_id = int(call.data.split(":")[2])
        loc = await db.get_location_by_id(loc_id, call.from_user.id)
        if not loc:
            await call.answer("Локацію не знайдено", show_alert=True)
            return
        await call.answer()
        now = datetime.now(settings.tz)
        sched = await _get_or_fetch_schedule(loc, now.date(), force_refresh=False)
        if sched:
            await _reply_with_schedule_card(call.message, loc, sched, now, "today")  # type: ignore
        else:
            await call.message.answer(
                f"📍 <b>{loc.label}</b> (Черга {loc.queue})\n"
                f"Графік на сьогодні ще не завантажено. Натисніть «Оновити зараз».",
                parse_mode="HTML",
                reply_markup=schedule_view_actions_kb(loc.id, "today"),
            )

    @router.callback_query(F.data.startswith("loc:del:"))
    async def cb_loc_del(call: CallbackQuery) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        loc_id = int(call.data.split(":")[2])
        deleted = await db.delete_location(loc_id, call.from_user.id)
        if deleted:
            await call.answer("🗑 Локацію видалено")
        else:
            await call.answer("Не вдалося видалити локацію", show_alert=True)
            return

        locations = await db.get_user_locations(call.from_user.id)
        if not locations:
            await call.message.edit_text(
                "📍 Усі локації видалено. Додайте нову локацію:",
                reply_markup=add_location_method_kb(),
            )
        else:
            await call.message.edit_reply_markup(reply_markup=locations_list_kb(locations))

    # -------------------------------------------------------------------------
    # FSM: Додавання локації (за чергою або за адресою)
    # -------------------------------------------------------------------------
    @router.callback_query(F.data == "addloc:cancel")
    async def cb_addloc_cancel(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.answer("Скасовано")
        if call.message:
            await call.message.edit_text("❌ Додавання локації скасовано.")

    @router.callback_query(F.data == "addloc:method:queue")
    async def cb_addloc_method_queue(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddLocationStates.choosing_queue)
        await call.answer()
        if call.message:
            await call.message.edit_text(
                "🔢 <b>Оберіть вашу чергу (підчергу) ГПВ:</b>",
                parse_mode="HTML",
                reply_markup=queue_picker_kb(),
            )

    @router.callback_query(
        AddLocationStates.choosing_queue, F.data.startswith("addloc:queue:")
    )
    async def cb_addloc_pick_queue(call: CallbackQuery, state: FSMContext) -> None:
        if not call.data or not call.message:
            return
        queue = call.data.split(":")[2]
        await state.update_data(queue=queue)
        await state.set_state(AddLocationStates.entering_label)
        await call.answer()
        await call.message.edit_text(
            f"✅ Обрано чергу <b>{queue}</b>.\n\n"
            "🏷 Тепер оберіть назву для цієї локації або просто напишіть свою (наприклад: <i>Квартира</i>, <i>Кав'ярня</i>):",
            parse_mode="HTML",
            reply_markup=label_presets_kb(),
        )

    @router.callback_query(F.data == "addloc:method:image")
    async def cb_addloc_method_image(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.answer()
        if call.message:
            await call.message.edit_text(
                "📸 <b>Розпізнавання графіка за зображенням</b>\n\n"
                "Просто надішліть або перешліть мені картинку розкладу з офіційного бота "
                "<code>@pat_voe_bot</code> у цей чат.\n"
                "У підписі до фото можете вказати вашу чергу (наприклад <code>3.2</code>)!",
                parse_mode="HTML",
            )

    # Пошук за адресою: Крок 1 — Місто
    @router.callback_query(F.data == "addloc:method:address")
    async def cb_addloc_method_address(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddLocationStates.entering_city)
        await call.answer()
        if call.message:
            await call.message.edit_text(
                "🏙 <b>Крок 1 із 3: Населений пункт</b>\n"
                "Введіть назву міста або села українською мовою (наприклад: <code>Вінниця</code>, <code>Жмеринка</code>, <code>Агрономічне</code>):",
                parse_mode="HTML",
            )

    @router.message(AddLocationStates.entering_city, F.text)
    async def msg_entering_city(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if len(query) < 2:
            await message.answer("Введіть щонайменше 2 літери назви населеного пункту:")
            return

        status_msg = await message.answer("🔍 Шукаю населений пункт у базі Вінницяобленерго...")
        cities = await voe_client.search_cities(query)
        if not cities:
            await status_msg.edit_text(
                "⚠️ На жаль, не вдалося знайти населений пункт або сайт обленерго тимчасово не відповідає.\n"
                "Спробуйте ввести іншу назву або оберіть чергу вручну:",
                reply_markup=queue_picker_kb(),
            )
            await state.set_state(AddLocationStates.choosing_queue)
            return

        await state.update_data(
            city_candidates=[{"id": c.id, "label": c.label} for c in cities[:10]]
        )
        await state.set_state(AddLocationStates.selecting_city)
        await status_msg.edit_text(
            "🏙 Оберіть ваш населений пункт зі списку:",
            reply_markup=autocomplete_items_kb(cities, "addloc:city"),
        )

    @router.callback_query(
        AddLocationStates.selecting_city, F.data.startswith("addloc:city:")
    )
    async def cb_select_city(call: CallbackQuery, state: FSMContext) -> None:
        if not call.data or not call.message:
            return
        idx = int(call.data.split(":")[2])
        data = await state.get_data()
        candidates = data.get("city_candidates", [])
        if idx >= len(candidates):
            await call.answer("Помилка вибору", show_alert=True)
            return

        chosen = candidates[idx]
        await state.update_data(city_id=chosen["id"], city_name=chosen["label"])
        await state.set_state(AddLocationStates.entering_street)
        await call.answer()
        await call.message.edit_text(
            f"🏙 Населений пункт: <b>{chosen['label']}</b>\n\n"
            "🛣 <b>Крок 2 із 3: Вулиця</b>\n"
            "Введіть назву вулиці (наприклад: <code>Соборна</code>, <code>Келецька</code>):",
            parse_mode="HTML",
        )

    @router.message(AddLocationStates.entering_street, F.text)
    async def msg_entering_street(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if len(query) < 2:
            await message.answer("Введіть щонайменше 2 літери назви вулиці:")
            return

        data = await state.get_data()
        city_id = data.get("city_id", "")
        status_msg = await message.answer("🔍 Шукаю вулицю...")
        streets = await voe_client.search_streets(city_id, query)
        if not streets:
            await status_msg.edit_text(
                "⚠️ Вулицю не знайдено. Спробуйте ввести частину назви вулиці ще раз або оберіть чергу вручну:",
                reply_markup=queue_picker_kb(),
            )
            return

        await state.update_data(
            street_candidates=[{"id": s.id, "label": s.label} for s in streets[:10]]
        )
        await state.set_state(AddLocationStates.selecting_street)
        await status_msg.edit_text(
            "🛣 Оберіть вулицю зі списку:",
            reply_markup=autocomplete_items_kb(streets, "addloc:street"),
        )

    @router.callback_query(
        AddLocationStates.selecting_street, F.data.startswith("addloc:street:")
    )
    async def cb_select_street(call: CallbackQuery, state: FSMContext) -> None:
        if not call.data or not call.message:
            return
        idx = int(call.data.split(":")[2])
        data = await state.get_data()
        candidates = data.get("street_candidates", [])
        if idx >= len(candidates):
            await call.answer("Помилка вибору", show_alert=True)
            return

        chosen = candidates[idx]
        await state.update_data(street_id=chosen["id"], street_name=chosen["label"])
        await state.set_state(AddLocationStates.entering_house)
        await call.answer()
        await call.message.edit_text(
            f"🏙 {data.get('city_name')}, <b>{chosen['label']}</b>\n\n"
            "🏠 <b>Крок 3 із 3: Номер будинку</b>\n"
            "Введіть номер будинку (наприклад: <code>10</code>, <code>25А</code>):",
            parse_mode="HTML",
        )

    @router.message(AddLocationStates.entering_house, F.text)
    async def msg_entering_house(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        data = await state.get_data()
        street_id = data.get("street_id", "")
        status_msg = await message.answer("🔍 Перевіряю будинок...")
        houses = await voe_client.search_houses(street_id, query)
        if not houses:
            await status_msg.edit_text(
                "⚠️ Будинок не знайдено в базі. Оберіть вашу чергу вручну, щоб завершити прив'язку адреси:",
                reply_markup=queue_picker_kb(),
            )
            await state.update_data(house_num=query)
            await state.set_state(AddLocationStates.choosing_queue)
            return

        await state.update_data(
            house_candidates=[{"id": h.id, "label": h.label} for h in houses[:10]]
        )
        await state.set_state(AddLocationStates.selecting_house)
        await status_msg.edit_text(
            "🏠 Оберіть ваш будинок зі списку:",
            reply_markup=autocomplete_items_kb(houses, "addloc:house"),
        )

    @router.callback_query(
        AddLocationStates.selecting_house, F.data.startswith("addloc:house:")
    )
    async def cb_select_house(call: CallbackQuery, state: FSMContext) -> None:
        if not call.data or not call.message:
            return
        idx = int(call.data.split(":")[2])
        data = await state.get_data()
        candidates = data.get("house_candidates", [])
        if idx >= len(candidates):
            await call.answer("Помилка вибору", show_alert=True)
            return

        chosen = candidates[idx]
        city_id = data.get("city_id", "")
        city_name = data.get("city_name", "")
        street_id = data.get("street_id", "")
        street_name = data.get("street_name", "")
        house_id = chosen["id"]
        house_num = chosen["label"]

        await call.answer("Визначаю чергу ГПВ...")
        await call.message.edit_text("⏳ Запитую чергу та графік для вашої адреси на сайті Вінницяобленерго...")

        addr_res = await voe_client.fetch_by_address(
            city_id=city_id,
            street_id=street_id,
            house_id=house_id,
            city_name=city_name,
            street_name=street_name,
            house_num=house_num,
        )

        if addr_res:
            queue = addr_res.queue
            for _, s in addr_res.schedules.items():
                await db.save_schedule(s)
            await state.update_data(
                house_id=house_id,
                house_num=house_num,
                queue=queue,
            )
            await state.set_state(AddLocationStates.entering_label)
            await call.message.edit_text(
                f"✅ Адресу знайдено: <b>{city_name}, {street_name}, {house_num}</b>\n"
                f"🔢 Ваша черга ГПВ: <b>{queue}</b>\n\n"
                "🏷 Оберіть назву для цієї локації або напишіть свою:",
                parse_mode="HTML",
                reply_markup=label_presets_kb(),
            )
        else:
            await state.update_data(house_id=house_id, house_num=house_num)
            await state.set_state(AddLocationStates.choosing_queue)
            await call.message.edit_text(
                f"📍 Адреса: <b>{city_name}, {street_name}, {house_num}</b>\n"
                "Вкажіть вашу чергу ГПВ (якщо ви її знаєте з @pat_voe_bot):",
                parse_mode="HTML",
                reply_markup=queue_picker_kb(),
            )

    # Фінальний крок додавання локації: Назва (Label)
    @router.callback_query(
        AddLocationStates.entering_label, F.data.startswith("addloc:label:")
    )
    async def cb_addloc_label(call: CallbackQuery, state: FSMContext) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        label = call.data.split(":", 2)[2]
        await _finalize_location_creation(
            call.message, call.from_user.id, label, state  # type: ignore
        )
        await call.answer("✅ Локацію додано!")

    @router.message(AddLocationStates.entering_label, F.text)
    async def msg_addloc_label(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return
        label = (message.text or "🏠 Дім").strip()[:40]
        await _finalize_location_creation(message, message.from_user.id, label, state)

    async def _finalize_location_creation(
        message: Message, telegram_id: int, label: str, state: FSMContext
    ) -> None:
        data = await state.get_data()
        queue = data.get("queue", "1.1")
        await db.upsert_user(telegram_id=telegram_id)
        loc = await db.add_location(
            telegram_id=telegram_id,
            label=label,
            queue=queue,
            city_name=data.get("city_name"),
            city_id=data.get("city_id"),
            street_name=data.get("street_name"),
            street_id=data.get("street_id"),
            house_num=data.get("house_num"),
            house_id=data.get("house_id"),
        )
        await state.clear()
        await message.answer(
            f"🎉 Локацію <b>{loc.label}</b> (Черга <b>{loc.queue}</b>) успішно збережено!\n"
            "Тепер ви отримуватимете всі сповіщення про графіки та відключення за 15 хвилин.",
            parse_mode="HTML",
            reply_markup=main_reply_keyboard(),
        )
        now = datetime.now(settings.tz)
        sched = await _get_or_fetch_schedule(loc, now.date(), force_refresh=False)
        if sched:
            await _reply_with_schedule_card(message, loc, sched, now, "today")

    # -------------------------------------------------------------------------
    # Налаштування сповіщень та тихих годин
    # -------------------------------------------------------------------------
    @router.message(F.text == BTN_SETTINGS)
    @router.message(Command("settings"))
    async def handle_settings(message: Message) -> None:
        if not message.from_user:
            return
        user = await db.upsert_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        await message.answer(
            _format_settings_text(user),
            parse_mode="HTML",
            reply_markup=settings_kb(user),
        )

    @router.callback_query(F.data.startswith("set:toggle:"))
    async def cb_toggle_setting(call: CallbackQuery) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        field = call.data.split(":")[2]
        user = await db.toggle_user_setting(call.from_user.id, field)
        if not user:
            await call.answer("Помилка оновлення", show_alert=True)
            return
        await call.answer("Налаштування оновлено")
        await call.message.edit_text(
            _format_settings_text(user),
            parse_mode="HTML",
            reply_markup=settings_kb(user),
        )

    @router.callback_query(F.data == "set:quiet_range")
    async def cb_quiet_range(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(QuietHoursStates.entering_range)
        await call.answer()
        if call.message:
            await call.message.answer(
                "🌙 <b>Налаштування тихих годин</b>\n"
                "Введіть інтервал часу у форматі <code>ГГ:ХХ-ГГ:ХХ</code>, коли бот не повинен турбувати вас 15-хвилинними попередженнями.\n"
                "Наприклад: <code>00:00-07:00</code> або <code>23:00-07:30</code>:",
                parse_mode="HTML",
            )

    @router.message(QuietHoursStates.entering_range, F.text)
    async def msg_quiet_range(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return
        raw = (message.text or "").strip()
        match = re.match(r"^([0-2]\d:[0-5]\d)\s*[-–—]\s*([0-2]\d:[0-5]\d)$", raw)
        if not match:
            await message.answer(
                "⚠️ Невірний формат. Будь ласка, введіть час у форматі <code>00:00-07:00</code>:",
                parse_mode="HTML",
            )
            return

        start_str, end_str = match.group(1), match.group(2)
        user = await db.update_quiet_hours(message.from_user.id, start_str, end_str)
        await state.clear()
        if user:
            await message.answer(
                f"✅ Тихі години встановлено: <b>{start_str} – {end_str}</b>",
                parse_mode="HTML",
                reply_markup=settings_kb(user),
            )

    # -------------------------------------------------------------------------
    # Розпізнавання надісланого користувачем зображення з @pat_voe_bot
    # -------------------------------------------------------------------------
    @router.message(F.photo)
    async def handle_uploaded_schedule_photo(message: Message, bot: Bot) -> None:
        if not message.from_user or not message.photo:
            return

        # Визначаємо чергу та дату з підпису або з першої локації користувача
        caption = message.caption or ""
        locations = await db.get_user_locations(message.from_user.id)
        default_queue = locations[0].queue if locations else "3.2"

        q_match = re.search(r"([1-6][.,][1-2])", caption)
        queue = q_match.group(1).replace(",", ".") if q_match else default_queue

        now = datetime.now(settings.tz)
        target_date = now.date()
        d_match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", caption)
        if d_match:
            try:
                target_date = date(
                    int(d_match.group(3)), int(d_match.group(2)), int(d_match.group(1))
                )
            except ValueError:
                pass
        elif "завтра" in caption.lower():
            target_date = now.date() + timedelta(days=1)

        try:
            largest_photo = message.photo[-1]
            buf = io.BytesIO()
            await bot.download(largest_photo, destination=buf)
            img_bytes = buf.getvalue()

            parsed_sched = parse_voe_schedule_image(
                img_bytes, queue=queue, schedule_date=target_date
            )
            await db.save_schedule(parsed_sched)

            # Якщо у користувача ще немає локації з цією чергою, пропонуємо або створюємо
            if not locations:
                await db.upsert_user(
                    telegram_id=message.from_user.id,
                    username=message.from_user.username,
                    first_name=message.from_user.first_name,
                )
                loc = await db.add_location(
                    telegram_id=message.from_user.id,
                    label="🏠 Дім",
                    queue=queue,
                )
            else:
                loc = next((l for l in locations if l.queue == queue), locations[0])

            text = format_schedule_message(
                schedule=parsed_sched,
                location_label=loc.label,
                address_str=loc.display_address,
                now=now,
                header_prefix="📸✅ <b>Зображення графіка з @pat_voe_bot успішно розпізнано та збережено!</b>",
            )
            await message.answer(text, parse_mode="HTML", reply_markup=main_reply_keyboard())
        except Exception as exc:
            logger.exception("Failed to parse user photo: %s", exc)
            await message.answer(
                "⚠️ Не вдалося розпізнати таблицю на зображенні. Переконайтеся, що це оригінальна картинка графіка з @pat_voe_bot."
            )

    return router


def _format_settings_text(user) -> str:
    return (
        "⚙️ <b>Налаштування сповіщень</b>\n\n"
        "Тут ви можете гнучко налаштувати, які саме сповіщення отримувати:\n"
        "• <b>Графік на завтра</b> — сповіщення, коли ввечері публікується розклад на наступний день.\n"
        "• <b>Зміни в графіку</b> — сповіщення, якщо обленерго оперативно змінило години відключень.\n"
        "• <b>За 15 хв до вимкнення / увімкнення</b> — завчасне попередження перед кожним слотом.\n"
        "• <b>Можливі (жовті 🟡) слоти</b> — враховувати заплановані (жовті) слоти нарівні з підтвердженими (синіми/червоними 🔴).\n"
        "• <b>Тихі години</b> — не турбувати 15-хвилинними сповіщеннями вночі."
    )
