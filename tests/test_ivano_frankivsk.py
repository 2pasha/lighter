from __future__ import annotations

import tempfile
from datetime import date, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.config import (
    REGION_IF,
    REGION_VOE,
    is_valid_queue_key,
    make_queue_key,
    split_queue_key,
)
from app.database import Database
from app.models import DaySchedule, SlotState
from app.services.formatter import format_schedule_message
from app.services.if_client import (
    IFDataClient,
    generate_if_street_candidates,
    parse_freeform_if_address,
)
from app.services.image_parser import render_schedule_image
from app.services.notifier import NotificationEngine
from app.services.voe_client import VOEDataClient
from app.services.widget_service import resolve_widget_data


def test_region_queue_keys_and_models() -> None:
    assert split_queue_key("3.2") == (REGION_VOE, "3.2")
    assert split_queue_key("voe:3.2") == (REGION_VOE, "3.2")
    assert split_queue_key("if:4.1") == (REGION_IF, "4.1")
    assert split_queue_key("іф:6,2") == (REGION_IF, "6.2")

    assert make_queue_key(REGION_VOE, "3.2") == "3.2"
    assert make_queue_key(REGION_VOE, "if:3.2") == "3.2"
    assert make_queue_key(REGION_IF, "4.1") == "if:4.1"
    assert make_queue_key(REGION_IF, "if:4.1") == "if:4.1"

    assert is_valid_queue_key("3.2") is True
    assert is_valid_queue_key("if:4.1") is True
    assert is_valid_queue_key("if:9.9") is False
    assert is_valid_queue_key("unknown:1.1") is True  # falls back safely to voe

    sched_if = DaySchedule(queue="if:4.1", schedule_date=date(2026, 11, 25))
    assert sched_if.region == REGION_IF
    assert sched_if.queue_code == "4.1"

    sched_voe = DaySchedule(queue="3.2", schedule_date=date(2026, 11, 25))
    assert sched_voe.region == REGION_VOE
    assert sched_voe.queue_code == "3.2"


def test_if_popular_and_city_search() -> None:
    client = IFDataClient()
    popular = client.get_popular_cities()
    assert popular
    assert popular[0]["label"] == "м. Коломия"

    kol_matches = client.search_cities("Коломия")
    assert len(kol_matches) == 1
    assert kol_matches[0]["label"] == "м. Коломия"

    verbizh_matches = client.search_cities("Вербіж")
    labels = [m["label"] for m in verbizh_matches]
    assert "с. Нижній Вербіж" in labels
    assert "с. Верхній Вербіж" in labels

    # Довільне село Івано-Франківської області, якого немає у швидкому списку
    custom_village = client.search_cities("с. Коршів")
    assert custom_village
    assert custom_village[0]["label"] == "с. Коршів"


def test_if_freeform_and_street_candidates() -> None:
    assert parse_freeform_if_address("Коломия Чорновола 1") == (
        "м. Коломия",
        "Чорновола",
        "1",
    )
    assert parse_freeform_if_address("м. Коломия, вул. Січових Стрільців, 10а") == (
        "м. Коломия",
        "вул. Січових Стрільців",
        "10а",
    )
    assert parse_freeform_if_address("Івано-Франківськ Незалежності 10") == (
        "м. Івано-Франківськ",
        "Незалежності",
        "10",
    )
    assert parse_freeform_if_address("Шепарівці Шевченка 1") == (
        "с. Шепарівці",
        "Шевченка",
        "1",
    )

    chornovola_cands = generate_if_street_candidates("вул. В'ячеслава Чорновола")
    assert "В'ячеслава Чорновола" in chornovola_cands
    assert "Чорновола" in chornovola_cands

    sich_cands = generate_if_street_candidates("вул. Січових Стрільців")
    assert sich_cands[0] == "Січових Стрільців"
    assert "Стрільців" not in sich_cands


def test_if_parse_schedule_by_queue_and_search_responses() -> None:
    raw_queue_api = [
        {
            "eventDate": "25.11.2026",
            "queues": {
                "4.1": [
                    {"shutdownHours": "07:30-08:00", "from": "07:30", "to": "08:00", "status": 1},
                    {"shutdownHours": "14:00-17:30", "from": "14:00", "to": "17:30", "status": 1},
                    {"shutdownHours": "20:00-21:00", "from": "20:00", "to": "21:00", "status": 2},
                    {"shutdownHours": "22:00-00:00", "from": "22:00", "to": "00:00", "status": 1},
                ]
            },
            "createdAt": "24.11.2026 20:56",
            "scheduleApprovedSince": "25.11.2026 06:48",
        }
    ]
    parsed = IFDataClient.parse_queue_schedule_items(raw_queue_api, default_queue_code="4.1")
    assert ("if:4.1", date(2026, 11, 25)) in parsed
    sched = parsed[("if:4.1", date(2026, 11, 25))]
    assert sched.hour_state(7) == (SlotState.ON, SlotState.OFF)
    assert sched.hour_state(14) == (SlotState.OFF, SlotState.OFF)
    assert sched.hour_state(17) == (SlotState.OFF, SlotState.ON)
    assert sched.hour_state(20) == (SlotState.POSSIBLE_OFF, SlotState.POSSIBLE_OFF)
    assert sched.hour_state(22) == (SlotState.OFF, SlotState.OFF)
    assert sched.hour_state(23) == (SlotState.OFF, SlotState.OFF)
    assert sched.confirmed_outage_minutes == 360  # 0.5h + 3.5h + 2h = 6h
    assert sched.possible_outage_minutes == 60    # 1h

    # Exact address match (Коломия, Чорновола, 1 -> 4.1)
    exact_payload = {
        "current": {
            "queue": "4",
            "subQueue": "1",
            "hasQueue": "yes",
            "note": "За вказаною адресою Коломия, Чорновола, 1 споживач підпадає під чергу '4.1'",
        },
        "schedule": raw_queue_api,
    }
    res_exact = IFDataClient.parse_search_response(
        exact_payload,
        city_display="м. Коломия",
        street_display="вул. Чорновола",
        house_display="1",
    )
    assert res_exact.status == "exact"
    assert res_exact.queue_code == "4.1"
    assert res_exact.queue_key == "if:4.1"
    assert date(2026, 11, 25) in res_exact.schedules

    # Multi-queue building (300 Multiple Choices)
    many_payload = {
        "current": {
            "queue": "-1",
            "subQueue": "-1",
            "hasQueue": "many",
            "possibleQueues": ["1.1", "1.2"],
            "note": "Споживач може підпадати під декілька черг",
        },
        "schedule": [],
    }
    res_many = IFDataClient.parse_search_response(
        many_payload,
        city_display="м. Івано-Франківськ",
        street_display="вул. Незалежності",
        house_display="10",
    )
    assert res_many.status == "many"
    assert res_many.possible_queues == ["1.1", "1.2"]


@pytest.mark.asyncio
async def test_multi_region_coexistence_db_notifier_formatter_and_widget() -> None:
    tz = ZoneInfo("Europe/Kyiv")
    target_day = date(2026, 11, 25)
    now = datetime(2026, 11, 25, 11, 45, tzinfo=tz)  # 15 хв до 12:00!

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(
            db_path=f"{tmpdir}/multi_region.db",
            database_url="",
            supabase_url="",
            supabase_key="",
        )
        await db.init_db()
        await db.upsert_user(777, username="mover", first_name="Alex")

        # Користувач має дві локації з однаковим номером підчерги 4.1 у РІЗНИХ областях!
        loc_kolomyia = await db.add_location(
            telegram_id=777,
            label="🏠 Квартира",
            queue="if:4.1",
            city_name="м. Коломия",
            city_id="if",
            street_name="вул. Чорновола",
            street_id="if_st",
            house_num="1",
        )
        loc_vinnytsia = await db.add_location(
            telegram_id=777,
            label="👨‍👩‍👧 Батьки",
            queue="4.1",
            city_name="м. Вінниця",
            city_id="c_0",
            street_name="вул. Соборна",
            street_id="c_0_s_1",
            house_num="1",
        )

        assert loc_kolomyia.region == REGION_IF
        assert loc_kolomyia.queue_code == "4.1"
        assert loc_vinnytsia.region == REGION_VOE
        assert loc_vinnytsia.queue_code == "4.1"

        # Перевірка ізоляції address_cache між областями
        assert (
            await db.lookup_cached_address("м. Коломия", "вул. Чорновола", "1", region=REGION_IF)
            == "if:4.1"
        )
        assert (
            await db.lookup_cached_address("м. Коломия", "вул. Чорновола", "1", region=REGION_VOE)
            is None
        )

        # Різні графіки для if:4.1 (вимкнення о 12:00-14:00) та voe 4.1 (світло є весь день)
        slots_if = [SlotState.ON] * 48
        for idx in range(24, 28):  # 12:00..14:00
            slots_if[idx] = SlotState.OFF
        sched_if = DaySchedule(queue="if:4.1", schedule_date=target_day, slots=slots_if, source="if_api")

        slots_voe = [SlotState.ON] * 48
        sched_voe = DaySchedule(queue="4.1", schedule_date=target_day, slots=slots_voe, source="voe_json")

        await db.save_schedule(sched_if)
        await db.save_schedule(sched_voe)

        loaded_if = await db.get_schedule("if:4.1", target_day)
        loaded_voe = await db.get_schedule("4.1", target_day)
        assert loaded_if is not None and loaded_if.total_outage_minutes == 120
        assert loaded_voe is not None and loaded_voe.total_outage_minutes == 0

        # Перевіряємо форматування повідомлення та рендер PNG картки (без префікса if: у тексті!)
        msg_if = format_schedule_message(
            sched_if,
            location_label=loc_kolomyia.label,
            address_str=loc_kolomyia.display_address,
            now=now,
        )
        assert "Івано-Франківська обл." in msg_if
        assert "Черга <b>4.1</b>" in msg_if
        assert "if:4.1" not in msg_if

        png_if = render_schedule_image(sched_if, scale=1, now=now)
        png_voe = render_schedule_image(sched_voe, scale=1, now=now)
        assert png_if is not None and png_voe is not None
        assert png_if != png_voe  # Кеш зображень не змішує if:4.1 та 4.1!

        # Перевіряємо 15-хвилинне попередження о 11:45: має спрацювати ЛИШЕ для Коломиї!
        bot = AsyncMock()
        voe_client = VOEDataClient()
        engine = NotificationEngine(bot=bot, db=db, voe_client=voe_client)
        await engine.check_15min_warnings(now=now)

        assert bot.send_photo.call_count == 1
        sent_caption = bot.send_photo.call_args.kwargs.get("caption", "")
        assert "🏠 Квартира" in sent_caption
        assert "Івано-Франківська обл." in sent_caption
        assert "черга 4.1" in sent_caption
        assert "if:4.1" not in sent_caption
        assert "Вінницька" not in sent_caption

        # Перевіряємо роботу iOS Scriptable віджета для обох локацій
        w_kol = await resolve_widget_data(
            db=db, voe_client=voe_client, user_id=777, param="Коломия", now=now
        )
        assert w_kol["queue"] == "4.1"
        assert w_kol["region"] == REGION_IF
        assert w_kol["nextStatusTime"] == "12:00"

        w_voe = await resolve_widget_data(
            db=db, voe_client=voe_client, user_id=777, param="voe:4.1", now=now
        )
        assert w_voe["queue"] == "4.1"
        assert w_voe["region"] == REGION_VOE
        assert w_voe["nextStatusTime"] == ""

        # Перевіряємо збереження регіону при ручній зміні черги (напр. 4.1 -> 2.2)
        updated_kol = await db.update_location_queue(loc_kolomyia.id, 777, "2.2")
        assert updated_kol is not None
        assert updated_kol.queue == "if:2.2"
        assert updated_kol.queue_code == "2.2"
        assert updated_kol.region == REGION_IF

        await voe_client.close()
        await db.close()


@pytest.mark.asyncio
async def test_temporary_move_vinnytsia_to_kolomyia_and_back() -> None:
    """
    Повний сценарій тимчасового переїзду:
    1. У користувача збережено «🏠 Дім Вінниця» (id=1, черга 3.2) та встановлено iOS-віджет (defaultLocationId=1).
    2. Користувач додає «🏡 Квартира Коломия» (id=2, черга if:4.1) і вмикає «🎯 Тільки ця» для Коломиї.
    3. Сповіщення про зміни та 15-хвилинні попередження по Вінниці НЕ надходять, а по Коломиї — надходять;
       iOS-віджет автоматично перемикається на Коломию без перевстановлення Svitlo.js!
    4. Користувач ставить усі локації на паузу (відпустка) -> 0 активних підписок, доступні кнопки швидкого відновлення.
    5. Користувач повертається у Вінницю і натискає «🎯 Тільки ця» на Вінницю -> все (і сповіщення, і віджет) повертається на Вінницю, а Коломия залишається збереженою на паузі!
    """
    from app.bot.keyboards import (
        locations_list_kb,
        make_only_active_kb,
        resume_paused_locations_kb,
    )

    tz = ZoneInfo("Europe/Kyiv")
    target_day = date(2026, 11, 25)
    now = datetime(2026, 11, 25, 13, 45, tzinfo=tz)  # 15 хв до 14:00

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(
            db_path=f"{tmpdir}/relocation.db",
            database_url="",
            supabase_url="",
            supabase_key="",
        )
        await db.init_db()
        uid = 888
        await db.upsert_user(uid, username="traveler", first_name="Vatra")

        loc_vin = await db.add_location(
            telegram_id=uid,
            label="🏠 Дім Вінниця",
            queue="3.2",
            city_name="м. Вінниця",
            city_id="c_0",
            street_name="вул. Соборна",
            street_id="c_0_s_1",
            house_num="24",
        )
        loc_kol = await db.add_location(
            telegram_id=uid,
            label="🏡 Коломия",
            queue="if:4.1",
            city_name="м. Коломия",
            city_id="if",
            street_name="вул. Чорновола",
            street_id="if_st",
            house_num="1",
        )

        # Обидві локації спочатку активні
        locs_initial = await db.get_user_locations(uid)
        assert [l.is_active for l in locs_initial] == [True, True]
        kb_both = locations_list_kb(locs_initial)
        cb_data_both = [btn.callback_data for row in kb_both.inline_keyboard for btn in row]
        assert f"loc:only:{loc_vin.id}" in cb_data_both
        assert f"loc:only:{loc_kol.id}" in cb_data_both

        only_new_kb = make_only_active_kb(loc_kol.id, loc_kol.label)
        assert only_new_kb.inline_keyboard[0][0].callback_data == f"loc:only_new:{loc_kol.id}"

        # Переїзд у Коломию: натискаємо «🎯 Тільки ця» на Коломию
        focused_kol = await db.set_only_location_active(loc_kol.id, uid)
        assert focused_kol is not None and focused_kol.is_active is True

        locs_in_kol = await db.get_user_locations(uid)
        vin_state = next(l for l in locs_in_kol if l.id == loc_vin.id)
        kol_state = next(l for l in locs_in_kol if l.id == loc_kol.id)
        assert vin_state.is_active is False  # Вінниця збережена, але на паузі!
        assert kol_state.is_active is True   # Коломия активна!

        kb_kol_only = locations_list_kb(locs_in_kol)
        cb_data_kol = [btn.callback_data for row in kb_kol_only.inline_keyboard for btn in row]
        # Для вже єдиної активної локації зайва кнопка loc:only не показується, а для Вінниці на паузі — показується!
        assert f"loc:only:{loc_kol.id}" not in cb_data_kol
        assert f"loc:only:{loc_vin.id}" in cb_data_kol

        # Обидві черги мають вимкнення о 14:00 (слоти 28..31)
        slots_outage_14 = [SlotState.ON] * 48
        for idx in range(28, 32):
            slots_outage_14[idx] = SlotState.OFF
        await db.save_schedule(DaySchedule(queue="3.2", schedule_date=target_day, slots=slots_outage_14))
        await db.save_schedule(DaySchedule(queue="if:4.1", schedule_date=target_day, slots=slots_outage_14))

        bot = AsyncMock()
        voe_client = VOEDataClient()
        engine = NotificationEngine(bot=bot, db=db, voe_client=voe_client)

        # 1) 15-хвилинне попередження о 13:45 має надійти ТІЛЬКИ для Коломиї (Вінниця на паузі!)
        await engine.check_15min_warnings(now=now)
        assert bot.send_photo.call_count == 1
        warn_caption = bot.send_photo.call_args.kwargs.get("caption", "")
        assert "Коломия" in warn_caption
        assert "Вінниця" not in warn_caption

        # 2) Зміна графіка у Вінниці (3.2) НЕ турбує користувача, поки Вінниця на паузі
        bot.reset_mock()
        slots_vin_changed = list(slots_outage_14)
        slots_vin_changed[40] = SlotState.OFF
        await engine.save_and_notify_if_changed(
            DaySchedule(queue="3.2", schedule_date=target_day, slots=slots_vin_changed),
            now=now,
        )
        assert bot.send_photo.call_count == 0
        assert bot.send_message.call_count == 0

        # 3) iOS-віджет, який був згенерований ще у Вінниці (location_id=loc_vin.id, без param),
        #    автоматично перемикається на активну Коломию!
        widget_in_kol = await resolve_widget_data(
            db=db,
            voe_client=voe_client,
            user_id=uid,
            location_id=loc_vin.id,
            param="",
            now=now,
        )
        assert widget_in_kol["locationLabel"] == "🏡 Коломия"
        assert widget_in_kol["queue"] == "4.1"
        assert widget_in_kol["region"] == REGION_IF

        # 4) Ставимо і Коломию на паузу (0 активних локацій — режим відпустки)
        await db.set_location_active(loc_kol.id, uid, False)
        active_subs = await db.get_all_active_subscriptions()
        assert active_subs == []
        resume_kb = resume_paused_locations_kb(await db.get_user_locations(uid), "today")
        resume_cbs = [btn.callback_data for row in resume_kb.inline_keyboard for btn in row]
        assert f"loc:resume_show:{loc_vin.id}:today" in resume_cbs
        assert f"loc:resume_show:{loc_kol.id}:today" in resume_cbs

        # 5) Повернення у Вінницю: один клік «🎯 Тільки ця» на Вінницю!
        await db.set_only_location_active(loc_vin.id, uid)
        locs_back = await db.get_user_locations(uid)
        assert next(l for l in locs_back if l.id == loc_vin.id).is_active is True
        assert next(l for l in locs_back if l.id == loc_kol.id).is_active is False

        widget_back_in_vin = await resolve_widget_data(
            db=db,
            voe_client=voe_client,
            user_id=uid,
            location_id=loc_vin.id,
            param="",
            now=now,
        )
        assert widget_back_in_vin["locationLabel"] == "🏠 Дім Вінниця"
        assert widget_back_in_vin["queue"] == "3.2"
        assert widget_back_in_vin["region"] == REGION_VOE

        await voe_client.close()
        await db.close()
