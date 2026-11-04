from __future__ import annotations

import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.models import DaySchedule, SlotState
from app.services.formatter import format_schedule_diff, format_schedule_message
from app.services.image_parser import classify_rgb, parse_voe_schedule_image
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
        if not UPLOADED_IMAGE_PATH.exists():
            self.skipTest("Sample image not found in environment")

        raw = UPLOADED_IMAGE_PATH.read_bytes()
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


if __name__ == "__main__":
    unittest.main()
