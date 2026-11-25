from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
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
    broadcast_cancel_kb,
    broadcast_confirm_kb,
    change_location_queue_kb,
    choose_region_kb,
    label_presets_kb,
    locations_list_kb,
    main_reply_keyboard,
    make_only_active_kb,
    popular_cities_kb,
    queue_picker_kb,
    resume_paused_locations_kb,
    schedule_view_actions_kb,
    settings_kb,
    switch_queue_suggestion_kb,
)
from app.bot.middlewares import AntiSpamMiddleware
from app.bot.states import AddLocationStates, BroadcastStates, QuietHoursStates
from app.config import (
    ALL_QUEUES,
    REGION_IF,
    REGION_VOE,
    get_region_label,
    is_valid_queue_key,
    make_queue_key,
    settings,
    split_queue_key,
)
from app.database import Database, UserLocation
from app.models import DaySchedule
from app.services.formatter import format_schedule_message
from app.services.if_client import (
    format_if_street_display,
    parse_freeform_if_address,
)
from app.services.image_parser import (
    extract_header_queue_and_date,
    parse_voe_schedule_image,
    render_schedule_image,
)
from app.services.notifier import NotificationEngine, send_photo_cached
from app.services.voe_client import AutocompleteItem, VOEDataClient
from app.services.widget_service import generate_user_loader_js

logger = logging.getLogger(__name__)


def setup_routers(
    db: Database,
    voe_client: VOEDataClient,
    notifier: Optional[NotificationEngine] = None,
) -> Router:
    router = Router()
    anti_spam = AntiSpamMiddleware()
    router.message.middleware(anti_spam)
    router.callback_query.middleware(anti_spam)

    async def _save_schedule(s: DaySchedule) -> None:
        if notifier is not None:
            await notifier.save_and_notify_if_changed(s)
        else:
            await db.save_schedule(s)

    async def _save_other_schedules_bg(
        schedules_map: dict[tuple[str, date], DaySchedule],
        skip_key: tuple[str, date],
        today: date,
    ) -> None:
        try:
            for (q, d), s in schedules_map.items():
                if (q, d) == skip_key:
                    continue
                if d > today and s.total_outage_minutes == 0:
                    continue
                await _save_schedule(s)
        except Exception as exc:
            logger.debug("Background schedule save error: %s", exc)

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
            "Я — <b>Світло Бот</b> ⚡ Моніторю графіки погодинних відключень світла "
            "та завчасно попереджаю про зміни й вимкнення.\n\n"
            "🔔 <b>Що я вмію:</b>\n"
            "1️⃣ Сповіщати, щойно з'явиться графік на <b>завтра</b>\n"
            "2️⃣ Миттєво повідомляти, якщо графік <b>змінився</b> протягом дня\n"
            "3️⃣ Попереджати за <b>15 хвилин до вимкнення</b> світла (🔴 та 🟡)\n"
            "4️⃣ Повідомляти за <b>15 хвилин до увімкнення</b> світла 💡\n"
            "5️⃣ Відстежувати одразу <b>кілька локацій</b> у різних містах та областях"
        )
        await message.answer(welcome, parse_mode="HTML", reply_markup=main_reply_keyboard())

        if not locations:
            await state.set_state(AddLocationStates.choosing_region)
            await message.answer(
                "📍 <b>У вас ще немає збережених локацій.</b>\n"
                "Оберіть область, для якої хочете додати адресу або чергу:",
                parse_mode="HTML",
                reply_markup=choose_region_kb(),
            )
        else:
            await _send_schedules_for_day(message, user.telegram_id, "today")

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        text = (
            "ℹ️ <b>Довідка по Світло Боту:</b>\n\n"
            "• <b>📅 Графік на сьогодні</b> — актуальний розклад по всіх ваших локаціях\n"
            "• <b>🌅 Графік на завтра</b> — розклад на наступну добу\n"
            "• <b>📍 Мої локації</b> — керування вашими адресами та чергами (Івано-Франківська та Вінницька області)\n"
            "• <b>⚙️ Налаштування</b> — увімкнення/вимкнення типів сповіщень та тихих годин (вночі)\n"
            "• <b>/widget</b> — персональний віджет для iPhone (Lock Screen та Home Screen)"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=main_reply_keyboard())

    # -------------------------------------------------------------------------
    # Прихована команда /widget (iOS Scriptable Lock Screen & Home Screen Widget)
    # -------------------------------------------------------------------------
    @router.message(Command("widget"))
    async def cmd_widget(message: Message) -> None:
        if not message.from_user:
            return
        user = await db.upsert_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        locations = await db.get_user_locations(user.telegram_id)
        default_loc = (
            next((l for l in locations if l.is_active), locations[0])
            if locations
            else None
        )
        default_loc_id = default_loc.id if default_loc else None

        # Дозволяємо передати кастомний URL сервера аргументом для тестування: /widget https://...
        raw_text = (message.text or "").strip()
        parts = raw_text.split(maxsplit=1)
        custom_base_url: Optional[str] = None
        if len(parts) > 1 and parts[1].startswith(("http://", "https://")):
            custom_base_url = parts[1].strip()

        js_bytes = generate_user_loader_js(
            user_id=user.telegram_id,
            base_url=custom_base_url,
            default_location_id=default_loc_id,
        )
        doc = BufferedInputFile(js_bytes, filename="Svitlo.js")

        loc_note = (
            f"📍 Активна локація: <b>{default_loc.label} ({default_loc.region_short_label} · Черга {default_loc.queue_code})</b>\n"
            "<i>Віджет автоматично перемикатиметься слідом за вашою активною локацією в боті!</i>\n"
            if default_loc
            else "📍 За замовчуванням: <b>Черга 3.2</b> (додайте власну локацію в меню бота)\n"
        )

        caption = (
            "📱 <b>Ваш персональний iOS-віджет (Scriptable) готовий!</b>\n\n"
            f"{loc_note}\n"
            "<b>Інструкція зі встановлення:</b>\n"
            "1️⃣ Завантажте безкоштовний додаток <a href=\"https://apps.apple.com/app/scriptable/id1405459188\">Scriptable</a> з App Store.\n"
            "2️⃣ Натисніть на файл <code>Svitlo.js</code> вище ➡️ <b>Поділитися (Share)</b> ➡️ оберіть <b>Scriptable</b> (<i>Add to Scriptable</i>).\n"
            "3️⃣ Додайте віджет <b>Scriptable</b> на Екран блокування (Lock Screen) або Домашній екран та оберіть у його налаштуваннях скрипт <b>Svitlo</b>.\n\n"
            "💡 <i>Підказка:</i> Якщо у вас декілька локацій, ви можете вказати назву локації чи міста (напр. <code>Коломия</code>, <code>Робота</code>) або чергу (напр. <code>4.1</code>, <code>if:4.1</code>) у полі <b>Parameter</b> налаштувань віджета."
        )
        await message.answer_document(
            document=doc,
            caption=caption,
            parse_mode="HTML",
        )

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
        t0 = time.perf_counter()
        locations = await db.get_user_locations(telegram_id)
        t_locs = time.perf_counter()
        if not locations:
            await message.answer(
                "📍 Ви ще не додали жодної локації. Оберіть вашу область:",
                reply_markup=choose_region_kb(),
            )
            return

        active_locations = [l for l in locations if l.is_active]
        if not active_locations:
            await message.answer(
                "⏸ <b>Усі ваші локації зараз на паузі</b> (сповіщення тимчасово вимкнено).\n"
                "Оберіть локацію нижче, щоб увімкнути її та переглянути актуальний графік:",
                parse_mode="HTML",
                reply_markup=resume_paused_locations_kb(locations, day_tag),
            )
            return

        now = datetime.now(settings.tz)
        target_date = now.date() if day_tag == "today" else now.date() + timedelta(days=1)

        for loc in active_locations:
            t_sched0 = time.perf_counter()
            sched = await _get_or_fetch_schedule(loc, target_date, force_refresh=False)
            t_sched1 = time.perf_counter()
            if not sched:
                day_label = "сьогодні" if day_tag == "today" else "завтра"
                await message.answer(
                    f"📍 <b>{loc.label}</b> ({loc.region_short_label} · Черга {loc.queue_code})\n"
                    f"⏳ Графік на <b>{day_label} ({target_date.strftime('%d.%m.%Y')})</b> "
                    f"ще не опубліковано.\n"
                    f"<i>Щойно він з'явиться, я одразу надішлю вам сповіщення!</i>",
                    parse_mode="HTML",
                    reply_markup=schedule_view_actions_kb(loc.id, day_tag),
                )
                continue

            render_ms, send_ms = await _reply_with_schedule_card(
                message, loc, sched, now, day_tag
            )
            total_ms = (time.perf_counter() - t0) * 1000.0
            locs_ms = (t_locs - t0) * 1000.0
            sched_ms = (t_sched1 - t_sched0) * 1000.0
            logger.info(
                "PERF | cmd=%s | user=%s | queue=%s | total=%.0fms "
                "(db_locs=%.0fms, sched=%.0fms, render=%.0fms, tg_send=%.0fms)",
                day_tag,
                telegram_id,
                loc.queue,
                total_ms,
                locs_ms,
                sched_ms,
                render_ms,
                send_ms,
            )

    async def _get_or_fetch_schedule(
        loc: UserLocation, target_date: date, force_refresh: bool = False
    ) -> Optional[DaySchedule]:
        today = datetime.now(settings.tz).date()

        # 1. Швидкий шлях (Cache-First): при звичайному натисканні кнопки одразу повертаємо
        # актуальний графік із пам'яті/БД (фоновий планувальник оновлює всі черги кожні 5 хв)
        if not force_refresh:
            cached = await db.get_schedule(loc.queue, target_date)
            if cached:
                if target_date > today and cached.total_outage_minutes == 0:
                    return None
                return cached

        # 2. Якщо в кеші/БД графіка ще немає або натиснуто «🔄 Оновити зараз» (force_refresh=True)
        all_regional = await voe_client.fetch_all_queues_for_region(
            region=loc.region,
            ttl_seconds=0.0 if force_refresh else 60.0,
        )

        # Якщо натиснуто «🔄 Оновити зараз» і локація має адресу — перевіряємо, чи не змінилася черга
        if force_refresh and loc.city_name:
            verified_q: Optional[str] = None
            if loc.region == REGION_IF and loc.street_name and loc.house_num:
                verified_q = await voe_client.if_client.verify_address_queue(
                    city_name=loc.city_name,
                    street_name=loc.street_name,
                    house_num=loc.house_num,
                )
            elif loc.region == REGION_VOE and (loc.street_name or loc.city_id):
                verified_q = await voe_client.verify_address_queue(
                    city_name=loc.city_name,
                    street_name=loc.street_name or "",
                    house_num=loc.house_num or "",
                    city_id=loc.city_id or "",
                    street_id=loc.street_id or "",
                    house_id=loc.house_id or "",
                    known_schedules=all_regional,
                )
            if verified_q and is_valid_queue_key(verified_q) and verified_q != loc.queue:
                updated_loc = await db.update_location_queue(
                    loc_id=loc.id,
                    telegram_id=loc.telegram_id,
                    new_queue=verified_q,
                )
                loc.queue = verified_q
                if updated_loc:
                    loc.house_id = updated_loc.house_id

        target_key = (loc.queue, target_date)
        if target_key in all_regional:
            cand = all_regional[target_key]
            if target_date <= today or cand.total_outage_minutes > 0:
                await _save_schedule(cand)

        if all_regional:
            asyncio.create_task(
                _save_other_schedules_bg(all_regional, skip_key=target_key, today=today)
            )

        if not force_refresh:
            cached = await db.get_schedule(loc.queue, target_date)
            if cached:
                if target_date > today and cached.total_outage_minutes == 0:
                    return None
                return cached

        if target_key in all_regional:
            cand = all_regional[target_key]
            if target_date <= today or cand.total_outage_minutes > 0:
                return cand

        # 3. Регіональний fallback за точною адресою
        if loc.region == REGION_IF and loc.city_name and loc.street_name and loc.house_num:
            if_res = await voe_client.if_client.lookup_address(
                loc.city_name, loc.street_name, loc.house_num
            )
            if if_res.schedules:
                for d, s in if_res.schedules.items():
                    if d > today and s.total_outage_minutes == 0:
                        continue
                    s.queue = loc.queue
                    await _save_schedule(s)
                if target_date in if_res.schedules:
                    cand = if_res.schedules[target_date]
                    if target_date <= today or cand.total_outage_minutes > 0:
                        return cand
        elif loc.region == REGION_VOE and loc.city_id and loc.street_id and loc.house_id:
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
                    if d > today and s.total_outage_minutes == 0:
                        continue
                    s.queue = loc.queue
                    await _save_schedule(s)
                if target_date in addr_res.schedules:
                    cand = addr_res.schedules[target_date]
                    if target_date <= today or cand.total_outage_minutes > 0:
                        return cand

        final_sched = await db.get_schedule(loc.queue, target_date)
        if final_sched and target_date > today and final_sched.total_outage_minutes == 0:
            return None
        return final_sched

    async def _reply_with_schedule_card(
        message: Message,
        loc: UserLocation,
        sched: DaySchedule,
        now: datetime,
        day_tag: str,
    ) -> tuple[float, float]:
        t_r0 = time.perf_counter()
        paused_prefix = (
            "⏸ <i>Ця локація зараз на паузі (фонові сповіщення вимкнено)</i>"
            if not loc.is_active
            else None
        )
        text = format_schedule_message(
            schedule=sched,
            location_label=loc.label,
            address_str=loc.display_address,
            now=now,
            header_prefix=paused_prefix,
        )
        img_bytes = render_schedule_image(sched, now=now)
        kb = schedule_view_actions_kb(loc.id, day_tag, is_paused=not loc.is_active)
        t_r1 = time.perf_counter()
        safe_q = sched.queue.replace(":", "_")

        if img_bytes and len(text) <= 1000:
            fname = f"svitlo_{safe_q}_{sched.schedule_date}.png"
            if message.bot:
                await send_photo_cached(
                    bot=message.bot,
                    chat_id=message.chat.id,
                    img_bytes=img_bytes,
                    filename=fname,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            else:
                photo = BufferedInputFile(img_bytes, filename=fname)
                await message.answer_photo(
                    photo=photo, caption=text, parse_mode="HTML", reply_markup=kb
                )
        else:
            if img_bytes:
                fname = f"svitlo_{safe_q}_{sched.schedule_date}.png"
                if message.bot:
                    await send_photo_cached(
                        bot=message.bot,
                        chat_id=message.chat.id,
                        img_bytes=img_bytes,
                        filename=fname,
                    )
                else:
                    photo = BufferedInputFile(img_bytes, filename=fname)
                    await message.answer_photo(photo=photo)
            await message.answer(text, parse_mode="HTML", reply_markup=kb)

        t_r2 = time.perf_counter()
        return (t_r1 - t_r0) * 1000.0, (t_r2 - t_r1) * 1000.0

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
        t0 = time.perf_counter()
        action, loc_id_str, day_tag = parts[1], parts[2], parts[3]
        loc = await db.get_location_by_id(int(loc_id_str), call.from_user.id)
        t_loc = time.perf_counter()
        if not loc:
            await call.answer("Локацію не знайдено", show_alert=True)
            return

        now = datetime.now(settings.tz)
        target_date = now.date() if day_tag == "today" else now.date() + timedelta(days=1)
        force = action == "refresh"
        if force:
            await call.answer("🔄 Оновлюю графік...")
        else:
            await call.answer()

        t_sched0 = time.perf_counter()
        sched = await _get_or_fetch_schedule(loc, target_date, force_refresh=force)
        t_sched1 = time.perf_counter()
        if not sched:
            day_label = "сьогодні" if day_tag == "today" else "завтра"
            await call.message.answer(
                f"📍 <b>{loc.label}</b> ({loc.region_short_label} · Черга {loc.queue_code})\n"
                f"⏳ Графік на <b>{day_label} ({target_date.strftime('%d.%m.%Y')})</b> ще не з'явився.",
                parse_mode="HTML",
                reply_markup=schedule_view_actions_kb(loc.id, day_tag),
            )
            return

        render_ms, send_ms = await _reply_with_schedule_card(
            call.message, loc, sched, now, day_tag  # type: ignore
        )
        total_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "PERF | cb=sched:%s:%s | user=%s | queue=%s | total=%.0fms "
            "(db_loc=%.0fms, sched=%.0fms, render=%.0fms, tg_send=%.0fms)",
            action,
            day_tag,
            call.from_user.id,
            loc.queue,
            total_ms,
            (t_loc - t0) * 1000.0,
            (t_sched1 - t_sched0) * 1000.0,
            render_ms,
            send_ms,
        )

    def _format_locations_list_text(locations: list[UserLocation]) -> str:
        lines = ["📍 <b>Ваші збережені локації:</b>\n"]
        for loc in locations:
            status_badge = "🟢" if loc.is_active else "⏸"
            paused_tag = " <i>(на паузі)</i>" if not loc.is_active else ""
            addr_suffix = (
                f" ({loc.display_address})"
                if loc.display_address != f"Черга {loc.queue_code}"
                else ""
            )
            lines.append(
                f"{status_badge} <b>{loc.label}</b>{paused_tag} — {loc.region_short_label}, Черга <b>{loc.queue_code}</b>{addr_suffix}"
            )
        if len(locations) > 1:
            lines.append(
                "\n💡 <i>Кнопка «🎯 Тільки ця» залишає активною лише обрану локацію, а інші зберігає на паузі (без видалення).</i>"
            )
        else:
            lines.append("\nНатисніть на локацію для перегляду графіка або додайте нову:")
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # Мої локації: перегляд, перемикання активності, додавання, видалення
    # -------------------------------------------------------------------------
    @router.message(F.text == BTN_LOCATIONS)
    @router.message(Command("locations"))
    async def handle_locations(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return
        locations = await db.get_user_locations(message.from_user.id)
        if not locations:
            await state.set_state(AddLocationStates.choosing_region)
            await message.answer(
                "📍 У вас ще немає збережених локацій.\nОберіть вашу область:",
                reply_markup=choose_region_kb(),
            )
            return

        await message.answer(
            _format_locations_list_text(locations),
            parse_mode="HTML",
            reply_markup=locations_list_kb(locations),
        )

    @router.callback_query(F.data == "loc:add")
    async def cb_loc_add(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(AddLocationStates.choosing_region)
        await call.answer()
        if call.message:
            await call.message.answer(
                "➕ <b>Додавання нової локації</b>\nОберіть область:",
                parse_mode="HTML",
                reply_markup=choose_region_kb(),
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
                f"📍 <b>{loc.label}</b> ({loc.region_short_label} · Черга {loc.queue_code})\n"
                f"Графік на сьогодні ще не завантажено. Натисніть «Оновити зараз».",
                parse_mode="HTML",
                reply_markup=schedule_view_actions_kb(
                    loc.id, "today", is_paused=not loc.is_active
                ),
            )

    @router.callback_query(F.data == "loc:list")
    async def cb_loc_list(call: CallbackQuery, state: FSMContext) -> None:
        if not call.from_user or not call.message:
            return
        await call.answer()
        locations = await db.get_user_locations(call.from_user.id)
        if not locations:
            await state.set_state(AddLocationStates.choosing_region)
            await call.message.edit_text(
                "📍 У вас ще немає збережених локацій.\nОберіть вашу область:",
                reply_markup=choose_region_kb(),
            )
            return
        await call.message.edit_text(
            _format_locations_list_text(locations),
            parse_mode="HTML",
            reply_markup=locations_list_kb(locations),
        )

    @router.callback_query(F.data.startswith("loc:toggle:"))
    async def cb_loc_toggle(call: CallbackQuery) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        loc_id = int(call.data.split(":")[2])
        loc = await db.get_location_by_id(loc_id, call.from_user.id)
        if not loc:
            await call.answer("Локацію не знайдено", show_alert=True)
            return

        new_active = not loc.is_active
        await db.set_location_active(loc_id, call.from_user.id, new_active)
        locations = await db.get_user_locations(call.from_user.id)
        toast = (
            f"🔔 Сповіщення для «{loc.label}» увімкнено"
            if new_active
            else f"🔕 «{loc.label}» поставлено на паузу"
        )
        await call.answer(toast)
        try:
            await call.message.edit_text(
                _format_locations_list_text(locations),
                parse_mode="HTML",
                reply_markup=locations_list_kb(locations),
            )
        except Exception:
            await call.message.answer(
                _format_locations_list_text(locations),
                parse_mode="HTML",
                reply_markup=locations_list_kb(locations),
            )

    @router.callback_query(F.data.startswith("loc:only:"))
    async def cb_loc_only(call: CallbackQuery) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        loc_id = int(call.data.split(":")[2])
        target = await db.set_only_location_active(loc_id, call.from_user.id)
        if not target:
            await call.answer("Локацію не знайдено", show_alert=True)
            return

        locations = await db.get_user_locations(call.from_user.id)
        await call.answer(f"🎯 Активна лише «{target.label}»!")
        try:
            await call.message.edit_text(
                _format_locations_list_text(locations),
                parse_mode="HTML",
                reply_markup=locations_list_kb(locations),
            )
        except Exception:
            await call.message.answer(
                _format_locations_list_text(locations),
                parse_mode="HTML",
                reply_markup=locations_list_kb(locations),
            )

    @router.callback_query(F.data.startswith("loc:only_new:"))
    async def cb_loc_only_new(call: CallbackQuery) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        loc_id = int(call.data.split(":")[2])
        target = await db.set_only_location_active(loc_id, call.from_user.id)
        if not target:
            await call.answer("Локацію не знайдено", show_alert=True)
            return

        await call.answer(f"🎯 Активна лише «{target.label}»!")
        msg_txt = (
            f"🎯 Готово! Зараз активна лише локація <b>{target.label}</b> "
            f"({target.region_short_label} · Черга <b>{target.queue_code}</b>).\n"
            "<i>Інші локації збережено на паузі — перемкнутися назад можна в будь-який момент у «📍 Мої локації».</i>"
        )
        if getattr(call.message, "photo", None):
            try:
                await call.message.edit_reply_markup(
                    reply_markup=schedule_view_actions_kb(target.id, "today", is_paused=False)
                )
            except Exception:
                pass
            await call.message.answer(msg_txt, parse_mode="HTML")
        else:
            try:
                await call.message.edit_text(msg_txt, parse_mode="HTML")
            except Exception:
                await call.message.answer(msg_txt, parse_mode="HTML")

    @router.callback_query(F.data.startswith("loc:resume_show:"))
    async def cb_loc_resume_and_show(call: CallbackQuery) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        parts = call.data.split(":")
        loc_id = int(parts[2])
        day_tag = parts[3] if len(parts) > 3 else "today"
        loc = await db.set_location_active(loc_id, call.from_user.id, True)
        if not loc:
            await call.answer("Локацію не знайдено", show_alert=True)
            return

        await call.answer(f"🔔 «{loc.label}» увімкнено!")
        try:
            await call.message.edit_text(
                f"🔔 Локацію <b>{loc.label}</b> ({loc.region_short_label} · Черга <b>{loc.queue_code}</b>) знову увімкнено!",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await _send_schedules_for_day(call.message, call.from_user.id, day_tag)  # type: ignore

    @router.callback_query(F.data.startswith("loc:chq:"))
    async def cb_loc_change_queue_menu(call: CallbackQuery) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        loc_id = int(call.data.split(":")[2])
        loc = await db.get_location_by_id(loc_id, call.from_user.id)
        if not loc:
            await call.answer("Локацію не знайдено", show_alert=True)
            return
        await call.answer()
        addr_line = (
            f"\n🏠 Адреса: <b>{loc.display_address}</b>"
            if loc.display_address != f"Черга {loc.queue_code}"
            else ""
        )
        await call.message.edit_text(
            f"🔢 <b>Зміна черги ГПВ для локації «{loc.label}»</b>\n"
            f"🗺 Область: <b>{loc.region_label}</b>{addr_line}\n"
            f"Поточна черга: <b>{loc.queue_code}</b>\n\n"
            "Оберіть нову чергу зі списку нижче або натисніть перевірку за адресою:",
            parse_mode="HTML",
            reply_markup=change_location_queue_kb(loc),
        )

    @router.callback_query(F.data.startswith("loc:verify:"))
    async def cb_loc_verify_queue(call: CallbackQuery) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        loc_id = int(call.data.split(":")[2])
        loc = await db.get_location_by_id(loc_id, call.from_user.id)
        if not loc:
            await call.answer("Локацію не знайдено", show_alert=True)
            return

        await call.answer("🔍 Перевіряю актуальну чергу за вашою адресою...")
        verified_q: Optional[str] = None
        if loc.region == REGION_IF:
            verified_q = await voe_client.if_client.verify_address_queue(
                city_name=loc.city_name or "",
                street_name=loc.street_name or "",
                house_num=loc.house_num or "",
            )
        else:
            all_github = await voe_client.fetch_all_queues_from_github()
            verified_q = await voe_client.verify_address_queue(
                city_name=loc.city_name or "",
                street_name=loc.street_name or "",
                house_num=loc.house_num or "",
                city_id=loc.city_id or "",
                street_id=loc.street_id or "",
                house_id=loc.house_id or "",
                known_schedules=all_github,
            )

        if not verified_q and loc.city_name:
            verified_q = await db.lookup_cached_address(
                loc.city_name, loc.street_name, loc.house_num, region=loc.region
            )

        if not verified_q or not is_valid_queue_key(verified_q):
            await call.message.edit_text(
                f"⚠️ Не вдалося автоматично визначити чергу для адреси <b>{loc.display_address}</b>.\n"
                "Будь ласка, оберіть актуальну чергу вручну:",
                parse_mode="HTML",
                reply_markup=change_location_queue_kb(loc),
            )
            return

        _, verified_code = split_queue_key(verified_q)
        if verified_q == loc.queue:
            await call.message.edit_text(
                f"✅ За даними довідника для <b>{loc.display_address}</b> встановлена черга <b>{loc.queue_code}</b>.\n\n"
                "Якщо вашу лінію тимчасово перемкнули на іншу підчергу, ви можете обрати її вручну нижче:",
                parse_mode="HTML",
                reply_markup=change_location_queue_kb(loc),
            )
            return

        old_code = loc.queue_code
        updated_loc = await db.update_location_queue(
            loc_id=loc.id,
            telegram_id=call.from_user.id,
            new_queue=verified_q,
        )
        loc = updated_loc or loc
        await call.message.edit_text(
            f"⚡🔄 <b>Чергу оновлено!</b>\n"
            f"📍 Локація: <b>{loc.label}</b> ({loc.display_address})\n"
            f"🔢 Була черга: <b>{old_code}</b> ➡️ Нова черга: <b>{verified_code}</b>",
            parse_mode="HTML",
        )
        now = datetime.now(settings.tz)
        sched = await _get_or_fetch_schedule(loc, now.date(), force_refresh=False)
        if sched:
            await _reply_with_schedule_card(call.message, loc, sched, now, "today")  # type: ignore

    @router.callback_query(F.data.startswith("loc:setq:"))
    async def cb_loc_set_queue(call: CallbackQuery) -> None:
        if not call.data or not call.from_user or not call.message:
            return
        parts = call.data.split(":")
        if len(parts) < 4:
            return
        loc_id = int(parts[2])
        new_queue_code = parts[3]
        if new_queue_code not in ALL_QUEUES:
            await call.answer("Невірна черга", show_alert=True)
            return

        loc = await db.get_location_by_id(loc_id, call.from_user.id)
        if not loc:
            await call.answer("Локацію не знайдено", show_alert=True)
            return

        old_code = loc.queue_code
        resolved_new_key = make_queue_key(loc.region, new_queue_code)
        updated_loc = await db.update_location_queue(
            loc_id=loc.id,
            telegram_id=call.from_user.id,
            new_queue=resolved_new_key,
        )
        loc = updated_loc or loc

        # Якщо локація має точну адресу — оновлюємо in-memory override та синхронізуємо сусідів
        if loc.city_name and loc.street_name and loc.house_num:
            if loc.region == REGION_IF:
                voe_client.if_client.set_address_override(
                    loc.city_name, loc.street_name, loc.house_num, new_queue_code
                )
            else:
                voe_client.address_index.set_address_override(
                    loc.city_name, loc.street_name, loc.house_num, new_queue_code
                )
            if notifier is not None:
                asyncio.create_task(notifier.sync_locations_queues())

        await call.answer(f"✅ Чергу змінено на {new_queue_code}!")
        try:
            await call.message.edit_text(
                f"✅ Для локації <b>{loc.label}</b> ({loc.region_short_label}) встановлено чергу <b>{new_queue_code}</b> (була {old_code}).",
                parse_mode="HTML",
            )
        except Exception:
            await call.message.answer(
                f"✅ Для локації <b>{loc.label}</b> ({loc.region_short_label}) встановлено чергу <b>{new_queue_code}</b> (була {old_code}).",
                parse_mode="HTML",
            )

        now = datetime.now(settings.tz)
        sched = await _get_or_fetch_schedule(loc, now.date(), force_refresh=False)
        if sched:
            await _reply_with_schedule_card(call.message, loc, sched, now, "today")  # type: ignore

    @router.callback_query(F.data.startswith("loc:del:"))
    async def cb_loc_del(call: CallbackQuery, state: FSMContext) -> None:
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
            await state.set_state(AddLocationStates.choosing_region)
            await call.message.edit_text(
                "📍 Усі локації видалено. Оберіть область для нової локації:",
                reply_markup=choose_region_kb(),
            )
        else:
            await call.message.edit_text(
                _format_locations_list_text(locations),
                parse_mode="HTML",
                reply_markup=locations_list_kb(locations),
            )

    # -------------------------------------------------------------------------
    # FSM: Додавання локації (Область -> Черга або Адреса -> Назва)
    # -------------------------------------------------------------------------
    @router.callback_query(F.data == "addloc:cancel")
    async def cb_addloc_cancel(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.answer("Скасовано")
        if call.message:
            await call.message.edit_text("❌ Додавання локації скасовано.")

    @router.callback_query(F.data == "addloc:back_reg")
    async def cb_addloc_back_reg(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(AddLocationStates.choosing_region)
        await call.answer()
        if call.message:
            await call.message.edit_text(
                "🗺 <b>Оберіть область:</b>",
                parse_mode="HTML",
                reply_markup=choose_region_kb(),
            )

    @router.callback_query(F.data.startswith("addloc:reg:"))
    async def cb_addloc_choose_region(call: CallbackQuery, state: FSMContext) -> None:
        if not call.data or not call.message:
            return
        chosen_region = call.data.split(":")[2]
        if chosen_region not in (REGION_IF, REGION_VOE):
            chosen_region = REGION_VOE
        await state.clear()
        await state.update_data(region=chosen_region)
        await state.set_state(AddLocationStates.choosing_method)
        await call.answer()
        await call.message.edit_text(
            f"🗺 Область: <b>{get_region_label(chosen_region)}</b>\n\n"
            "Оберіть зручний спосіб додавання вашої локації:",
            parse_mode="HTML",
            reply_markup=add_location_method_kb(chosen_region),
        )

    @router.callback_query(F.data == "addloc:method:queue")
    async def cb_addloc_method_queue(call: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        region = data.get("region") or REGION_VOE
        await state.update_data(region=region)
        await state.set_state(AddLocationStates.choosing_queue)
        await call.answer()
        if call.message:
            await call.message.edit_text(
                f"🗺 Область: <b>{get_region_label(region)}</b>\n"
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
        queue_code = call.data.split(":")[2]
        data = await state.get_data()
        region = data.get("region") or REGION_VOE
        full_queue = make_queue_key(region, queue_code)
        await state.update_data(queue=full_queue)
        await state.set_state(AddLocationStates.entering_label)
        await call.answer()
        await call.message.edit_text(
            f"✅ Обрано: <b>{get_region_label(region, short=True)} · Черга {queue_code}</b>\n\n"
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
                "Надішліть або перешліть мені картинку таблиці розкладу на 24 години у цей чат.\n"
                "У підписі до фото можете вказати вашу чергу (наприклад <code>3.2</code>)!",
                parse_mode="HTML",
            )

    async def _resolve_if_address_and_proceed(
        message: Message,
        state: FSMContext,
        city_name: str,
        street_name: str,
        house_num: str,
    ) -> None:
        """Визначає чергу за адресою в Івано-Франківській області та переводить у вибір назви."""
        res = await voe_client.if_client.lookup_address(city_name, street_name, house_num)
        await state.update_data(
            region=REGION_IF,
            city_id="if",
            city_name=res.city_display or city_name,
            street_id="if_st",
            street_name=res.street_display or street_name,
            house_num=res.house_display or house_num,
        )
        disp_addr = ", ".join(
            p for p in (res.city_display or city_name, res.street_display or street_name, res.house_display or house_num) if p
        )

        if res.status == "exact" and res.queue_key and res.queue_code:
            for s in res.schedules.values():
                await _save_schedule(s)
            await state.update_data(queue=res.queue_key)
            await state.set_state(AddLocationStates.entering_label)
            await message.answer(
                f"✅ Адресу знайдено: <b>{disp_addr}</b>\n"
                f"🔢 Ваша черга ГПВ: <b>{res.queue_code}</b>\n\n"
                "🏷 Оберіть назву для цієї локації або напишіть свою:",
                parse_mode="HTML",
                reply_markup=label_presets_kb(),
            )
            return

        if res.status == "many" and res.possible_queues:
            await state.set_state(AddLocationStates.choosing_queue)
            await message.answer(
                f"🏠 За адресою <b>{disp_addr}</b> можливі декілька черг: "
                f"<b>{', '.join(res.possible_queues)}</b>.\n"
                "Оберіть вашу чергу зі списку:",
                parse_mode="HTML",
                reply_markup=queue_picker_kb(allowed_queues=res.possible_queues),
            )
            return

        cached_q = await db.lookup_cached_address(
            res.city_display or city_name,
            res.street_display or street_name,
            res.house_display or house_num,
            region=REGION_IF,
        )
        if cached_q and is_valid_queue_key(cached_q):
            _, c_code = split_queue_key(cached_q)
            await state.update_data(queue=cached_q)
            await state.set_state(AddLocationStates.entering_label)
            await message.answer(
                f"✅ Адресу знайдено: <b>{disp_addr}</b>\n"
                f"🔢 Ваша черга ГПВ: <b>{c_code}</b>\n\n"
                "🏷 Оберіть назву для цієї локації або напишіть свою:",
                parse_mode="HTML",
                reply_markup=label_presets_kb(),
            )
            return

        if res.status == "no_queue":
            await state.set_state(AddLocationStates.choosing_queue)
            await message.answer(
                f"ℹ️ За адресою <b>{disp_addr}</b> графіки погодинних вимкнень не застосовуються.\n"
                "Якщо ви все одно хочете відстежувати певну чергу, оберіть її зі списку:",
                parse_mode="HTML",
                reply_markup=queue_picker_kb(),
            )
            return

        await state.set_state(AddLocationStates.choosing_queue)
        await message.answer(
            f"⚠️ Не вдалося автоматично знайти адресу <b>{disp_addr}</b> у базі.\n"
            "Перевірте правильність назви вулиці/номера або оберіть вашу чергу ГПВ вручну:",
            parse_mode="HTML",
            reply_markup=queue_picker_kb(),
        )

    # Пошук за адресою: Крок 1 — Місто або швидкий пошук одним рядком
    @router.callback_query(F.data == "addloc:method:address")
    async def cb_addloc_method_address(call: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        region = data.get("region") or REGION_VOE
        await state.update_data(region=region)

        if region == REGION_IF:
            if_popular = [
                AutocompleteItem(id=c["id"], label=c["label"])
                for c in voe_client.if_client.get_popular_cities(limit=10)
            ]
            await state.update_data(
                city_candidates=[
                    {"id": c.id, "label": c.label, "queue": None}
                    for c in if_popular
                ]
            )
            await state.set_state(AddLocationStates.entering_city)
            await call.answer()
            if call.message:
                await call.message.edit_text(
                    "🏙 <b>Пошук черги за адресою (Івано-Франківська область)</b>\n\n"
                    "1️⃣ <b>Оберіть населений пункт</b> із популярних нижче або введіть назву будь-якого міста чи села області "
                    "(наприклад: <code>Коломия</code>, <code>Івано-Франківськ</code>, <code>Шепарівці</code>).\n\n"
                    "2️⃣ Або <b>напишіть одразу повну адресу</b> одним рядком "
                    "(наприклад: <code>Коломия Чорновола 1</code> або <code>Коломия, Січових Стрільців, 10</code>) — "
                    "і я миттєво знайду вашу чергу!",
                    parse_mode="HTML",
                    reply_markup=popular_cities_kb(if_popular, "addloc:city"),
                )
            return

        popular = voe_client.get_popular_cities(limit=10)
        await state.update_data(
            city_candidates=[
                {"id": c.id, "label": c.label, "queue": c.queue}
                for c in popular
            ]
        )
        await state.set_state(AddLocationStates.entering_city)
        await call.answer()
        if call.message:
            await call.message.edit_text(
                "🏙 <b>Пошук черги за адресою (Вінницька область)</b>\n\n"
                "1️⃣ <b>Оберіть населений пункт</b> із популярних нижче або введіть назву міста/села "
                "(наприклад: <code>Вінниця</code>, <code>Ладижин</code>, <code>Бар</code>).\n\n"
                "2️⃣ Або <b>напишіть одразу повну адресу</b> одним рядком "
                "(наприклад: <code>Соборна 24</code>, <code>Вінниця Келецька 120</code>, "
                "<code>Агрономічне Мічуріна</code>) — і я миттєво знайду вашу чергу!",
                parse_mode="HTML",
                reply_markup=popular_cities_kb(popular, "addloc:city"),
            )

    @router.message(AddLocationStates.entering_city, F.text)
    async def msg_entering_city(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if len(query) < 2:
            await message.answer("Введіть щонайменше 2 літери назви населеного пункту або адреси:")
            return

        data = await state.get_data()
        region = data.get("region") or REGION_VOE

        if region == REGION_IF:
            # 1. Спроба розпізнати повну адресу одним рядком (напр. "Коломия Чорновола 1")
            if re.search(r"\d", query) or query.count(",") >= 2:
                parsed_free = parse_freeform_if_address(query)
                if parsed_free:
                    c_part, st_part, h_part = parsed_free
                    await _resolve_if_address_and_proceed(
                        message, state, c_part, st_part, h_part
                    )
                    return

            if_cities_raw = voe_client.if_client.search_cities(query, limit=10)
            if len(if_cities_raw) == 1:
                only = if_cities_raw[0]
                await _proceed_with_chosen_city(
                    message,
                    state,
                    {"id": only["id"], "label": only["label"], "queue": None},
                    edit=False,
                )
                return

            if_items = [
                AutocompleteItem(id=c["id"], label=c["label"]) for c in if_cities_raw
            ]
            await state.update_data(
                city_candidates=[
                    {"id": c.id, "label": c.label, "queue": None}
                    for c in if_items[:10]
                ]
            )
            await state.set_state(AddLocationStates.selecting_city)
            await message.answer(
                "🏙 Оберіть ваш населений пункт зі списку:",
                reply_markup=autocomplete_items_kb(if_items, "addloc:city"),
            )
            return

        # 1. Якщо запит містить номер будинку, кому або кілька слів (вулиця + будинок) —
        # пробуємо швидкий пошук повної адреси одним рядком (Вінницька область)
        has_digits_or_comma = bool(re.search(r"\d|,", query))
        cities = await voe_client.search_cities(query)

        if has_digits_or_comma or not cities:
            free_matches = voe_client.search_address_freeform(query, limit=8)
            if len(free_matches) == 1:
                m = free_matches[0]
                await state.update_data(
                    city_id=m["city_id"],
                    city_name=m["city_name"],
                    street_id=m.get("street_id"),
                    street_name=m.get("street_name"),
                    house_num=m.get("house"),
                    queue=m["queue"],
                )
                await state.set_state(AddLocationStates.entering_label)
                await message.answer(
                    f"✅ Адресу знайдено: <b>{m['display']}</b>\n"
                    f"🔢 Ваша черга ГПВ: <b>{m['queue']}</b>\n\n"
                    "🏷 Оберіть назву для цієї локації або напишіть свою:",
                    parse_mode="HTML",
                    reply_markup=label_presets_kb(),
                )
                return
            elif len(free_matches) > 1:
                items = [
                    AutocompleteItem(
                        id=str(idx),
                        label=f"{m['display']} (черга {m['queue']})",
                        queue=m["queue"],
                    )
                    for idx, m in enumerate(free_matches[:10])
                ]
                await state.update_data(freeform_candidates=free_matches[:10])
                await state.set_state(AddLocationStates.selecting_house)
                await message.answer(
                    "🏠 Знайдено кілька варіантів за вашим запитом. Оберіть потрібний:",
                    reply_markup=autocomplete_items_kb(items, "addloc:free"),
                )
                return

        if not cities:
            await message.answer(
                "⚠️ На жаль, не вдалося знайти таку адресу чи населений пункт у довіднику.\n"
                "Спробуйте ввести лише назву міста/села або оберіть чергу вручну:",
                reply_markup=queue_picker_kb(),
            )
            await state.set_state(AddLocationStates.choosing_queue)
            return

        # Якщо знайдено рівно 1 населений пункт — переходимо одразу далі без зайвого кліку
        if len(cities) == 1:
            c = cities[0]
            await _proceed_with_chosen_city(
                message, state, {"id": c.id, "label": c.label, "queue": c.queue}, edit=False
            )
            return

        await state.update_data(
            city_candidates=[
                {"id": c.id, "label": c.label, "queue": c.queue}
                for c in cities[:10]
            ]
        )
        await state.set_state(AddLocationStates.selecting_city)
        await message.answer(
            "🏙 Оберіть ваш населений пункт зі списку:",
            reply_markup=autocomplete_items_kb(cities, "addloc:city"),
        )

    async def _proceed_with_chosen_city(
        msg_or_call_msg: Message,
        state: FSMContext,
        chosen: dict,
        edit: bool = True,
    ) -> None:
        city_id = chosen["id"]
        city_name = chosen["label"]
        data = await state.get_data()
        region = data.get("region") or (
            REGION_IF if str(city_id).startswith("if") else REGION_VOE
        )
        await state.update_data(region=region, city_id=city_id, city_name=city_name)

        if region == REGION_IF:
            await state.set_state(AddLocationStates.entering_street)
            txt = (
                f"🏙 Населений пункт: <b>{city_name}</b>\n\n"
                "🛣 <b>Крок 2 із 3: Вулиця</b>\n"
                "Введіть назву вулиці або одразу вулицю з номером будинку "
                "(наприклад: <code>Чорновола 1</code> або <code>Січових Стрільців</code>):"
            )
            if edit:
                await msg_or_call_msg.edit_text(txt, parse_mode="HTML")
            else:
                await msg_or_call_msg.answer(txt, parse_mode="HTML")
            return

        # Перевіряємо, чи має населений пункт деталізацію за вулицями в локальному індексі VOE
        c_obj = voe_client.address_index.get_city(city_id)
        if c_obj and not c_obj.get("streets"):
            c_queues = c_obj.get("queues", [])
            if len(c_queues) == 1:
                q_code = c_queues[0]
                await state.update_data(queue=q_code)
                await state.set_state(AddLocationStates.entering_label)
                txt = (
                    f"✅ Населений пункт: <b>{city_name}</b>\n"
                    f"🔢 Ваша черга ГПВ: <b>{q_code}</b>\n\n"
                    "🏷 Оберіть назву для цієї локації або напишіть свою:"
                )
                if edit:
                    await msg_or_call_msg.edit_text(txt, parse_mode="HTML", reply_markup=label_presets_kb())
                else:
                    await msg_or_call_msg.answer(txt, parse_mode="HTML", reply_markup=label_presets_kb())
                return
            elif len(c_queues) > 1:
                await state.set_state(AddLocationStates.choosing_queue)
                txt = (
                    f"🏘 Населений пункт: <b>{city_name}</b>\n"
                    f"За офіційним графіком тут діють черги: <b>{', '.join(c_queues)}</b>.\n"
                    "Оберіть вашу чергу зі списку:"
                )
                kb = queue_picker_kb(allowed_queues=c_queues)
                if edit:
                    await msg_or_call_msg.edit_text(txt, parse_mode="HTML", reply_markup=kb)
                else:
                    await msg_or_call_msg.answer(txt, parse_mode="HTML", reply_markup=kb)
                return

        await state.set_state(AddLocationStates.entering_street)
        txt = (
            f"🏙 Населений пункт: <b>{city_name}</b>\n\n"
            "🛣 <b>Крок 2 із 3: Вулиця</b>\n"
            "Введіть назву вулиці або одразу вулицю з номером будинку "
            "(наприклад: <code>Соборна 24</code> або <code>Келецька</code>):"
        )
        if edit:
            await msg_or_call_msg.edit_text(txt, parse_mode="HTML")
        else:
            await msg_or_call_msg.answer(txt, parse_mode="HTML")

    @router.callback_query(
        AddLocationStates.entering_city, F.data.startswith("addloc:city:")
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
        await call.answer()
        await _proceed_with_chosen_city(call.message, state, chosen, edit=True)  # type: ignore

    @router.message(AddLocationStates.entering_street, F.text)
    async def msg_entering_street(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if len(query) < 2:
            await message.answer("Введіть щонайменше 2 літери назви вулиці:")
            return

        data = await state.get_data()
        region = data.get("region") or REGION_VOE
        city_id = data.get("city_id", "")
        city_name = data.get("city_name", "")

        hm = re.search(
            r"(?:[,\s]+(?:буд\.?|будинок|№)?\s*|\s+)(\d+[а-яіїєґА-ЯІЇЄҐa-zA-Z]?(?:/\d+[а-яіїєґА-ЯІЇЄҐa-zA-Z]?)?)$",
            query,
        )

        if region == REGION_IF:
            if hm and query[: hm.start()].strip(" ,;."):
                street_part = query[: hm.start()].strip(" ,;.")
                house_part = hm.group(1).strip()
                await _resolve_if_address_and_proceed(
                    message, state, city_name, street_part, house_part
                )
                return

            street_disp = format_if_street_display(query)
            await state.update_data(street_id="if_st", street_name=street_disp)
            await state.set_state(AddLocationStates.entering_house)
            await message.answer(
                f"🏙 {city_name}, <b>{street_disp}</b>\n\n"
                "🏠 <b>Крок 3 із 3: Номер будинку</b>\n"
                "Введіть номер будинку (наприклад: <code>10</code>, <code>25А</code>):",
                parse_mode="HTML",
            )
            return

        # Якщо користувач ввів одразу вулицю та номер будинку (Вінницька обл., напр. "Соборна 24")
        if hm and query[: hm.start()].strip(" ,;."):
            street_part = query[: hm.start()].strip(" ,;.")
            house_part = hm.group(1).strip()
            streets_with_num = await voe_client.search_streets(city_id, street_part)
            if len(streets_with_num) == 1:
                st = streets_with_num[0]
                await state.update_data(street_id=st.id, street_name=st.label)
                await _resolve_or_pick_house(message, state, st.id, st.label, house_part)
                return

        streets = await voe_client.search_streets(city_id, query)
        if not streets:
            c_obj = voe_client.address_index.get_city(city_id)
            allowed_q = c_obj.get("queues") if c_obj else None
            await message.answer(
                f"⚠️ Вулицю «{query}» у <b>{city_name}</b> не знайдено.\n"
                "Спробуйте ввести іншу назву вулиці або оберіть чергу вручну:",
                parse_mode="HTML",
                reply_markup=queue_picker_kb(allowed_queues=allowed_q),
            )
            return

        if len(streets) == 1:
            st = streets[0]
            await _proceed_with_chosen_street(
                message, state, {"id": st.id, "label": st.label, "queue": st.queue}, edit=False
            )
            return

        await state.update_data(
            street_candidates=[
                {"id": s.id, "label": s.label, "queue": s.queue}
                for s in streets[:10]
            ]
        )
        await state.set_state(AddLocationStates.selecting_street)
        await message.answer(
            "🛣 Оберіть вулицю зі списку:",
            reply_markup=autocomplete_items_kb(streets, "addloc:street"),
        )

    async def _proceed_with_chosen_street(
        msg_or_call_msg: Message,
        state: FSMContext,
        chosen: dict,
        edit: bool = True,
    ) -> None:
        street_id = chosen["id"]
        street_name = chosen["label"]
        data = await state.get_data()
        city_name = data.get("city_name", "")
        await state.update_data(street_id=street_id, street_name=street_name)

        # Перевіряємо, чи вся вулиця належить до однієї черги без поділу за номерами будинків
        _, st_obj = voe_client.address_index.get_street(street_id)
        if st_obj:
            records = st_obj.get("records", [])
            st_queues = sorted({r["queue"] for r in records if r.get("queue")})
            has_house_ranges = any(bool(r.get("houses")) for r in records)
            if len(st_queues) == 1 and not has_house_ranges:
                q_code = st_queues[0]
                await state.update_data(queue=q_code)
                await state.set_state(AddLocationStates.entering_label)
                txt = (
                    f"✅ Адресу знайдено: <b>{city_name}, {street_name}</b>\n"
                    f"🔢 Ваша черга ГПВ: <b>{q_code}</b>\n\n"
                    "🏷 Оберіть назву для цієї локації або напишіть свою:"
                )
                if edit:
                    await msg_or_call_msg.edit_text(txt, parse_mode="HTML", reply_markup=label_presets_kb())
                else:
                    await msg_or_call_msg.answer(txt, parse_mode="HTML", reply_markup=label_presets_kb())
                return

        await state.set_state(AddLocationStates.entering_house)
        txt = (
            f"🏙 {city_name}, <b>{street_name}</b>\n\n"
            "🏠 <b>Крок 3 із 3: Номер будинку</b>\n"
            "Введіть номер будинку (наприклад: <code>10</code>, <code>25А</code>):"
        )
        if edit:
            await msg_or_call_msg.edit_text(txt, parse_mode="HTML")
        else:
            await msg_or_call_msg.answer(txt, parse_mode="HTML")

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
        await call.answer()
        await _proceed_with_chosen_street(call.message, state, chosen, edit=True)  # type: ignore

    async def _resolve_or_pick_house(
        message: Message,
        state: FSMContext,
        street_id: str,
        street_name: str,
        house_query: str,
    ) -> None:
        data = await state.get_data()
        city_name = data.get("city_name", "")

        # Спочатку перевіряємо кеш бази даних
        cached_q = await db.lookup_cached_address(
            city_name, street_name, house_query, region=REGION_VOE
        )
        houses = await voe_client.search_houses(street_id, house_query)

        if len(houses) == 1 and houses[0].queue:
            q_code = houses[0].queue
            await state.update_data(
                house_id=houses[0].id,
                house_num=house_query,
                queue=q_code,
            )
            await state.set_state(AddLocationStates.entering_label)
            await message.answer(
                f"✅ Адресу знайдено: <b>{city_name}, {street_name}, {house_query}</b>\n"
                f"🔢 Ваша черга ГПВ: <b>{q_code}</b>\n\n"
                "🏷 Оберіть назву для цієї локації або напишіть свою:",
                parse_mode="HTML",
                reply_markup=label_presets_kb(),
            )
            return

        if cached_q and not houses:
            _, cached_code = split_queue_key(cached_q)
            await state.update_data(house_num=house_query, queue=cached_q)
            await state.set_state(AddLocationStates.entering_label)
            await message.answer(
                f"✅ Адресу знайдено: <b>{city_name}, {street_name}, {house_query}</b>\n"
                f"🔢 Ваша черга ГПВ: <b>{cached_code}</b>\n\n"
                "🏷 Оберіть назву для цієї локації або напишіть свою:",
                parse_mode="HTML",
                reply_markup=label_presets_kb(),
            )
            return

        if not houses:
            _, st_obj = voe_client.address_index.get_street(street_id)
            allowed_q = (
                sorted({r["queue"] for r in st_obj.get("records", []) if r.get("queue")})
                if st_obj
                else None
            )
            await state.update_data(house_num=house_query)
            await state.set_state(AddLocationStates.choosing_queue)
            await message.answer(
                f"📍 Адреса: <b>{city_name}, {street_name}, {house_query}</b>\n"
                "Оберіть вашу чергу ГПВ зі списку:",
                parse_mode="HTML",
                reply_markup=queue_picker_kb(allowed_queues=allowed_q),
            )
            return

        await state.update_data(
            house_candidates=[
                {
                    "id": h.id,
                    "label": h.label,
                    "house_num": house_query,
                    "queue": h.queue,
                }
                for h in houses[:10]
            ]
        )
        await state.set_state(AddLocationStates.selecting_house)
        await message.answer(
            f"🏠 Для адреси <b>{city_name}, {street_name}, {house_query}</b> знайдено кілька приєднань.\n"
            "Оберіть ваш варіант зі списку:",
            parse_mode="HTML",
            reply_markup=autocomplete_items_kb(houses, "addloc:house"),
        )

    @router.message(AddLocationStates.entering_house, F.text)
    async def msg_entering_house(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        data = await state.get_data()
        region = data.get("region") or REGION_VOE
        city_name = data.get("city_name", "")
        street_id = data.get("street_id", "")
        street_name = data.get("street_name", "")

        if region == REGION_IF:
            await _resolve_if_address_and_proceed(
                message, state, city_name, street_name, query
            )
            return

        await _resolve_or_pick_house(message, state, street_id, street_name, query)

    @router.callback_query(
        AddLocationStates.selecting_house, F.data.startswith("addloc:free:")
    )
    async def cb_select_freeform(call: CallbackQuery, state: FSMContext) -> None:
        if not call.data or not call.message:
            return
        idx = int(call.data.split(":")[2])
        data = await state.get_data()
        candidates = data.get("freeform_candidates", [])
        if idx >= len(candidates):
            await call.answer("Помилка вибору", show_alert=True)
            return

        chosen = candidates[idx]
        await state.update_data(
            city_id=chosen.get("city_id"),
            city_name=chosen.get("city_name"),
            street_id=chosen.get("street_id"),
            street_name=chosen.get("street_name"),
            house_num=chosen.get("house"),
            queue=chosen["queue"],
        )
        await state.set_state(AddLocationStates.entering_label)
        await call.answer()
        await call.message.edit_text(
            f"✅ Адресу знайдено: <b>{chosen['display']}</b>\n"
            f"🔢 Ваша черга ГПВ: <b>{chosen['queue']}</b>\n\n"
            "🏷 Оберіть назву для цієї локації або напишіть свою:",
            parse_mode="HTML",
            reply_markup=label_presets_kb(),
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
        house_num = chosen.get("house_num") or chosen["label"]
        preset_queue = chosen.get("queue")

        if preset_queue:
            await state.update_data(
                house_id=house_id,
                house_num=house_num,
                queue=preset_queue,
            )
            await state.set_state(AddLocationStates.entering_label)
            await call.answer()
            await call.message.edit_text(
                f"✅ Адресу знайдено: <b>{city_name}, {street_name}, {house_num}</b>\n"
                f"🔢 Ваша черга ГПВ: <b>{preset_queue}</b>\n\n"
                "🏷 Оберіть назву для цієї локації або напишіть свою:",
                parse_mode="HTML",
                reply_markup=label_presets_kb(),
            )
            return

        await call.answer("Визначаю чергу ГПВ...")
        await call.message.edit_text("⏳ Визначаю чергу та графік для вашої адреси...")

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
                await _save_schedule(s)
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
                "Вкажіть вашу чергу ГПВ:",
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
        region = data.get("region") or REGION_VOE
        raw_queue = data.get("queue", "1.1")
        queue = raw_queue if ":" in raw_queue else make_queue_key(region, raw_queue)

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
        all_user_locs = await db.get_user_locations(telegram_id)
        other_active = [
            l for l in all_user_locs if l.id != loc.id and l.is_active
        ]

        await message.answer(
            f"🎉 Локацію <b>{loc.label}</b> ({loc.region_short_label} · Черга <b>{loc.queue_code}</b>) успішно збережено!\n"
            "Тепер ви отримуватимете всі сповіщення про графіки та відключення за 15 хвилин.",
            parse_mode="HTML",
            reply_markup=main_reply_keyboard(),
        )
        if other_active:
            other_names = ", ".join(f"«{l.label}»" for l in other_active[:3])
            await message.answer(
                f"🧳 <b>Переїхали тимчасово?</b>\n"
                f"У вас також активні сповіщення для {other_names}. "
                f"Натисніть кнопку нижче, якщо хочете отримувати сповіщення <b>лише для «{loc.label}»</b>, зберігши інші локації на паузі:",
                parse_mode="HTML",
                reply_markup=make_only_active_kb(loc.id, loc.label),
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
    # Адмін-розсилка (/broadcast)
    # -------------------------------------------------------------------------
    @router.message(Command("broadcast"))
    async def cmd_broadcast(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ заборонено. Ця команда доступна лише адміністратору.")
            return

        await state.clear()
        user_ids = await db.get_all_user_ids()
        if not user_ids:
            await message.answer("ℹ️ У базі даних ще немає жодного користувача для розсилки.")
            return

        await state.set_state(BroadcastStates.entering_message)
        await message.answer(
            f"📢 <b>Нова розсилка</b>\n\n"
            f"Знайдено користувачів: <b>{len(user_ids)}</b>.\n"
            "Надішліть повідомлення, яке ви хочете розіслати (текст, фото з підписом тощо):",
            parse_mode="HTML",
            reply_markup=broadcast_cancel_kb(),
        )

    @router.message(BroadcastStates.entering_message)
    async def handle_broadcast_message(
        message: Message, state: FSMContext, bot: Bot
    ) -> None:
        if not message.from_user or not is_admin(message.from_user.id):
            await state.clear()
            return

        await state.update_data(
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
        )
        await state.set_state(BroadcastStates.confirming)

        user_ids = await db.get_all_user_ids()
        await message.answer("👀 <b>Попередній перегляд повідомлення:</b>", parse_mode="HTML")
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await message.answer(
            f"Підтвердіть відправку для <b>{len(user_ids)}</b> користувачів:",
            parse_mode="HTML",
            reply_markup=broadcast_confirm_kb(len(user_ids)),
        )

    @router.callback_query(F.data == "broadcast:edit")
    async def cb_broadcast_edit(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not is_admin(callback.from_user.id):
            await callback.answer("⛔ Доступ заборонено.", show_alert=True)
            return

        await state.set_state(BroadcastStates.entering_message)
        if callback.message:
            await callback.message.edit_text(
                "✏️ Надішліть нове повідомлення для розсилки:",
                reply_markup=broadcast_cancel_kb(),
            )
        await callback.answer()

    @router.callback_query(F.data == "broadcast:cancel")
    async def cb_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not is_admin(callback.from_user.id):
            await callback.answer("⛔ Доступ заборонено.", show_alert=True)
            return

        await state.clear()
        if callback.message:
            await callback.message.edit_text("❌ Розсилку скасовано.")
        await callback.answer()

    @router.callback_query(F.data == "broadcast:send")
    async def cb_broadcast_send(
        callback: CallbackQuery, state: FSMContext, bot: Bot
    ) -> None:
        if not callback.from_user or not is_admin(callback.from_user.id):
            await callback.answer("⛔ Доступ заборонено.", show_alert=True)
            return

        data = await state.get_data()
        source_chat_id = data.get("source_chat_id")
        source_message_id = data.get("source_message_id")
        await state.clear()

        if source_chat_id is None or source_message_id is None:
            if callback.message:
                await callback.message.edit_text(
                    "⚠️ Дані повідомлення втрачено. Запустіть /broadcast знову."
                )
            await callback.answer()
            return

        user_ids = await db.get_all_user_ids()
        total = len(user_ids)
        status_msg = callback.message
        if status_msg:
            await status_msg.edit_text(
                f"⏳ <b>Розсилка розпочата...</b>\nНадіслано: 0 / {total}",
                parse_mode="HTML",
            )
        await callback.answer("Розсилка запущена!")

        async def _on_progress(processed: int, total_count: int) -> None:
            if status_msg and processed % 25 == 0 and processed < total_count:
                try:
                    await status_msg.edit_text(
                        f"⏳ <b>Розсилка триває...</b>\nОброблено: {processed} / {total_count}",
                        parse_mode="HTML",
                    )
                except TelegramBadRequest:
                    pass

        stats = await execute_broadcast(
            bot=bot,
            user_ids=user_ids,
            from_chat_id=int(source_chat_id),
            message_id=int(source_message_id),
            progress_cb=_on_progress,
        )

        stats_text = (
            "📊 <b>Статистика розсилки</b>\n\n"
            f"• Всього користувачів: <b>{stats['total']}</b>\n"
            f"• ✅ Успішно доставлено: <b>{stats['sent']}</b>\n"
            f"• 🚫 Заблокували бота: <b>{stats['blocked']}</b>\n"
            f"• ⚠️ Інші помилки: <b>{stats['failed']}</b>\n"
            f"• ⏱ Час виконання: <b>{stats['duration_sec']} сек</b>"
        )
        if status_msg:
            await status_msg.edit_text(stats_text, parse_mode="HTML")

    # -------------------------------------------------------------------------
    # Розпізнавання надісланого користувачем зображення графіка
    # -------------------------------------------------------------------------
    @router.message(F.photo)
    async def handle_uploaded_schedule_photo(message: Message, bot: Bot) -> None:
        if not message.from_user or not message.photo:
            return

        # Визначаємо чергу та дату з підпису або з першої локації користувача
        caption = message.caption or ""
        locations = await db.get_user_locations(message.from_user.id)
        voe_locs = [l for l in locations if l.region == REGION_VOE]
        default_queue = voe_locs[0].queue_code if voe_locs else "3.2"

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

            hdr_q, hdr_d = extract_header_queue_and_date(img_bytes)
            if not q_match and hdr_q in ALL_QUEUES:
                queue = hdr_q
            if not d_match and "завтра" not in caption.lower() and hdr_d is not None:
                target_date = hdr_d

            parsed_sched = parse_voe_schedule_image(
                img_bytes, queue=queue, schedule_date=target_date
            )

            # Якщо в підписі та шапці не було знайдено чергу — визначаємо її за 48-слотовим відбитком!
            if not q_match and not hdr_q:
                all_github = await voe_client.fetch_all_queues_from_github()
                matched_q = voe_client.match_schedule_to_queue(parsed_sched, all_github)
                if matched_q in ALL_QUEUES:
                    queue = matched_q
                    parsed_sched.queue = matched_q

            await _save_schedule(parsed_sched)

            # Якщо у користувача ще немає локації з цією чергою, пропонуємо або створюємо
            suggest_switch_loc: Optional[UserLocation] = None
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
                exact_loc = next((l for l in locations if l.queue == queue), None)
                if exact_loc:
                    loc = exact_loc
                elif voe_locs:
                    loc = voe_locs[0]
                    suggest_switch_loc = loc
                else:
                    loc = locations[0]

            header_prefix = "📸✅ <b>Зображення графіка успішно розпізнано та збережено!</b>"
            if suggest_switch_loc is not None:
                header_prefix += (
                    f"\n💡 <i>Розпізнаний графік відповідає черзі <b>{queue}</b> "
                    f"(у локації «{suggest_switch_loc.label}» зараз встановлено <b>{suggest_switch_loc.queue_code}</b>).</i>"
                )

            text = format_schedule_message(
                schedule=parsed_sched,
                location_label=loc.label,
                address_str=loc.display_address,
                now=now,
                header_prefix=header_prefix,
            )
            reply_kb = (
                switch_queue_suggestion_kb(suggest_switch_loc, queue)
                if suggest_switch_loc is not None
                else main_reply_keyboard()
            )
            await message.answer(text, parse_mode="HTML", reply_markup=reply_kb)
        except Exception as exc:
            logger.exception("Failed to parse user photo: %s", exc)
            await message.answer(
                "⚠️ Не вдалося розпізнати таблицю на зображенні. Переконайтеся, що на картинці чітко видно 24-годинну сітку графіка."
            )

    return router


def _format_settings_text(user) -> str:
    return (
        "⚙️ <b>Налаштування сповіщень</b>\n\n"
        "Тут ви можете гнучко налаштувати, які саме сповіщення отримувати:\n"
        "• <b>Графік на завтра</b> — сповіщення, коли ввечері публікується розклад на наступний день.\n"
        "• <b>Зміни в графіку</b> — сповіщення, якщо оперативно змінилися години відключень.\n"
        "• <b>За 15 хв до вимкнення / увімкнення</b> — завчасне попередження перед кожним слотом.\n"
        "• <b>Можливі (жовті 🟡) слоти</b> — враховувати заплановані (жовті) слоти нарівні з підтвердженими (синіми/червоними 🔴).\n"
        "• <b>Тихі години</b> — не турбувати 15-хвилинними сповіщеннями вночі."
    )


def is_admin(user_id: int) -> bool:
    """Перевіряє, чи є користувач адміністратором бота."""
    return user_id in settings.admin_ids


async def execute_broadcast(
    bot: Bot,
    user_ids: list[int],
    from_chat_id: int,
    message_id: int,
    progress_cb=None,
    delay_seconds: float = 0.05,
) -> dict[str, int | float]:
    """Розсилає скопійоване повідомлення всім користувачам та повертає статистику."""
    started = time.monotonic()
    total = len(user_ids)
    sent = 0
    blocked = 0
    failed = 0

    for idx, uid in enumerate(user_ids, start=1):
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=from_chat_id,
                message_id=message_id,
            )
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.1)
            try:
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                )
                sent += 1
            except TelegramForbiddenError:
                blocked += 1
            except (TelegramAPIError, Exception) as retry_exc:
                logger.warning("Broadcast retry failed for %s: %s", uid, retry_exc)
                failed += 1
        except TelegramForbiddenError:
            blocked += 1
        except (TelegramAPIError, Exception) as exc:
            logger.warning("Broadcast failed for %s: %s", uid, exc)
            failed += 1

        if progress_cb is not None:
            await progress_cb(idx, total)

        if delay_seconds > 0 and idx < total:
            await asyncio.sleep(delay_seconds)

    return {
        "total": total,
        "sent": sent,
        "blocked": blocked,
        "failed": failed,
        "duration_sec": round(time.monotonic() - started, 2),
    }
