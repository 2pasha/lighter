from aiogram.fsm.state import State, StatesGroup


class AddLocationStates(StatesGroup):
    """Кроки додавання нової локації (за чергою або за адресою)."""

    choosing_method = State()
    choosing_queue = State()
    entering_city = State()
    selecting_city = State()
    entering_street = State()
    selecting_street = State()
    entering_house = State()
    selecting_house = State()
    entering_label = State()


class QuietHoursStates(StatesGroup):
    """Введення кастомного діапазону тихих годин (наприклад 23:00-07:00)."""

    entering_range = State()
