import os
import re
import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command


# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()

BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip().lstrip("@")

PICKUP_LABEL = os.getenv("PICKUP_LABEL", "Самовывоз из Belgrade Waterfront").strip()
PICKUP_ADDRESS = os.getenv("PICKUP_ADDRESS", "Belgrade Waterfront").strip()
ARRIVAL_CONTACT = os.getenv("ARRIVAL_CONTACT", "@liusene").strip()
TZ_NAME = os.getenv("TZ", "Europe/Belgrade").strip()

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is missing")
if not ADMIN_IDS_RAW:
    raise RuntimeError('ENV ADMIN_IDS is missing (example: "123,456")')

ADMIN_IDS: List[int] = []
for x in re.split(r"[,\s]+", ADMIN_IDS_RAW):
    x = x.strip()
    if x:
        ADMIN_IDS.append(int(x))

TZ = ZoneInfo(TZ_NAME)

SUPPORT_TEXT = f"Если бот не отвечает или лагает — напишите в личку {ARRIVAL_CONTACT}"

DB_PATH = "sale.db"


# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                price TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cart (
                user_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                qty INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, item_id),
                FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                time TEXT NOT NULL,
                items_snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
            """
        )


def add_item(title: str, price: str) -> int:
    now_iso = datetime.now(TZ).isoformat()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO items(title, price, created_at) VALUES(?,?,?)",
            (title, price, now_iso),
        )
        return int(cur.lastrowid)


def list_items(limit: int = 50) -> List[Tuple[int, str, str]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, title, price FROM items ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def cart_add(user_id: int, item_id: int) -> Tuple[bool, str]:
    with db() as conn:
        it = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
        if not it:
            return False, "Товар не найден (возможно, удалён)."
        row = conn.execute(
            "SELECT qty FROM cart WHERE user_id=? AND item_id=?",
            (user_id, item_id),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE cart SET qty=qty+1 WHERE user_id=? AND item_id=?",
                (user_id, item_id),
            )
        else:
            conn.execute(
                "INSERT INTO cart(user_id, item_id, qty) VALUES(?,?,1)",
                (user_id, item_id),
            )
    return True, "Ок"


def cart_clear(user_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM cart WHERE user_id=?", (user_id,))


def get_cart_items(user_id: int) -> List[Tuple[int, str, str, int]]:
    """
    returns list of (item_id, title, price, qty)
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT c.item_id, i.title, i.price, c.qty
            FROM cart c
            JOIN items i ON i.id = c.item_id
            WHERE c.user_id=?
            ORDER BY c.item_id ASC
            """,
            (user_id,),
        ).fetchall()
    return [(int(r[0]), str(r[1]), str(r[2]), int(r[3])) for r in rows]


def booking_create(user_id: int, day_str: str, time_str: str, items_snapshot: str) -> int:
    now = datetime.now(TZ)
    expires = now + timedelta(hours=24)
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO bookings(user_id, day, time, items_snapshot, created_at, expires_at, status)
            VALUES(?,?,?,?,?,?, 'active')
            """,
            (user_id, day_str, time_str, items_snapshot, now.isoformat(), expires.isoformat()),
        )
        return int(cur.lastrowid)


def expire_bookings() -> int:
    now = datetime.now(TZ).isoformat()
    with db() as conn:
        cur = conn.execute(
            "UPDATE bookings SET status='expired' WHERE status='active' AND expires_at < ?",
            (now,),
        )
        return cur.rowcount


# =========================
# UI
# =========================
def kb_channel_item(item_id: int) -> InlineKeyboardMarkup:
    # кнопка в канале — callback приходит боту
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛒 В корзину", callback_data=f"add:{item_id}"),
                InlineKeyboardButton(
                    text="✅ Оформить в боте",
                    url=f"https://t.me/{BOT_USERNAME}?start=cart",
                ),
            ]
        ]
    )


def kb_cart() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Выбрать дату", callback_data="pick:day")],
            [InlineKeyboardButton(text="🧹 Очистить корзину", callback_data="cart:clear")],
        ]
    )


def kb_days() -> InlineKeyboardMarkup:
    today = datetime.now(TZ).date()
    rows = []
    for i in range(5):
        d = today + timedelta(days=i)
        rows.append(
            [InlineKeyboardButton(text=d.strftime("%Y-%m-%d"), callback_data=f"pick:day:{d.strftime('%Y-%m-%d')}")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад в корзину", callback_data="cart:show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_times(day_str: str) -> InlineKeyboardMarkup:
    day = datetime.strptime(day_str, "%Y-%m-%d").date()
    start = datetime.combine(day, dtime(9, 0))
    end = datetime.combine(day, dtime(20, 30))

    btns = []
    t = start
    while t <= end:
        btns.append(
            InlineKeyboardButton(
                text=t.strftime("%H:%M"),
                callback_data=f"pick:time:{day_str}:{t.strftime('%H:%M')}",
            )
        )
        t += timedelta(minutes=30)

    rows = [btns[i : i + 4] for i in range(0, len(btns), 4)]
    rows.append([InlineKeyboardButton(text="⬅️ Назад к дням", callback_data="pick:day")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_confirm(day_str: str, time_str: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить бронь", callback_data=f"book:confirm:{day_str}:{time_str}")],
            [InlineKeyboardButton(text="⬅️ Назад к времени", callback_data=f"pick:day:{day_str}")],
        ]
    )


def format_cart(user_id: int) -> str:
    items = get_cart_items(user_id)
    if not items:
        return f"🧺 Корзина пустая.\n\nℹ️ {SUPPORT_TEXT}"

    lines = [f"📍 {PICKUP_LABEL}", f"📌 Адрес: {PICKUP_ADDRESS}", ""]
    lines.append("🧺 Корзина:")
    for item_id, title, price, qty in items:
        price_part = f" — {price}" if price else ""
        q_part = f" x{qty}" if qty > 1 else ""
        lines.append(f"• {title}{price_part}{q_part}")
    lines.append("")
    lines.append(f"ℹ️ {SUPPORT_TEXT}")
    return "\n".join(lines)


def items_snapshot_text(user_id: int) -> str:
    items = get_cart_items(user_id)
    parts = []
    for _, title, price, qty in items:
        p = f"{title}"
        if price:
            p += f" ({price})"
        if qty > 1:
            p += f" x{qty}"
        parts.append(p)
    return "; ".join(parts) if parts else ""


# =========================
# BOT
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# -------- /start
@dp.message(Command("start"))
async def cmd_start(m: Message):
    # deep-link: /start cart
    arg = (m.text or "").split(maxsplit=1)
    if len(arg) == 2 and arg[1].strip() == "cart":
        await cmd_cart(m)
        return

    text = (
        "Привет 👋\n"
        "Добавляйте товары в корзину из канала и оформляйте бронь здесь.\n\n"
        "Команды:\n"
        "/cart — корзина\n"
        "/add — добавить товар (админ)\n\n"
        f"ℹ️ {SUPPORT_TEXT}"
    )
    await m.answer(text)


# -------- /cart
@dp.message(Command("cart"))
async def cmd_cart(m: Message):
    await m.answer(format_cart(m.from_user.id), reply_markup=kb_cart())


# -------- admin /add
@dp.message(Command("add"))
async def cmd_add(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("⛔️ Только для админов.")
        return

    await m.answer(
        "Ок. Пришли одним сообщением:\n"
        "<название> | <цена>\n\n"
        "Пример:\n"
        "Футболка белая | 2500 RSD\n\n"
        "Чтобы отменить: /cancel"
    )
    # ставим простой флаг в памяти (по user_id)
    dp["awaiting_add"] = dp.get("awaiting_add", set())
    dp["awaiting_add"].add(m.from_user.id)


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    s = dp.get("awaiting_add", set())
    if m.from_user.id in s:
        s.discard(m.from_user.id)
        await m.answer("Ок, отменено.")
    else:
        await m.answer("Нечего отменять.")


@dp.message()
async def catch_add_flow(m: Message):
    s = dp.get("awaiting_add", set())
    if m.from_user.id not in s:
        return

    txt = (m.text or "").strip()
    if not txt:
        await m.answer("Пусто. Пришли текст как в примере.")
        return

    if "|" in txt:
        title, price = [x.strip() for x in txt.split("|", 1)]
    else:
        title, price = txt.strip(), ""

    if not title:
        await m.answer("Название пустое. Пришли ещё раз.")
        return

    item_id = add_item(title=title, price=price)
    s.discard(m.from_user.id)

    await m.answer(f"✅ Товар добавлен: #{item_id}\nТеперь отправляю в канал…")

    if not CHANNEL_USERNAME:
        await m.answer("⚠️ ENV CHANNEL_USERNAME пустой. Я не могу отправить в канал.")
        return

    channel_text = f"🛍 {title}"
    if price:
        channel_text += f"\n💰 {price}"
    channel_text += f"\n\nℹ️ {SUPPORT_TEXT}"

    try:
        await bot.send_message(
            chat_id=f"@{CHANNEL_USERNAME}",
            text=channel_text,
            reply_markup=kb_channel_item(item_id),
            disable_web_page_preview=True,
        )
        await m.answer("✅ Отправлено в канал.")
    except Exception as e:
        await m.answer(f"❌ Не смог отправить в канал.\nПроверь что бот админ в канале.\nОшибка: {e}")


# -------- callbacks: cart
@dp.callback_query(F.data == "cart:show")
async def cb_cart_show(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(format_cart(cb.from_user.id), reply_markup=kb_cart())


@dp.callback_query(F.data == "cart:clear")
async def cb_cart_clear(cb: CallbackQuery):
    cart_clear(cb.from_user.id)
    await cb.answer("Корзина очищена", show_alert=True)
    await cb.message.edit_text(format_cart(cb.from_user.id), reply_markup=kb_cart())


# -------- callbacks: add from channel
@dp.callback_query(F.data.startswith("add:"))
async def cb_add_to_cart(cb: CallbackQuery):
    # IMPORTANT: always answer callback to avoid "button feels dead"
    try:
        item_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Ошибка кнопки", show_alert=True)
        return

    ok, msg = cart_add(cb.from_user.id, item_id)
    if ok:
        await cb.answer("✅ Добавлено в корзину", show_alert=True)
        # дублируем в личку, чтобы человек увидел куда идти
        try:
            await bot.send_message(
                cb.from_user.id,
                "🧺 Товар добавлен в корзину.\n"
                "Откройте: /cart\n\n"
                f"ℹ️ {SUPPORT_TEXT}",
            )
        except Exception:
            pass
    else:
        await cb.answer(f"⚠️ {msg}", show_alert=True)


# -------- callbacks: pick day / time
@dp.callback_query(F.data == "pick:day")
async def cb_pick_day(cb: CallbackQuery):
    await cb.answer()
    items = get_cart_items(cb.from_user.id)
    if not items:
        await cb.message.edit_text(f"🧺 Корзина пустая.\n\nℹ️ {SUPPORT_TEXT}")
        return

    # ВАЖНО: показываем самовывоз ДО выбора дня
    text = f"📍 {PICKUP_LABEL}\n📌 {PICKUP_ADDRESS}\n\nВыберите день:"
    await cb.message.edit_text(text, reply_markup=kb_days())


@dp.callback_query(F.data.startswith("pick:day:"))
async def cb_pick_day_value(cb: CallbackQuery):
    await cb.answer()
    day_str = cb.data.split(":", 2)[2]
    text = f"📍 {PICKUP_LABEL}\n\nВыберите время на {day_str}:"
    await cb.message.edit_text(text, reply_markup=kb_times(day_str))


@dp.callback_query(F.data.startswith("pick:time:"))
async def cb_pick_time(cb: CallbackQuery):
    await cb.answer()
    parts = cb.data.split(":")
    # pick:time:YYYY-MM-DD:HH:MM
    if len(parts) < 5:
        await cb.answer("Ошибка кнопки", show_alert=True)
        return
    day_str = parts[2]
    time_str = f"{parts[3]}:{parts[4]}"

    items = get_cart_items(cb.from_user.id)
    if not items:
        await cb.message.edit_text(f"🧺 Корзина пустая.\n\nℹ️ {SUPPORT_TEXT}")
        return

    text = (
        f"📍 {PICKUP_LABEL}\n\n"
        f"Подтвердите бронь:\n"
        f"🗓 {day_str} {time_str}\n"
        f"📦 Товаров: {len(items)}\n\n"
        "Бронь будет на 24 часа.\n\n"
        f"ℹ️ {SUPPORT_TEXT}"
    )
    await cb.message.edit_text(text, reply_markup=kb_confirm(day_str, time_str))


# -------- callbacks: confirm booking
@dp.callback_query(F.data.startswith("book:confirm:"))
async def cb_book_confirm(cb: CallbackQuery):
    await cb.answer()
    parts = cb.data.split(":")
    # book:confirm:YYYY-MM-DD:HH:MM
    if len(parts) < 5:
        await cb.answer("Ошибка кнопки", show_alert=True)
        return
    day_str = parts[2]
    time_str = f"{parts[3]}:{parts[4]}"

    items = get_cart_items(cb.from_user.id)
    if not items:
        await cb.message.edit_text(f"🧺 Корзина пустая.\n\nℹ️ {SUPPORT_TEXT}")
        return

    snap = items_snapshot_text(cb.from_user.id)
    booking_id = booking_create(cb.from_user.id, day_str, time_str, snap)

    cart_clear(cb.from_user.id)

    text = (
        f"✅ Бронь подтверждена!\n\n"
        f"📍 {PICKUP_LABEL}\n"
        f"🗓 {day_str} {time_str}\n"
        f"📦 {snap}\n\n"
        f"📌 Адрес: {PICKUP_ADDRESS}\n\n"
        f"Если опаздываете — напишите: {ARRIVAL_CONTACT}\n\n"
        f"Номер брони: #{booking_id}\n\n"
        f"ℹ️ {SUPPORT_TEXT}"
    )
    await cb.message.edit_text(text)


# =========================
# background
# =========================
async def expire_loop():
    while True:
        try:
            expire_bookings()
        except Exception:
            pass
        await asyncio.sleep(60)


async def main():
    init_db()

    # на всякий случай — чтобы не было конфликтов polling/webhook
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    asyncio.create_task(expire_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
