from __future__ import annotations

import asyncio
import getpass
import io
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from PIL import Image

# Публічні офіційні ключі Telegram Desktop (дозволяють не чекати код на my.telegram.org)
DEFAULT_TG_DESKTOP_API_ID = 2040
DEFAULT_TG_DESKTOP_API_HASH = "b18441a1ff607e10a989891a5462e627"


def _render_qr_png_to_terminal(png_bytes: bytes) -> str:
    """Конвертує PNG-картинку QR-коду у компактний текстовий QR у терміналі (Unicode half-blocks)."""
    with Image.open(io.BytesIO(png_bytes)) as im:
        im_gray = im.convert("L")
        # Стискаємо до розміру модуля QR (~37x37..45x45 пікселів)
        target_size = 41
        im_small = im_gray.resize((target_size, target_size), Image.Resampling.NEAREST)
        w, h = im_small.size
        px = im_small.load()

        lines: list[str] = []
        for y in range(0, h, 2):
            row_chars: list[str] = []
            for x in range(w):
                top_dark = px[x, y] < 128
                bot_dark = (px[x, y + 1] < 128) if (y + 1 < h) else False
                if top_dark and bot_dark:
                    row_chars.append("█")
                elif top_dark and not bot_dark:
                    row_chars.append("▀")
                elif not top_dark and bot_dark:
                    row_chars.append("▄")
                else:
                    row_chars.append(" ")
            lines.append("  " + "".join(row_chars))
        return "\n".join(lines)


async def _show_qr_code(url: str) -> None:
    qr_api_url = f"https://api.qrserver.com/v1/create-qr-code/?size=420x420&margin=16&data={quote(url, safe='')}"
    qr_path = Path("/tmp/svitlo_tg_qr.png")
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(qr_api_url)
            resp.raise_for_status()
            png_bytes = resp.content
            qr_path.write_bytes(png_bytes)

        try:
            print("\n" + _render_qr_png_to_terminal(png_bytes) + "\n")
        except Exception:
            pass

        if sys.platform == "darwin":
            subprocess.run(["open", str(qr_path)], check=False)
            print(f"🖼 Вікно з QR-кодом також відкрито на екрані ({qr_path}).")
    except Exception as exc:
        print(f"⚠️ Не вдалося завантажити PNG QR-коду ({exc}).")
        print(f"Посилання для входу: {url}")


async def main() -> None:
    load_dotenv()
    print("=" * 70)
    print("🔑 Швидка авторизація Telethon через QR-код (БЕЗ СМС та кодів!)")
    print("=" * 70)

    api_id_raw = os.getenv("TELETHON_API_ID", "").strip()
    api_hash = os.getenv("TELETHON_API_HASH", "").strip()

    if api_id_raw.isdigit() and api_hash:
        api_id = int(api_id_raw)
    else:
        print(
            "💡 Використовуємо вбудовані ключі Telegram Desktop (my.telegram.org не потрібен).\n"
        )
        api_id = DEFAULT_TG_DESKTOP_API_ID
        api_hash = DEFAULT_TG_DESKTOP_API_HASH

    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        device_model="MacBook Pro",
        system_version="macOS 15.0",
        app_version="5.6.3",
    )
    await client.connect()

    try:
        if not await client.is_user_authorized():
            print("📲 Як увійти за 5 секунд:")
            print("   1. Відкрийте Telegram на телефоні")
            print("   2. Перейдіть у: Налаштування (Settings) -> Пристрої (Devices)")
            print("   3. Натисніть «Підключити пристрій» (Link Desktop Device)")
            print("   4. Наведіть камеру на QR-код, який зараз з'явиться:\n")

            while not await client.is_user_authorized():
                qr_login = await client.qr_login()
                await _show_qr_code(qr_login.url)
                try:
                    await qr_login.wait(timeout=45)
                    break
                except asyncio.TimeoutError:
                    print("🔄 Час дії QR-коду минув, генерую новий QR-код...")
                except SessionPasswordNeededError:
                    print("\n🔒 На вашому акаунті увімкнено хмарний пароль (2FA).")
                    pwd = getpass.getpass("Введіть ваш хмарний пароль Telegram (символи не відображаються): ")
                    if not pwd:
                        pwd = input("Введіть ваш хмарний пароль Telegram: ").strip()
                    await client.sign_in(password=pwd)
                    break

        session_str = client.session.save()
        me = await client.get_me()
        username_part = f" (@{me.username})" if getattr(me, "username", None) else ""
        print(f"\n✅ Успішно авторизовано як: {getattr(me, 'first_name', 'User')}{username_part}!")

        env_path = Path(__file__).resolve().parent.parent.parent / ".env"
        if env_path.exists():
            content = env_path.read_text(encoding="utf-8")
            content = _set_env_var(content, "TELETHON_ENABLED", "true")
            content = _set_env_var(content, "TELETHON_API_ID", str(api_id))
            content = _set_env_var(content, "TELETHON_API_HASH", api_hash)
            content = _set_env_var(content, "TELETHON_SESSION_STRING", session_str)
            env_path.write_text(content, encoding="utf-8")
            print(f"🎉 Усі ключі та TELETHON_SESSION_STRING автоматично збережено у {env_path}!")
        else:
            print("\nСкопіюйте у ваш .env:")
            print(f"TELETHON_ENABLED=true")
            print(f"TELETHON_API_ID={api_id}")
            print(f"TELETHON_API_HASH={api_hash}")
            print(f"TELETHON_SESSION_STRING={session_str}")
    finally:
        await client.disconnect()


def _set_env_var(env_text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f"{key}={value}"
    if pattern.search(env_text):
        return pattern.sub(line, env_text)
    suffix = "" if env_text.endswith("\n") else "\n"
    return f"{env_text}{suffix}{line}\n"


if __name__ == "__main__":
    asyncio.run(main())
