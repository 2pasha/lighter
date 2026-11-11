from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Optional

try:
    from aiogram import Bot
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.types import BufferedInputFile
except ImportError:
    Bot = Any  # type: ignore
    TelegramRetryAfter = Exception  # type: ignore
    BufferedInputFile = Any  # type: ignore

from app.config import settings
from app.database import Database, UserLocation, UserProfile
from app.models import DaySchedule, OutageInterval, SlotState
from app.services.formatter import (
    format_date_ua,
    format_schedule_diff,
    format_schedule_message,
    has_meaningful_outage_changes,
)
from app.services.image_parser import render_notification_image, render_schedule_image
from app.services.voe_client import VOEDataClient

logger = logging.getLogger(__name__)

# Кеш Telegram file_id за SHA-256 хешем PNG-байтів (дозволяє не завантажувати те саме фото повторно)
PHOTO_FILE_ID_CACHE: dict[str, str] = {}
_PHOTO_CACHE_MAX_SIZE = 256


async def send_photo_cached(
    bot: Bot,
    chat_id: int,
    img_bytes: bytes,
    filename: str,
    caption: Optional[str] = None,
    parse_mode: Optional[str] = None,
    reply_markup: Optional[Any] = None,
) -> Any:
    """
    Відправляє фото в Telegram. Якщо ці байти вже відправлялися раніше,
    використовує збережений Telegram `file_id` (миттєва відправка без повторного аплоаду PNG).
    Також автоматично обробляє TelegramRetryAfter (429 Too Many Requests).
    """
    digest = hashlib.sha256(img_bytes).hexdigest()[:24]
    cached_file_id = PHOTO_FILE_ID_CACHE.get(digest)

    for attempt in range(2):
        try:
            if cached_file_id:
                try:
                    return await bot.send_photo(
                        chat_id=chat_id,
                        photo=cached_file_id,
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
                except TelegramRetryAfter:
                    raise
                except Exception:
                    # Якщо file_id застарів або недійсний — видаляємо з кешу і вантажимо байти
                    PHOTO_FILE_ID_CACHE.pop(digest, None)
                    cached_file_id = None

            photo_file = BufferedInputFile(img_bytes, filename=filename)
            sent_msg = await bot.send_photo(
                chat_id=chat_id,
                photo=photo_file,
                caption=caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            if sent_msg and getattr(sent_msg, "photo", None):
                file_id = getattr(sent_msg.photo[-1], "file_id", None)
                if file_id:
                    if len(PHOTO_FILE_ID_CACHE) >= _PHOTO_CACHE_MAX_SIZE:
                        oldest = next(iter(PHOTO_FILE_ID_CACHE))
                        PHOTO_FILE_ID_CACHE.pop(oldest, None)
                    PHOTO_FILE_ID_CACHE[digest] = file_id
            return sent_msg
        except TelegramRetryAfter as exc:
            if attempt == 0:
                retry_sec = min(float(getattr(exc, "retry_after", 2.0)), 15.0)
                await asyncio.sleep(retry_sec)
            else:
                raise
    return None


def is_in_quiet_hours(now_time_str: str, start_str: str, end_str: str) -> bool:
    """
    Перевіряє, чи потрапляє поточний час (HH:MM) у тихі години (наприклад 00:00 .. 07:00 або 23:00 .. 07:00).
    """
    try:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        nh, nm = map(int, now_time_str.split(":"))
    except Exception:
        return False

    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    now_m = nh * 60 + nm

    if start_m <= end_m:
        return start_m <= now_m < end_m
    else:
        # Перехід через північ (наприклад 23:00 .. 07:00)
        return now_m >= start_m or now_m < end_m


class NotificationEngine:
    """
    Головний рушій моніторингу графіків та розсилки 4 типів сповіщень:
    1. Коли з'являється графік на завтра.
    2. Коли змінюється графік на сьогодні або завтра.
    3. За 15 хвилин до вимкнення світла (підтвердженого 🔴 або запланованого/можливого 🟡).
    4. За 15 хвилин до відновлення світла.
    """

    def __init__(self, bot: Bot, db: Database, voe_client: VOEDataClient) -> None:
        self.bot = bot
        self.db = db
        self.voe_client = voe_client
        # Обмежуємо паралельну розсилку до 20 одночасних запитів (ліміт Telegram ~30 повід./сек)
        self._send_semaphore = asyncio.Semaphore(20)

    # -------------------------------------------------------------------------
    # Періодичне оновлення графіків та сповіщення 1 і 2 (Новий графік / Зміни)
    # -------------------------------------------------------------------------
    async def poll_and_process_schedules(self) -> None:
        now = datetime.now(settings.tz)
        today = now.date()
        tomorrow = today + timedelta(days=1)

        # Прогріваємо in-memory кеш графіків за 1 запит до БД
        await self.db.prefetch_schedules_for_dates([today, tomorrow])

        subscriptions = await self.db.get_all_active_subscriptions()

        # 1. Завантажуємо глобальний JSON для всіх черг з GitHub-дзеркала + bezsvitla
        fetched_schedules = await self.voe_client.fetch_all_queues_from_github()

        # 2. Якщо для якихось активних локацій з адресою немає свіжих даних або треба перевірити сайт напряму
        checked_queues_via_address: set[str] = set()
        for _, loc in subscriptions:
            if (
                loc.city_id
                and loc.street_id
                and loc.house_id
                and loc.queue not in checked_queues_via_address
            ):
                checked_queues_via_address.add(loc.queue)
                addr_res = await self.voe_client.fetch_by_address(
                    city_id=loc.city_id,
                    street_id=loc.street_id,
                    house_id=loc.house_id,
                    city_name=loc.city_name or "",
                    street_name=loc.street_name or "",
                    house_num=loc.house_num or "",
                )
                if addr_res and addr_res.schedules:
                    for d, sched in addr_res.schedules.items():
                        sched.queue = loc.queue
                        fetched_schedules[(loc.queue, d)] = sched

        # 3. Опціональний fallback через офіційний бот @pat_voe_bot (якщо увімкнено Telethon)
        monitored_queues = {loc.queue for _, loc in subscriptions}
        for q in monitored_queues:
            for target_d in (today, tomorrow):
                if (q, target_d) not in fetched_schedules:
                    bot_sched = await self.voe_client.fetch_from_official_bot(q, target_d)
                    if bot_sched:
                        fetched_schedules[(q, target_d)] = bot_sched

        # 4. Зберігаємо графіки в БД та визначаємо події (новий на завтра / зміна графіка)
        for (queue, sched_date), new_sched in fetched_schedules.items():
            await self.save_and_notify_if_changed(
                new_sched, now=now, preloaded_subscriptions=subscriptions
            )

    async def save_and_notify_if_changed(
        self,
        new_sched: DaySchedule,
        now: Optional[datetime] = None,
        preloaded_subscriptions: Optional[list[tuple[UserProfile, UserLocation]]] = None,
    ) -> bool:
        """
        Зберігає новий графік у БД. Якщо графік змінився або з'явився новий на завтра,
        автоматично здійснює розсилку всім активним підписникам відповідної черги.
        Повертає True, якщо графік змінився та був опрацьований.
        """
        if now is None:
            now = datetime.now(settings.tz)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        sched_date = new_sched.schedule_date
        queue = new_sched.queue

        if sched_date < today:
            return False

        # Ігноруємо порожні шаблони на завтра (0 хв відключень), якщо графіка в БД ще не було
        existing_sched = await self.db.get_schedule(queue, sched_date)
        if (
            sched_date > today
            and existing_sched is None
            and new_sched.total_outage_minutes == 0
        ):
            return False

        changed, old_sched = await self.db.save_schedule(new_sched)
        if not changed:
            return False

        # Розсилаємо сповіщення всім підписникам цієї черги
        subscriptions = (
            preloaded_subscriptions
            if preloaded_subscriptions is not None
            else await self.db.get_all_active_subscriptions()
        )
        matching_subs = [
            (user, loc) for user, loc in subscriptions if loc.queue == queue
        ]
        if not matching_subs:
            return True

        if old_sched is None and sched_date == tomorrow:
            # Подія №1: З'явився графік на завтра!
            await self._broadcast_tomorrow_appeared(matching_subs, new_sched, now)
        elif old_sched is not None:
            # Подія №2: Графік змінився (на сьогодні або на завтра)!
            await self._broadcast_schedule_changed(
                matching_subs, old_sched, new_sched, now
            )
        return True

    async def _broadcast_tomorrow_appeared(
        self,
        subs: list[tuple[UserProfile, UserLocation]],
        schedule: DaySchedule,
        now: datetime,
    ) -> None:
        if schedule.total_outage_minutes == 0:
            return

        img_bytes = render_schedule_image(schedule, now=now)
        alert_key = f"tomorrow_new:{schedule.schedule_date.isoformat()}:{schedule.content_hash}"
        now_time_str = now.strftime("%H:%M")

        already_sent = await self.db.get_sent_alerts_for_keys([alert_key])
        eligible: list[tuple[UserProfile, UserLocation, str]] = []

        for user, loc in subs:
            if not user.notify_tomorrow:
                continue
            if user.quiet_hours_enabled and is_in_quiet_hours(
                now_time_str, user.quiet_start, user.quiet_end
            ):
                continue
            if (user.telegram_id, loc.id, alert_key) in already_sent:
                continue

            text = format_schedule_message(
                schedule=schedule,
                location_label=loc.label,
                address_str=loc.display_address,
                now=now,
                header_prefix="🆕📅 <b>З'явився графік відключень на завтра!</b>",
            )
            eligible.append((user, loc, text))

        if not eligible:
            return

        sent_entries: list[tuple[int, int, str]] = []

        # Відправляємо першому отримувачу, щоб закешувати Telegram file_id перед паралельною розсилкою
        first_user, first_loc, first_text = eligible[0]
        if await self._send_schedule_notification(
            first_user.telegram_id, first_text, img_bytes, schedule
        ):
            sent_entries.append((first_user.telegram_id, first_loc.id, alert_key))

        async def _worker(u: UserProfile, l: UserLocation, txt: str) -> None:
            async with self._send_semaphore:
                if await self._send_schedule_notification(
                    u.telegram_id, txt, img_bytes, schedule
                ):
                    sent_entries.append((u.telegram_id, l.id, alert_key))

        if len(eligible) > 1:
            await asyncio.gather(*(_worker(u, l, t) for u, l, t in eligible[1:]))

        await self.db.mark_alerts_sent_batch(sent_entries)

    async def _broadcast_schedule_changed(
        self,
        subs: list[tuple[UserProfile, UserLocation]],
        old_sched: DaySchedule,
        new_sched: DaySchedule,
        now: datetime,
    ) -> None:
        # Ігноруємо зміни лише кольору (🟡 ↔ 🔴) без зміни наявності світла
        if not has_meaningful_outage_changes(old_sched, new_sched, now=now):
            return

        # Якщо в БД помилково був збережений порожній шаблон на завтра (0 хв), а тепер з'явився реальний графік —
        # відправляємо це як "З'явився графік на завтра!", а не як "Змінився графік"
        tomorrow = now.date() + timedelta(days=1)
        if (
            new_sched.schedule_date == tomorrow
            and old_sched.total_outage_minutes == 0
            and new_sched.total_outage_minutes > 0
        ):
            await self._broadcast_tomorrow_appeared(subs, new_sched, now)
            return

        img_bytes = render_schedule_image(new_sched, now=now)
        alert_key = (
            f"changed:{new_sched.schedule_date.isoformat()}:"
            f"{old_sched.content_hash}->{new_sched.content_hash}"
        )
        diff_block = format_schedule_diff(old_sched, new_sched, now=now)
        day_word = "сьогодні" if new_sched.schedule_date == now.date() else "завтра"
        now_time_str = now.strftime("%H:%M")

        already_sent = await self.db.get_sent_alerts_for_keys([alert_key])
        eligible: list[tuple[UserProfile, UserLocation, str]] = []

        header = f"🔄⚠️ <b>Оновлено графік відключень на {day_word}!</b>"
        if diff_block:
            header += f"\n{diff_block}"

        for user, loc in subs:
            if not user.notify_changes:
                continue
            if user.quiet_hours_enabled and is_in_quiet_hours(
                now_time_str, user.quiet_start, user.quiet_end
            ):
                continue
            if (user.telegram_id, loc.id, alert_key) in already_sent:
                continue

            text = format_schedule_message(
                schedule=new_sched,
                location_label=loc.label,
                address_str=loc.display_address,
                now=now,
                header_prefix=header,
            )
            eligible.append((user, loc, text))

        if not eligible:
            return

        sent_entries: list[tuple[int, int, str]] = []

        first_user, first_loc, first_text = eligible[0]
        if await self._send_schedule_notification(
            first_user.telegram_id, first_text, img_bytes, new_sched
        ):
            sent_entries.append((first_user.telegram_id, first_loc.id, alert_key))

        async def _worker(u: UserProfile, l: UserLocation, txt: str) -> None:
            async with self._send_semaphore:
                if await self._send_schedule_notification(
                    u.telegram_id, txt, img_bytes, new_sched
                ):
                    sent_entries.append((u.telegram_id, l.id, alert_key))

        if len(eligible) > 1:
            await asyncio.gather(*(_worker(u, l, t) for u, l, t in eligible[1:]))

        await self.db.mark_alerts_sent_batch(sent_entries)

    # -------------------------------------------------------------------------
    # Щохвилинна перевірка 15-хвилинних попереджень (Події №3 та №4)
    # -------------------------------------------------------------------------
    async def check_15min_warnings(self, now: Optional[datetime] = None) -> None:
        """
        Викликається кожну хвилину.
        Перевіряє, чи через 14..16 хвилин (цільове вікно 15 хвилин) відбудеться:
        - Початок відключення (перехід зі стану ON -> OFF або POSSIBLE_OFF)
        - Відновлення світла (перехід зі стану OFF / POSSIBLE_OFF -> ON)
        Враховує навіть переходи через північ (наприклад, о 23:45 перевіряє слот 00:00 завтрашнього дня).
        """
        if now is None:
            now = datetime.now(settings.tz)

        # Шукаємо точний час через 15 хвилин ПЕРЕД запитами до БД
        target_dt = now + timedelta(minutes=15)
        # Округлюємо до найближчої межі 30 хвилин, якщо ми у вікні [14..16] хвилин до переходу
        for lead_min in (15, 14, 16):
            cand_dt = now + timedelta(minutes=lead_min)
            if cand_dt.minute in (0, 30):
                target_dt = cand_dt
                break
        else:
            # Зараз не ~15 хвилин до межі 00 або 30 хвилин — виходимо за 0 мс без звернень до БД
            return

        subscriptions = await self.db.get_all_active_subscriptions()
        if not subscriptions:
            return

        today = now.date()
        tomorrow = today + timedelta(days=1)
        await self.db.prefetch_schedules_for_dates([today, tomorrow])

        # Кешуємо графіки з БД для всіх потрібних черг
        queues = {loc.queue for _, loc in subscriptions}
        schedules_today: dict[str, DaySchedule] = {}
        schedules_tomorrow: dict[str, DaySchedule] = {}

        for q in queues:
            st = await self.db.get_schedule(q, today)
            if st:
                schedules_today[q] = st
            stm = await self.db.get_schedule(q, tomorrow)
            if stm:
                schedules_tomorrow[q] = stm

        target_date = target_dt.date()
        target_slot_idx = (target_dt.hour * 60 + target_dt.minute) // 30
        prev_dt = target_dt - timedelta(minutes=30)
        prev_date = prev_dt.date()
        prev_slot_idx = (prev_dt.hour * 60 + prev_dt.minute) // 30

        now_time_str = now.strftime("%H:%M")
        target_time_str = target_dt.strftime("%H:%M")
        warn_off_key = f"warn_off:{target_date.isoformat()}:{target_time_str}"
        warn_on_key = f"warn_on:{target_date.isoformat()}:{target_time_str}"

        already_sent = await self.db.get_sent_alerts_for_keys([warn_off_key, warn_on_key])
        img_off_confirmed = render_notification_image("outage_soon", slot_state=SlotState.OFF)
        img_off_possible = render_notification_image("outage_soon", slot_state=SlotState.POSSIBLE_OFF)
        img_light_on = render_notification_image("light_on")

        pending_alerts: list[tuple[int, int, str, str, Optional[bytes], str]] = []

        for user, loc in subscriptions:
            # Перевірка тихих годин (Do Not Disturb)
            if user.quiet_hours_enabled and is_in_quiet_hours(
                now_time_str, user.quiet_start, user.quiet_end
            ):
                continue

            sched_prev = (
                schedules_today.get(loc.queue)
                if prev_date == today
                else schedules_tomorrow.get(loc.queue)
            )
            sched_target = (
                schedules_today.get(loc.queue)
                if target_date == today
                else schedules_tomorrow.get(loc.queue)
            )

            if not sched_prev or not sched_target:
                continue

            prev_state = sched_prev.slots[prev_slot_idx]
            next_state = sched_target.slots[target_slot_idx]

            def is_effective_outage(st: SlotState) -> bool:
                if st == SlotState.OFF:
                    return True
                if st == SlotState.POSSIBLE_OFF and user.notify_possible:
                    return True
                return False

            was_off = is_effective_outage(prev_state)
            will_be_off = is_effective_outage(next_state)

            addr_suffix = (
                f" ({loc.display_address})"
                if loc.display_address != f"Черга {loc.queue}"
                else ""
            )

            if not was_off and will_be_off and user.notify_15m_off:
                # Подія №3: За 15 хвилин вимкнуть світло!
                if (user.telegram_id, loc.id, warn_off_key) not in already_sent:
                    end_str, duration_str = self._calc_outage_end(
                        sched_target,
                        schedules_tomorrow.get(loc.queue) if target_date == today else None,
                        target_slot_idx,
                        user.notify_possible,
                    )
                    outage_type_str = (
                        "🔴 <b>Підтверджене відключення</b>"
                        if next_state == SlotState.OFF
                        else "🟡 <b>Заплановане (можливе) відключення</b>"
                    )
                    msg = (
                        f"⚠️🔌 <b>Увага! Відключення світла через 15 хвилин (о {target_time_str})</b>\n\n"
                        f"📍 Локація: <b>{loc.label}</b>{addr_suffix}\n"
                        f"🔢 Черга: <b>{loc.queue}</b>\n"
                        f"📌 Статус слоту: {outage_type_str}\n"
                        f"🕒 Орієнтовно до: <b>{end_str}</b> (тривалість: {duration_str})\n\n"
                        f"<i>Рекомендуємо зарядити пристрої та завершити важливі справи за комп'ютером.</i>"
                    )
                    img_bytes = (
                        img_off_confirmed
                        if next_state == SlotState.OFF
                        else img_off_possible
                    )
                    pending_alerts.append(
                        (
                            user.telegram_id,
                            loc.id,
                            warn_off_key,
                            msg,
                            img_bytes,
                            f"warn_off_{loc.queue}_{target_time_str.replace(':', '')}.png",
                        )
                    )

            elif was_off and not will_be_off and user.notify_15m_on:
                # Подія №4: За 15 хвилин увімкнуть світло!
                if (user.telegram_id, loc.id, warn_on_key) not in already_sent:
                    next_off_str = self._calc_light_until(
                        sched_target, target_slot_idx, user.notify_possible
                    )
                    msg = (
                        f"💡🎉 <b>Світло має з'явитися через 15 хвилин (о {target_time_str})!</b>\n\n"
                        f"📍 Локація: <b>{loc.label}</b>{addr_suffix}\n"
                        f"🔢 Черга: <b>{loc.queue}</b>\n"
                        f"🟢 Наступне відключення: <b>{next_off_str}</b>"
                    )
                    pending_alerts.append(
                        (
                            user.telegram_id,
                            loc.id,
                            warn_on_key,
                            msg,
                            img_light_on,
                            f"warn_on_{loc.queue}_{target_time_str.replace(':', '')}.png",
                        )
                    )

        if not pending_alerts:
            return

        sent_entries: list[tuple[int, int, str]] = []

        # Прогріваємо file_id для унікальних картинок перед паралельною розсилкою
        warmed_digests: set[str] = set()
        remaining_alerts: list[tuple[int, int, str, str, Optional[bytes], str]] = []
        for item in pending_alerts:
            tg_id, loc_id, a_key, msg_txt, img_b, fname = item
            digest = hashlib.sha256(img_b).hexdigest()[:24] if img_b else ""
            if digest and digest not in PHOTO_FILE_ID_CACHE and digest not in warmed_digests:
                warmed_digests.add(digest)
                if await self._safe_send_alert(
                    tg_id, msg_txt, img_bytes=img_b, filename=fname
                ):
                    sent_entries.append((tg_id, loc_id, a_key))
            else:
                remaining_alerts.append(item)

        async def _alert_worker(
            item: tuple[int, int, str, str, Optional[bytes], str]
        ) -> None:
            tg_id, loc_id, a_key, msg_txt, img_b, fname = item
            async with self._send_semaphore:
                if await self._safe_send_alert(
                    tg_id, msg_txt, img_bytes=img_b, filename=fname
                ):
                    sent_entries.append((tg_id, loc_id, a_key))

        if remaining_alerts:
            await asyncio.gather(*(_alert_worker(it) for it in remaining_alerts))

        await self.db.mark_alerts_sent_batch(sent_entries)

    @staticmethod
    def _calc_outage_end(
        sched: DaySchedule,
        next_day_sched: Optional[DaySchedule],
        start_slot_idx: int,
        include_possible: bool,
    ) -> tuple[str, str]:
        slots_count = 0
        for idx in range(start_slot_idx, 48):
            st = sched.slots[idx]
            is_off = st == SlotState.OFF or (
                st == SlotState.POSSIBLE_OFF and include_possible
            )
            if is_off:
                slots_count += 1
            else:
                break

        end_min = (start_slot_idx + slots_count) * 30
        if end_min == 1440 and next_day_sched:
            for idx in range(0, 48):
                st = next_day_sched.slots[idx]
                is_off = st == SlotState.OFF or (
                    st == SlotState.POSSIBLE_OFF and include_possible
                )
                if is_off:
                    slots_count += 1
                else:
                    break
            end_min_mod = ((start_slot_idx + slots_count) * 30) % 1440
            h, m = divmod(end_min_mod, 60)
            end_str = f"{h:02d}:{m:02d} (наступної доби)"
        elif end_min >= 1440:
            end_str = "24:00"
        else:
            h, m = divmod(end_min, 60)
            end_str = f"{h:02d}:{m:02d}"

        total_mins = slots_count * 30
        dh, dm = divmod(total_mins, 60)
        dur_parts = []
        if dh:
            dur_parts.append(f"{dh} год")
        if dm:
            dur_parts.append(f"{dm} хв")
        return end_str, (" ".join(dur_parts) if dur_parts else "30 хв")

    @staticmethod
    def _calc_light_until(
        sched: DaySchedule, start_slot_idx: int, include_possible: bool
    ) -> str:
        for idx in range(start_slot_idx, 48):
            st = sched.slots[idx]
            is_off = st == SlotState.OFF or (
                st == SlotState.POSSIBLE_OFF and include_possible
            )
            if is_off:
                mins = idx * 30
                h, m = divmod(mins, 60)
                return f"о {h:02d}:{m:02d} ({st.emoji})"
        return "до кінця доби не заплановано ✅"

    async def _send_schedule_notification(
        self,
        chat_id: int,
        text: str,
        img_bytes: Optional[bytes],
        schedule: DaySchedule,
    ) -> bool:
        fname = f"schedule_{schedule.queue}_{schedule.schedule_date}.png"
        return await self._safe_send_alert(
            chat_id=chat_id,
            text=text,
            img_bytes=img_bytes,
            filename=fname,
        )

    async def _safe_send_alert(
        self,
        chat_id: int,
        text: str,
        img_bytes: Optional[bytes] = None,
        filename: str = "alert.png",
    ) -> bool:
        try:
            if img_bytes and len(text) <= 1000:
                await send_photo_cached(
                    bot=self.bot,
                    chat_id=chat_id,
                    img_bytes=img_bytes,
                    filename=filename,
                    caption=text,
                    parse_mode="HTML",
                )
            else:
                if img_bytes:
                    await send_photo_cached(
                        bot=self.bot,
                        chat_id=chat_id,
                        img_bytes=img_bytes,
                        filename=filename,
                    )
                await self.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="HTML"
                )
            return True
        except Exception as exc:
            logger.warning("Failed to send alert message to %s: %s", chat_id, exc)
            return False

    async def _safe_send_message(self, chat_id: int, text: str) -> bool:
        return await self._safe_send_alert(chat_id, text)
