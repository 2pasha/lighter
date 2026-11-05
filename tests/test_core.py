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

        # Перевіряємо, що минулі години та переходи лише між 🟡 і 🔴 ігноруються
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
        # Якщо зміна була о 10:00, а зараз вже 21:30 — вона в минулому і не турбує користувача
        self.assertFalse(
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
        ]
        for query, expected_queue in cases:
            res = idx.parse_and_search_freeform(query)
            self.assertTrue(res, f"No results for {query}")
            self.assertEqual(res[0]["queue"], expected_queue, f"Mismatch for {query}: {res}")

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


if __name__ == "__main__":
    unittest.main()
