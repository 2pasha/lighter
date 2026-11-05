from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

try:
    from aiogram import BaseMiddleware
    from aiogram.types import CallbackQuery, Message, TelegramObject
except ImportError:
    BaseMiddleware = object  # type: ignore
    CallbackQuery = Any  # type: ignore
    Message = Any  # type: ignore
    TelegramObject = Any  # type: ignore

logger = logging.getLogger(__name__)


@dataclass
class _UserThrottleState:
    timestamps: deque[float] = field(default_factory=deque)
    last_message_at: float = 0.0
    last_callback_at: float = 0.0
    last_refresh_at: float = 0.0
    blocked_until: float = 0.0
    last_warned_at: float = 0.0


class AntiSpamMiddleware(BaseMiddleware):  # type: ignore[misc]
    """
    Middleware для захисту бота від спаму командами та натисканнями кнопок:
    1. Обмежує мінімальний інтервал між повідомленнями (min_message_interval) та колбеками (min_callback_interval).
    2. Окремий кулдаун на важку кнопку «🔄 Оновити зараз» (refresh_cooldown = 5 сек).
    3. Захист від серійного спаму (burst): більше `max_burst` дій за `burst_window` сек -> пауза на `cooldown_seconds`.
    4. Відправляє лише 1 попередження за період блокування, мовчки ігноруючи подальший флуд (щоб не витрачати ліміти Telegram API).
    """

    def __init__(
        self,
        min_message_interval: float = 0.8,
        min_callback_interval: float = 0.35,
        refresh_cooldown: float = 5.0,
        max_burst: int = 7,
        burst_window: float = 5.0,
        cooldown_seconds: float = 5.0,
        warn_cooldown: float = 5.0,
    ) -> None:
        super().__init__()
        self.min_message_interval = min_message_interval
        self.min_callback_interval = min_callback_interval
        self.refresh_cooldown = refresh_cooldown
        self.max_burst = max_burst
        self.burst_window = burst_window
        self.cooldown_seconds = cooldown_seconds
        self.warn_cooldown = warn_cooldown
        self._users: dict[int, _UserThrottleState] = {}
        self._last_cleanup: float = 0.0

    def check_user_allowed(
        self,
        user_id: int,
        is_callback: bool = False,
        callback_data: Optional[str] = None,
        now: Optional[float] = None,
    ) -> tuple[bool, bool, str]:
        """
        Перевіряє, чи дозволена дія користувача.
        Повертає кортеж: (allowed, should_warn, reason_message).
        """
        if now is None:
            now = time.monotonic()

        if len(self._users) > 1000 and (now - self._last_cleanup) > 120.0:
            self._cleanup(now)

        st = self._users.get(user_id)
        if st is None:
            st = _UserThrottleState()
            self._users[user_id] = st

        # 1. Якщо користувач уже в тимчасовому блокуванні через спам
        if now < st.blocked_until:
            should_warn = (now - st.last_warned_at) >= self.warn_cooldown
            if should_warn:
                st.last_warned_at = now
            return (
                False,
                should_warn,
                "⏳ Забагато запитів поспіль! Зачекайте кілька секунд перед наступною дією.",
            )

        # 2. Перевірка спеціального кулдауну на кнопку примусового оновлення «sched:refresh:...»
        if is_callback and callback_data and callback_data.startswith("sched:refresh:"):
            if (now - st.last_refresh_at) < self.refresh_cooldown:
                should_warn = (now - st.last_warned_at) >= 1.5
                if should_warn:
                    st.last_warned_at = now
                return (
                    False,
                    should_warn,
                    "⏳ Дані щойно оновлено. Зачекайте кілька секунд.",
                )

        # 3. Перевірка мінімального інтервалу між двома подіями поспіль
        if is_callback:
            if (now - st.last_callback_at) < self.min_callback_interval:
                should_warn = (now - st.last_warned_at) >= self.warn_cooldown
                if should_warn:
                    st.last_warned_at = now
                return (
                    False,
                    should_warn,
                    "⏳ Не так швидко! Зачекайте секунду.",
                )
        else:
            if (now - st.last_message_at) < self.min_message_interval:
                should_warn = (now - st.last_warned_at) >= self.warn_cooldown
                if should_warn:
                    st.last_warned_at = now
                return (
                    False,
                    should_warn,
                    "⏳ Забагато команд поспіль! Зачекайте кілька секунд.",
                )

        # 4. Перевірка серійного спаму (sliding window)
        cutoff = now - self.burst_window
        while st.timestamps and st.timestamps[0] < cutoff:
            st.timestamps.popleft()

        st.timestamps.append(now)
        if len(st.timestamps) > self.max_burst:
            st.blocked_until = now + self.cooldown_seconds
            should_warn = (now - st.last_warned_at) >= self.warn_cooldown
            if should_warn:
                st.last_warned_at = now
            logger.info(
                "User %s throttled for %.1fs due to burst spam (%d events in %.1fs)",
                user_id,
                self.cooldown_seconds,
                len(st.timestamps),
                self.burst_window,
            )
            return (
                False,
                should_warn,
                "⏳ Забагато запитів поспіль! Зачекайте кілька секунд перед наступною дією.",
            )

        # Дозволено — оновлюємо мітки часу
        if is_callback:
            st.last_callback_at = now
            if callback_data and callback_data.startswith("sched:refresh:"):
                st.last_refresh_at = now
        else:
            st.last_message_at = now

        return True, False, ""

    def _cleanup(self, now: float) -> None:
        self._last_cleanup = now
        stale_cutoff = now - 300.0
        stale_ids = [
            uid
            for uid, st in self._users.items()
            if st.last_message_at < stale_cutoff
            and st.last_callback_at < stale_cutoff
            and st.blocked_until < now
        ]
        for uid in stale_ids:
            self._users.pop(uid, None)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        is_cb = isinstance(event, CallbackQuery)
        cb_data = getattr(event, "data", None) if is_cb else None

        allowed, should_warn, reason = self.check_user_allowed(
            user_id=user.id,
            is_callback=is_cb,
            callback_data=cb_data,
        )
        if allowed:
            return await handler(event, data)

        if should_warn:
            try:
                if is_cb:
                    await event.answer(reason, show_alert=False)  # type: ignore[union-attr]
                elif isinstance(event, Message):
                    await event.answer(reason)
            except Exception:
                pass
        elif is_cb:
            # Для колбеків тихо закриваємо спінер без тексту, щоб клієнт Telegram не висів
            try:
                await event.answer()  # type: ignore[union-attr]
            except Exception:
                pass
        return None
