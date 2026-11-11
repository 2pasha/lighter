from __future__ import annotations

import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.models import DaySchedule, SlotState
from app.services.formatter import format_schedule_diff, format_schedule_message
from app.services.image_parser import (
    classify_rgb,
    parse_voe_schedule_image,
    render_notification_image,
    render_schedule_image,
)
from app.services.notifier import NotificationEngine, is_in_quiet_hours

UPLOADED_IMAGE_PATH = Path(
    "/Users/vatra/.gemini/antigravity/brain/d1c4193e-1d16-483a-af0e-8670cd183cba/.user_uploaded/media_1793817223571_852a184e.png"
)


class TestSvitloBotCore(unittest.TestCase):
    def test_classify_rgb_colors(self) -> None:
        # Блакитний з офіційного бота @pat_voe_bot -> підтверджене відключення
        self.assertEqual(classify_rgb(1, 176, 241), SlotState.OFF)
        # Жовтий з офіційного бота @pat_voe_bot -> заплановане (можливе) відключення
        self.assertEqual(classify_rgb(255, 192, 0), SlotState.POSSIBLE_OFF)
        # Білий -> світло є
        self.assertEqual(classify_rgb(255, 255, 255), SlotState.ON)

    def test_parse_uploaded_pat_voe_bot_image(self) -> None:
        try:
            if not UPLOADED_IMAGE_PATH.exists():
                self.skipTest("Sample image not found in environment")
            raw = UPLOADED_IMAGE_PATH.read_bytes()
        except PermissionError:
            self.skipTest("Sample image not accessible in sandbox")
        sched = parse_voe_schedule_image(
            raw, queue="3.2", schedule_date=date(2026, 11, 4)
        )
        self.assertEqual(sched.queue, "3.2")
        self.assertEqual(sched.schedule_date, date(2026, 11, 4))

        # Перевіряємо всі 24 години відповідно до картинки:
        # 00..03 -> ON (білий)
        for h in (0, 1, 2, 3):
            self.assertEqual(sched.hour_state(h), (SlotState.ON, SlotState.ON))
        # 04..05 -> POSSIBLE_OFF (жовтий)
        for h in (4, 5):
            self.assertEqual(
                sched.hour_state(h), (SlotState.POSSIBLE_OFF, SlotState.POSSIBLE_OFF)
            )
        # 06..07 -> ON (білий)
        for h in (6, 7):
            self.assertEqual(sched.hour_state(h), (SlotState.ON, SlotState.ON))
        # 08..11 -> OFF (синій)
        for h in (8, 9, 10, 11):
            self.assertEqual(sched.hour_state(h), (SlotState.OFF, SlotState.OFF))
        # 12..15 -> ON (білий)
        for h in (12, 13, 14, 15):
            self.assertEqual(sched.hour_state(h), (SlotState.ON, SlotState.ON))
        # 16..17 -> OFF (синій)
        for h in (16, 17):
            self.assertEqual(sched.hour_state(h), (SlotState.OFF, SlotState.OFF))
        # 18..19 -> ON (білий)
        for h in (18, 19):
            self.assertEqual(sched.hour_state(h), (SlotState.ON, SlotState.ON))
        # 20..23 -> POSSIBLE_OFF (жовтий)
        for h in (20, 21, 22, 23):
            self.assertEqual(
                sched.hour_state(h), (SlotState.POSSIBLE_OFF, SlotState.POSSIBLE_OFF)
            )

        # Перевіряємо інтервали
        intervals = sched.get_outage_intervals(merge_confirmed_and_possible=False)
        self.assertEqual(len(intervals), 4)
        self.assertEqual((intervals[0].start_str, intervals[0].end_str, intervals[0].state), ("04:00", "06:00", SlotState.POSSIBLE_OFF))
        self.assertEqual((intervals[1].start_str, intervals[1].end_str, intervals[1].state), ("08:00", "12:00", SlotState.OFF))
        self.assertEqual((intervals[2].start_str, intervals[2].end_str, intervals[2].state), ("16:00", "18:00", SlotState.OFF))
        self.assertEqual((intervals[3].start_str, intervals[3].end_str, intervals[3].state), ("20:00", "24:00", SlotState.POSSIBLE_OFF))

        # Загальна статистика
        self.assertEqual(sched.confirmed_outage_minutes, 6 * 60)  # 6 годин синіх
        self.assertEqual(sched.possible_outage_minutes, 6 * 60)   # 6 годин жовтих
        self.assertEqual(sched.total_outage_minutes, 12 * 60)     # 12 годин всього

    def test_quiet_hours_logic(self) -> None:
        # Діапазон 00:00 - 07:00
        self.assertTrue(is_in_quiet_hours("03:45", "00:00", "07:00"))
        self.assertFalse(is_in_quiet_hours("07:45", "00:00", "07:00"))
        self.assertFalse(is_in_quiet_hours("19:45", "00:00", "07:00"))

        # Діапазон через північ 23:00 - 07:00
        self.assertTrue(is_in_quiet_hours("23:30", "23:00", "07:00"))
        self.assertTrue(is_in_quiet_hours("02:15", "23:00", "07:00"))
        self.assertFalse(is_in_quiet_hours("12:00", "23:00", "07:00"))

    def test_formatter_and_diff(self) -> None:
        hourly1 = [SlotState.ON] * 24
        hourly1[8] = SlotState.OFF
        hourly1[9] = SlotState.OFF
        s1 = DaySchedule.from_hourly("3.2", date(2026, 11, 4), hourly1)

        hourly2 = list(hourly1)
        hourly2[10] = SlotState.POSSIBLE_OFF
        s2 = DaySchedule.from_hourly("3.2", date(2026, 11, 4), hourly2)

        self.assertNotEqual(s1.content_hash, s2.content_hash)
        diff_txt = format_schedule_diff(
            s1, s2, now=datetime(2026, 11, 4, 7, 45, tzinfo=ZoneInfo("Europe/Kyiv"))
        )
        self.assertIn("10:00–11:00", diff_txt)
        self.assertIn("1 год", diff_txt)

        # Перевіряємо, що переходи лише між 🟡 і 🔴 без зміни наявності світла ігноруються
        from app.services.formatter import has_meaningful_outage_changes

        s_color_only = DaySchedule.from_hourly(
            "3.2",
            date(2026, 11, 4),
            [SlotState.OFF if x != SlotState.ON else SlotState.ON for x in hourly2],
        )
        self.assertFalse(
            has_meaningful_outage_changes(
                s2,
                s_color_only,
                now=datetime(2026, 11, 4, 7, 45, tzinfo=ZoneInfo("Europe/Kyiv")),
            )
        )
        # Зміна розкладу на сьогодні фіксується як значуща, навіть якщо змінений інтервал уже розпочався або минув
        self.assertTrue(
            has_meaningful_outage_changes(
                s1,
                s2,
                now=datetime(2026, 11, 4, 21, 30, tzinfo=ZoneInfo("Europe/Kyiv")),
            )
        )

        msg = format_schedule_message(
            s2,
            location_label="🏠 Дім",
            address_str="м. Вінниця, вул. Соборна, 10",
            now=datetime(2026, 11, 4, 7, 45, tzinfo=ZoneInfo("Europe/Kyiv")),
        )
        self.assertIn("Черга: <b>3.2</b>", msg)
        self.assertIn("📊 <b>Всього без світла:</b> 3 год", msg)
        self.assertIn("🔴 підтверджених: 2 год", msg)
        self.assertIn("🟡 можливих: 1 год", msg)

    def test_schedule_change_in_past_and_cancellation(self) -> None:
        """
        Тест для сценарію користувача: о 16:29 додано 20:00-22:00 (10 год 30 хв без світла),
        а о ~20:15 обленерго скасувало це відключення (стало 8 год 30 хв).
        Сповіщення про скасування повинно надходити завжди: і о 20:15, і о 20:45, і після 22:00.
        """
        from app.services.formatter import has_meaningful_outage_changes

        slots_10h30 = [SlotState.ON] * 48
        for i in range(8, 12):  # 04:00-06:00
            slots_10h30[i] = SlotState.OFF
        for i in range(16, 21):  # 08:00-10:30
            slots_10h30[i] = SlotState.OFF
        for i in range(32, 36):  # 16:00-18:00
            slots_10h30[i] = SlotState.OFF
        for i in range(40, 48):  # 20:00-24:00
            slots_10h30[i] = SlotState.OFF

        s_with_outage = DaySchedule(
            queue="3.2", schedule_date=date(2026, 11, 6), slots=slots_10h30
        )
        self.assertEqual(s_with_outage.total_outage_minutes, 10 * 60 + 30)

        # Скасування відключення 20:00-22:00 (слоти 40..43 стають ON)
        slots_8h30 = list(slots_10h30)
        for i in range(40, 44):
            slots_8h30[i] = SlotState.ON

        s_cancelled = DaySchedule(
            queue="3.2", schedule_date=date(2026, 11, 6), slots=slots_8h30
        )
        self.assertEqual(s_cancelled.total_outage_minutes, 8 * 60 + 30)

        # Перевіряємо в різні моменти часу: під час інтервалу (20:15, 20:45) та після його завершення (22:05)
        times_to_test = [
            datetime(2026, 11, 6, 20, 15, tzinfo=ZoneInfo("Europe/Kyiv")),
            datetime(2026, 11, 6, 20, 45, tzinfo=ZoneInfo("Europe/Kyiv")),
            datetime(2026, 11, 6, 22, 5, tzinfo=ZoneInfo("Europe/Kyiv")),
        ]

        for test_time in times_to_test:
            self.assertTrue(
                has_meaningful_outage_changes(s_with_outage, s_cancelled, now=test_time),
                f"Failed to detect change at {test_time.strftime('%H:%M')}",
            )
            diff = format_schedule_diff(s_with_outage, s_cancelled, now=test_time)
            self.assertIn("20:00–22:00", diff)
            self.assertIn("Скасовано відключення", diff)
            self.assertIn("зменшився на <b>2 год</b>", diff)

    def test_outage_end_calculation_for_15min_warning(self) -> None:
        # Якщо о 07:45 попередження про відключення з 08:00 до 12:00 (слоти 16..23)
        hourly = [SlotState.ON] * 24
        for h in (8, 9, 10, 11):
            hourly[h] = SlotState.OFF
        sched = DaySchedule.from_hourly("3.2", date(2026, 11, 4), hourly)
        end_str, dur_str = NotificationEngine._calc_outage_end(
            sched, None, start_slot_idx=16, include_possible=True
        )
        self.assertEqual(end_str, "12:00")
        self.assertEqual(dur_str, "4 год")

    def test_parse_github_json_mirror_schema(self) -> None:
        from app.services.voe_client import VOEDataClient

        sample_json = {
            "fact": {
                "data": {
                    "2026-11-04": {
                        "GPV3.2": {
                            "1": "yes",
                            "2": "yes",
                            "3": "yes",
                            "4": "yes",
                            "5": "maybe",
                            "6": "maybe",
                            "7": "yes",
                            "8": "yes",
                            "9": "no",
                            "10": "no",
                            "11": "no",
                            "12": "no",
                            "13": "first",
                            "14": "second",
                        }
                    }
                }
            }
        }
        parsed = VOEDataClient.parse_github_json(sample_json)
        self.assertIn(("3.2", date(2026, 11, 4)), parsed)
        s = parsed[("3.2", date(2026, 11, 4))]
        self.assertEqual(s.hour_state(4), (SlotState.POSSIBLE_OFF, SlotState.POSSIBLE_OFF))
        self.assertEqual(s.hour_state(8), (SlotState.OFF, SlotState.OFF))
        self.assertEqual(s.hour_state(12), (SlotState.OFF, SlotState.ON))
        self.assertEqual(s.hour_state(13), (SlotState.ON, SlotState.OFF))

    def test_render_schedule_and_notification_images(self) -> None:
        hourly = [SlotState.ON] * 24
        hourly[4] = SlotState.OFF
        hourly[12] = SlotState.POSSIBLE_OFF
        sched = DaySchedule.from_hourly("3.2", date(2026, 11, 5), hourly)

        sched_png = render_schedule_image(
            sched, scale=2, now=datetime(2026, 11, 5, 23, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
        )
        self.assertIsNotNone(sched_png)
        self.assertTrue(sched_png.startswith(b"\x89PNG"))

        for kind, st in (
            ("light_on", None),
            ("outage_soon", SlotState.POSSIBLE_OFF),
            ("outage_soon", SlotState.OFF),
            ("schedule_updated", None),
        ):
            img_bytes = render_notification_image(kind, slot_state=st, scale=2)  # type: ignore
            self.assertIsNotNone(img_bytes)
            self.assertTrue(img_bytes.startswith(b"\x89PNG"))
            # Другий виклик повертає закешований об'єкт з пам'яті миттєво
            img_bytes_cached = render_notification_image(kind, slot_state=st, scale=2)  # type: ignore
            self.assertIs(img_bytes, img_bytes_cached)

        sched_png_cached = render_schedule_image(
            sched, scale=2, now=datetime(2026, 11, 5, 23, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
        )
        self.assertIs(sched_png, sched_png_cached)

    def test_anti_spam_middleware_logic(self) -> None:
        from app.bot.middlewares import AntiSpamMiddleware

        mw = AntiSpamMiddleware(
            min_message_interval=0.8,
            min_callback_interval=0.35,
            refresh_cooldown=5.0,
            max_burst=4,
            burst_window=5.0,
            cooldown_seconds=5.0,
            warn_cooldown=5.0,
        )

        # 1. Перше повідомлення о t=10.0 — дозволено
        allowed, warn, _ = mw.check_user_allowed(user_id=101, is_callback=False, now=10.0)
        self.assertTrue(allowed)
        self.assertFalse(warn)

        # 2. Друге повідомлення через 0.2 сек (t=10.2) — заблоковано, видається попередження
        allowed, warn, msg = mw.check_user_allowed(user_id=101, is_callback=False, now=10.2)
        self.assertFalse(allowed)
        self.assertTrue(warn)
        self.assertIn("Зачекайте", msg)

        # 3. Третє повідомлення через 0.3 сек (t=10.3) — заблоковано БЕЗ повторного попередження (щоб не спамити у відповідь)
        allowed, warn, _ = mw.check_user_allowed(user_id=101, is_callback=False, now=10.3)
        self.assertFalse(allowed)
        self.assertFalse(warn)

        # 4. Повідомлення через 1.0 сек після першого (t=11.0) — знову дозволено
        allowed, warn, _ = mw.check_user_allowed(user_id=101, is_callback=False, now=11.0)
        self.assertTrue(allowed)

        # 5. Кнопка "Оновити зараз" (sched:refresh) має 5-секундний кулдаун
        allowed, warn, _ = mw.check_user_allowed(
            user_id=202, is_callback=True, callback_data="sched:refresh:1:today", now=20.0
        )
        self.assertTrue(allowed)
        allowed, warn, _ = mw.check_user_allowed(
            user_id=202, is_callback=True, callback_data="sched:refresh:1:today", now=22.0
        )
        self.assertFalse(allowed)
        self.assertTrue(warn)
        allowed, warn, _ = mw.check_user_allowed(
            user_id=202, is_callback=True, callback_data="sched:refresh:1:today", now=25.5
        )
        self.assertTrue(allowed)

    def test_batch_sent_alerts_sqlite(self) -> None:
        import asyncio
        import tempfile
        from app.database import Database

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db = Database(
                    db_path=f"{tmpdir}/test.db",
                    database_url="",
                    supabase_url="",
                    supabase_key="",
                )
                await db.init_db()
                await db.mark_alerts_sent_batch(
                    [
                        (111, 1, "warn_off:2026-11-05:08:00"),
                        (222, 2, "warn_off:2026-11-05:08:00"),
                        (333, 3, "warn_on:2026-11-05:12:00"),
                    ]
                )
                found = await db.get_sent_alerts_for_keys(
                    ["warn_off:2026-11-05:08:00", "warn_on:2026-11-05:12:00"]
                )
                self.assertIn((111, 1, "warn_off:2026-11-05:08:00"), found)
                self.assertIn((222, 2, "warn_off:2026-11-05:08:00"), found)
                self.assertIn((333, 3, "warn_on:2026-11-05:12:00"), found)
                self.assertNotIn((999, 9, "warn_off:2026-11-05:08:00"), found)

                # Тестуємо кешування адреси -> черги
                await db.cache_address_queue(
                    city_name="м. Вінниця",
                    street_name="вул. Соборна",
                    house_num="24",
                    queue="1.1",
                )
                cached_q = await db.lookup_cached_address(
                    "м. Вінниця", "вул. Соборна", "24"
                )
                self.assertEqual(cached_q, "1.1")
                await db.close()

        asyncio.run(_run())

    def test_address_index_and_freeform_lookup(self) -> None:
        from app.services.address_index import AddressIndex

        idx = AddressIndex()
        self.assertGreater(len(idx.data.get("cities", [])), 1000)

        vin_matches = idx.search_cities("Вінниця")
        self.assertTrue(vin_matches)
        self.assertEqual(vin_matches[0]["name"], "м. Вінниця")

        vin_city = idx.get_city(vin_matches[0]["id"])
        self.assertIsNotNone(vin_city)
        self.assertGreater(len(vin_city["streets"]), 500)

        # Перевіряємо точне визначення черги за діапазонами та номерами будинків
        cases = [
            ("Соборна 24", "1.1"),
            ("Соборна 81", "2.1"),
            ("Соборна 10", "2.2"),
            ("Соборна 68", "3.2"),
            ("Келецька 10", "2.1"),
            ("Келецька 120", "4.2"),
            ("Агрономічне Мічуріна", "1.2"),
            ("Ладижин Суворова", "3.2"),
            ("Могилів-Подільський Полтавська", "3.1"),
            ("с. Побережне", "5.1"),
            # Випадки для Порика: дедуплікація аліасів та верифікована черга 3.2 (disconnection.voe.com.ua)
            ("Вінниця Порика 48", "3.2"),
            ("Порика 48", "3.2"),
            ("Порика 50", "4.2"),
            ("Порика 27а", "3.2"),
            ("Порика 15", "6.2"),
            ("Вінниця Василя Порика 3", "6.2"),
        ]
        for query, expected_queue in cases:
            res = idx.parse_and_search_freeform(query)
            self.assertTrue(res, f"No results for {query}")
            self.assertEqual(res[0]["queue"], expected_queue, f"Mismatch for {query}: {res}")
            # Для конкретних номерів переконуємось, що немає хибних дублікатів з іншими чергами
            if "48" in query:
                self.assertEqual(len(res), 1, f"Expected 1 unambiguous result for {query}, got {len(res)}: {res}")

    def test_save_and_notify_if_changed_broadcast(self) -> None:
        import asyncio
        import tempfile
        from unittest.mock import AsyncMock
        from app.database import Database
        from app.services.notifier import NotificationEngine
        from app.services.voe_client import VOEDataClient

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db = Database(
                    db_path=f"{tmpdir}/test_notify.db",
                    database_url="",
                    supabase_url="",
                    supabase_key="",
                )
                await db.init_db()
                await db.upsert_user(1001, username="testuser")
                await db.add_location(1001, label="🏠 Дім", queue="3.2")

                bot = AsyncMock()
                voe_client = VOEDataClient()
                engine = NotificationEngine(bot=bot, db=db, voe_client=voe_client)

                # 1. Початковий графік
                s1 = DaySchedule(
                    queue="3.2",
                    schedule_date=date(2026, 11, 6),
                    slots=[SlotState.ON] * 48,
                )
                changed = await engine.save_and_notify_if_changed(
                    s1,
                    now=datetime(2026, 11, 6, 8, 0, tzinfo=ZoneInfo("Europe/Kyiv")),
                )
                self.assertTrue(changed)

                # 2. Додано відключення 20:00-22:00
                slots_with_outage = [SlotState.ON] * 48
                for i in range(40, 44):
                    slots_with_outage[i] = SlotState.OFF
                s2 = DaySchedule(
                    queue="3.2",
                    schedule_date=date(2026, 11, 6),
                    slots=slots_with_outage,
                )
                changed = await engine.save_and_notify_if_changed(
                    s2,
                    now=datetime(2026, 11, 6, 16, 29, tzinfo=ZoneInfo("Europe/Kyiv")),
                )
                self.assertTrue(changed)
                call_count_1 = bot.send_photo.call_count + bot.send_message.call_count
                self.assertGreater(call_count_1, 0)

                # 3. Скасовано відключення 20:00-22:00 о 20:15
                s3 = DaySchedule(
                    queue="3.2",
                    schedule_date=date(2026, 11, 6),
                    slots=[SlotState.ON] * 48,
                )
                changed = await engine.save_and_notify_if_changed(
                    s3,
                    now=datetime(2026, 11, 6, 20, 15, tzinfo=ZoneInfo("Europe/Kyiv")),
                )
                self.assertTrue(changed)
                call_count_2 = bot.send_photo.call_count + bot.send_message.call_count
                self.assertGreater(call_count_2, call_count_1)

                await db.close()

        asyncio.run(_run())

    def test_parse_voe_detailed_div_html(self) -> None:
        from app.services.voe_client import VOEDataClient

        cells_html = ['<div class="disconnection-detailed-table-cell cell day_col">05.11.2026</div>']
        for h in range(24):
            if h == 4:
                # Повна година підтвердженого відключення
                cells_html.append(
                    '<div class="disconnection-detailed-table-cell cell hour_col has_disconnection full_hour confirm_1">'
                    '<div class="fill confirm_1" style="--start: 0; --size: 60"></div></div>'
                )
            elif h == 5:
                # Перші 30 хв можливого відключення
                cells_html.append(
                    '<div class="disconnection-detailed-table-cell cell hour_col has_disconnection confirm_0">'
                    '<div class="fill confirm_0" style="--start: 0; --size: 30"></div></div>'
                )
            elif h == 6:
                # Другі 30 хв підтвердженого відключення
                cells_html.append(
                    '<div class="disconnection-detailed-table-cell cell hour_col has_disconnection confirm_1">'
                    '<div class="fill confirm_1" style="--start: 30; --size: 30"></div></div>'
                )
            else:
                cells_html.append('<div class="disconnection-detailed-table-cell cell hour_col"></div>')

        sample_html = f"""
        <div class="disconnection-detailed-table">
          <p>2.1 черга</p>
          <div class="disconnection-detailed-table-container">
            {''.join(cells_html)}
          </div>
        </div>
        """
        res = VOEDataClient.parse_detailed_html(sample_html)
        self.assertIsNotNone(res)
        self.assertEqual(res.queue, "2.1")
        sched = res.schedules[date(2026, 11, 5)]
        self.assertEqual(sched.hour_state(3), (SlotState.ON, SlotState.ON))
        self.assertEqual(sched.hour_state(4), (SlotState.OFF, SlotState.OFF))
        self.assertEqual(sched.hour_state(5), (SlotState.POSSIBLE_OFF, SlotState.ON))
        self.assertEqual(sched.hour_state(6), (SlotState.ON, SlotState.OFF))

    def test_parse_bezsvitla_html_and_reconcile_sibling_queues(self) -> None:
        from app.services.voe_client import VOEDataClient

        html_3_1 = """
        <div class="bz-schedule-hero__meta">
          <span class="bz-schedule-hero__stamp bz-schedule-hero__stamp--updated">
            Оновлено: <time datetime="2026-11-07T09:55:00+02:00">07.11.2026 09:55</time>
          </span>
        </div>
        <div class="bz-schedule-countdown" data-bz-schedule-countdown
             data-schedule-tomorrow="[]"
             data-schedule-date="2026-11-07"
             data-schedule-intervals="[{&quot;from&quot;:&quot;00:00&quot;,&quot;to&quot;:&quot;04:00&quot;,&quot;state&quot;:&quot;ON&quot;},{&quot;from&quot;:&quot;04:00&quot;,&quot;to&quot;:&quot;06:00&quot;,&quot;state&quot;:&quot;OFF&quot;},{&quot;from&quot;:&quot;06:00&quot;,&quot;to&quot;:&quot;14:00&quot;,&quot;state&quot;:&quot;ON&quot;},{&quot;from&quot;:&quot;14:00&quot;,&quot;to&quot;:&quot;18:00&quot;,&quot;state&quot;:&quot;OFF&quot;},{&quot;from&quot;:&quot;18:00&quot;,&quot;to&quot;:&quot;22:00&quot;,&quot;state&quot;:&quot;ON&quot;},{&quot;from&quot;:&quot;22:00&quot;,&quot;to&quot;:&quot;24:00&quot;,&quot;state&quot;:&quot;OFF&quot;}]">
        </div>
        """
        html_3_2_stale = """
        <div class="bz-schedule-hero__meta">
          <span class="bz-schedule-hero__stamp bz-schedule-hero__stamp--updated">
            Оновлено: <time datetime="2026-11-06T21:05:00+02:00">06.11.2026 21:05</time>
          </span>
        </div>
        <div class="bz-schedule-countdown" data-bz-schedule-countdown
             data-schedule-tomorrow="[]"
             data-schedule-date="2026-11-07"
             data-schedule-intervals="[{&quot;from&quot;:&quot;00:00&quot;,&quot;to&quot;:&quot;04:00&quot;,&quot;state&quot;:&quot;ON&quot;},{&quot;from&quot;:&quot;04:00&quot;,&quot;to&quot;:&quot;06:00&quot;,&quot;state&quot;:&quot;OFF&quot;},{&quot;from&quot;:&quot;06:00&quot;,&quot;to&quot;:&quot;10:00&quot;,&quot;state&quot;:&quot;ON&quot;},{&quot;from&quot;:&quot;10:00&quot;,&quot;to&quot;:&quot;12:00&quot;,&quot;state&quot;:&quot;OFF&quot;},{&quot;from&quot;:&quot;12:00&quot;,&quot;to&quot;:&quot;14:00&quot;,&quot;state&quot;:&quot;ON&quot;},{&quot;from&quot;:&quot;14:00&quot;,&quot;to&quot;:&quot;18:00&quot;,&quot;state&quot;:&quot;OFF&quot;},{&quot;from&quot;:&quot;18:00&quot;,&quot;to&quot;:&quot;22:00&quot;,&quot;state&quot;:&quot;ON&quot;},{&quot;from&quot;:&quot;22:00&quot;,&quot;to&quot;:&quot;24:00&quot;,&quot;state&quot;:&quot;OFF&quot;}]">
        </div>
        """

        target_day = date(2026, 11, 7)
        s31, dt31, _ = VOEDataClient.parse_bezsvitla_queue_html(html_3_1, "3.1", target_day)
        s32, dt32, _ = VOEDataClient.parse_bezsvitla_queue_html(html_3_2_stale, "3.2", target_day)

        self.assertIsNotNone(s31)
        self.assertIsNotNone(s32)
        self.assertIsNotNone(dt31)
        self.assertIsNotNone(dt32)
        self.assertEqual(s31.total_outage_minutes, 8 * 60)
        self.assertEqual(s32.total_outage_minutes, 10 * 60)

        schedules = {
            ("3.1", target_day): s31,
            ("3.2", target_day): s32,
        }
        updated_timestamps = {
            "3.1": dt31,
            "3.2": dt32,
        }
        VOEDataClient._reconcile_stale_sibling_queues(
            schedules, updated_timestamps, target_day
        )

        reconciled_32 = schedules[("3.2", target_day)]
        self.assertEqual(reconciled_32.total_outage_minutes, 8 * 60)
        self.assertEqual(reconciled_32.hour_state(10), (SlotState.ON, SlotState.ON))
        self.assertEqual(reconciled_32.hour_state(11), (SlotState.ON, SlotState.ON))

    def test_widget_service_and_http_endpoints(self) -> None:
        import asyncio
        import json
        import tempfile
        from unittest.mock import AsyncMock
        from app.database import Database
        from app.main import start_healthcheck_server
        from app.services.voe_client import VOEDataClient
        from app.services.widget_service import (
            build_widget_payload,
            generate_user_loader_js,
            generate_widget_token,
            get_ota_ui_script_bytes,
            verify_widget_token,
        )

        # 1. Токен та генерація Svitlo.js Loader
        uid = 123456789
        tok = generate_widget_token(uid)
        self.assertTrue(verify_widget_token(uid, tok))
        self.assertFalse(verify_widget_token(uid, "wrong_token"))

        loader_bytes = generate_user_loader_js(
            user_id=uid,
            base_url="https://example.com",
            default_location_id=42,
        )
        loader_str = loader_bytes.decode("utf-8")
        self.assertIn('const BASE_URL = "https://example.com";', loader_str)
        self.assertIn('const USER_ID = "123456789";', loader_str)
        self.assertIn(f'const TOKEN = "{tok}";', loader_str)
        self.assertIn('const DEFAULT_LOCATION_ID = "42";', loader_str)
        self.assertIn("/api/widget/script", loader_str)

        ota_bytes = get_ota_ui_script_bytes()
        self.assertIn(b"SvitloWidgetRunner", ota_bytes)
        self.assertIn(b"refreshAfterDate", ota_bytes)

        # Перевіряємо, що вбудовані Base64 PNG-аватари в Svitlo.ota.js не пошкоджені і повністю декодуються
        import base64
        import io
        import re
        from PIL import Image

        ota_text = ota_bytes.decode("utf-8")
        for const_name in (
            "AVATAR_B64",
            "AVATAR_LS_B64",
            "AVATAR_POSSIBLE_B64",
            "AVATAR_POSSIBLE_LS_B64",
            "AVATAR_OUTAGE_B64",
            "AVATAR_OUTAGE_LS_B64",
        ):
            match = re.search(rf'const {const_name} = "([^"]+)";', ota_text)
            self.assertIsNotNone(match, f"Missing {const_name} in Svitlo.ota.js")
            raw_png = base64.b64decode(match.group(1))
            with Image.open(io.BytesIO(raw_png)) as av_im:
                av_im.load()
                self.assertEqual(av_im.size, (96, 96))

        # 2. Перевірка розрахунку статусу та часу до наступної зміни
        hourly = [SlotState.ON] * 24
        hourly[14] = SlotState.OFF
        hourly[15] = SlotState.OFF
        sched = DaySchedule.from_hourly("2.1", date(2026, 11, 9), hourly)

        now_online = datetime(2026, 11, 9, 11, 45, tzinfo=ZoneInfo("Europe/Kyiv"))
        payload_on = build_widget_payload(sched, None, now=now_online, location_label="🏠 Дім")
        self.assertEqual(payload_on["status"], "online")
        self.assertEqual(payload_on["currentGroup"], "Group 2.1")
        self.assertEqual(payload_on["timeUntilNextChange"], "2h 15m")
        self.assertEqual(payload_on["nextStatusTime"], "14:00")
        self.assertEqual(payload_on["nextChangeAt"], "2026-11-09T12:00:00Z")

        now_offline = datetime(2026, 11, 9, 14, 30, tzinfo=ZoneInfo("Europe/Kyiv"))
        payload_off = build_widget_payload(sched, None, now=now_offline, location_label="🏠 Дім")
        self.assertEqual(payload_off["status"], "offline")
        self.assertEqual(payload_off["timeUntilNextChange"], "1h 30m")
        self.assertEqual(payload_off["nextStatusTime"], "16:00")
        self.assertEqual(payload_off["nextChangeAt"], "2026-11-09T14:00:00Z")

        # 3. Перевірка HTTP-обробника (_handle_http_request для /api/widget/data та /api/widget/script)
        async def _run_http_test() -> None:
            from app.main import _handle_http_request

            class DummyWriter:
                def __init__(self) -> None:
                    self.buffer = bytearray()

                def write(self, data: bytes) -> None:
                    self.buffer.extend(data)

                async def drain(self) -> None:
                    pass

                def close(self) -> None:
                    pass

            with tempfile.TemporaryDirectory() as tmpdir:
                db = Database(
                    db_path=f"{tmpdir}/test_widget.db",
                    database_url="",
                    supabase_url="",
                    supabase_key="",
                )
                await db.init_db()
                await db.upsert_user(uid, username="tester")
                loc = await db.add_location(uid, label="🏠 Дім", queue="2.1")
                today = datetime.now(ZoneInfo("Europe/Kyiv")).date()
                await db.save_schedule(DaySchedule.from_hourly("2.1", today, hourly))

                voe_client = AsyncMock(spec=VOEDataClient)
                voe_client.fetch_all_queues_from_github.return_value = {}

                # Запит /api/widget/data
                reader = asyncio.StreamReader()
                req = (
                    f"GET /api/widget/data?userId={uid}&token={tok}&locationId={loc.id} HTTP/1.1\r\n"
                    f"Host: localhost\r\n\r\n"
                )
                reader.feed_data(req.encode("ascii"))
                reader.feed_eof()
                writer = DummyWriter()

                await _handle_http_request(reader, writer, db=db, voe_client=voe_client)  # type: ignore

                raw_resp = bytes(writer.buffer)
                header_part, body_part = raw_resp.split(b"\r\n\r\n", 1)
                self.assertIn(b"200 OK", header_part)
                data = json.loads(body_part.decode("utf-8"))
                self.assertIn(data["status"], ("online", "offline"))
                self.assertEqual(data["currentGroup"], "Group 2.1")
                self.assertEqual(data["queue"], "2.1")
                self.assertIn("timeUntilNextChange", data)
                self.assertIn("nextStatusTime", data)
                self.assertIn("lastUpdated", data)

                # Запит з невірним токеном -> 403 Forbidden
                reader_bad = asyncio.StreamReader()
                reader_bad.feed_data(
                    f"GET /api/widget/data?userId={uid}&token=invalid HTTP/1.1\r\nHost: localhost\r\n\r\n".encode("ascii")
                )
                reader_bad.feed_eof()
                writer_bad = DummyWriter()
                await _handle_http_request(reader_bad, writer_bad, db=db, voe_client=voe_client)  # type: ignore
                self.assertIn(b"403 Forbidden", bytes(writer_bad.buffer))

                # Запит /api/widget/script
                reader2 = asyncio.StreamReader()
                reader2.feed_data(
                    b"GET /api/widget/script?v=1 HTTP/1.1\r\nHost: localhost\r\n\r\n"
                )
                reader2.feed_eof()
                writer2 = DummyWriter()
                await _handle_http_request(reader2, writer2, db=db, voe_client=voe_client)  # type: ignore

                s_headers, s_body = bytes(writer2.buffer).split(b"\r\n\r\n", 1)
                self.assertIn(b"200 OK", s_headers)
                self.assertIn(b"SvitloWidgetRunner", s_body)

                await db.close()

        asyncio.run(_run_http_test())

    def test_schedule_cache_and_early_15min_warning_exit(self) -> None:
        import asyncio
        import tempfile
        from unittest.mock import AsyncMock
        from app.database import Database
        from app.services.notifier import NotificationEngine
        from app.services.voe_client import VOEDataClient

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db = Database(
                    db_path=f"{tmpdir}/test_perf.db",
                    database_url="",
                    supabase_url="",
                    supabase_key="",
                )
                await db.init_db()

                d_today = date(2026, 11, 11)
                sched = DaySchedule.from_hourly("3.2", d_today, [SlotState.ON] * 24)
                await db.save_schedule(sched)

                # Перевіряємо, що графік одразу в in-memory кеші
                self.assertIn(("3.2", "2026-11-11"), db._schedule_cache)
                cached = await db.get_schedule("3.2", d_today)
                self.assertIs(cached, sched)

                # Пакетне завантаження prefetch_schedules_for_dates
                db._schedule_cache.clear()
                prefetched = await db.prefetch_schedules_for_dates([d_today])
                self.assertIn(("3.2", d_today), prefetched)
                self.assertIn(("3.2", "2026-11-11"), db._schedule_cache)

                # Перевіряємо ранній вихід у check_15min_warnings поза вікном [14..16] хвилин
                bot = AsyncMock()
                voe_client = AsyncMock(spec=VOEDataClient)
                engine = NotificationEngine(bot=bot, db=db, voe_client=voe_client)
                db.get_all_active_subscriptions = AsyncMock(return_value=[])  # type: ignore

                # О 10:05 до найближчої межі (10:30) 25 хвилин -> ранній вихід без запитів у БД
                await engine.check_15min_warnings(
                    now=datetime(2026, 11, 11, 10, 5, tzinfo=ZoneInfo("Europe/Kyiv"))
                )
                db.get_all_active_subscriptions.assert_not_called()

                # О 10:15 до межі 10:30 рівно 15 хвилин -> перевірка підписок виконується
                await engine.check_15min_warnings(
                    now=datetime(2026, 11, 11, 10, 15, tzinfo=ZoneInfo("Europe/Kyiv"))
                )
                db.get_all_active_subscriptions.assert_called_once()

                await db.close()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
