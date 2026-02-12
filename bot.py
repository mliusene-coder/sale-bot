import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = "@bestgaragesale"     # канал
BOT_USERNAME = "my_sale_booking_bot"    # без @

TZ = ZoneInfo("Europe/Belgrade")

# Новые вводные:
PICKUP_AREA_LABEL = "Belgrade Waterfront"
PICKUP_ADDRESS_FULL = "BW Sole. Bulevar Vudroa Vilsona, 17"
ARRIVAL_CONTACT = "@liusene"

DB_PATH = "sale.db"

bot = Bot(TOKEN)
dp = Dispatcher()


# ---------------- DB ----------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def ensure_admins_table():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY)")
        con.commit()

def db_add_admin(user_id: int):
    ensure_admins_table()
    with db() as con:
        con.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (user_id,))
        con.commit()

def db_list_admins() -> list[int]:
    ensure_admins_table()
    with db() as con:
        rows = con.execute("SELECT user_id FROM admins").fetchall()
        return [int(r["user_id"]) for r in rows]

async def notify_admins(text: str):
    for admin_id in db_list_admins():
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


# ---------------- helpers ----------------
def now() -> datetime:
    return datetime.now(TZ)

def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m %H:%M")

def parse_iso(dt_iso: str) -> datetime:
    dt = datetime.fromisoformat(dt_iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)

def iter_slots_for_date(d):
    start = datetime.combine(d, time(9, 0), tzinfo=TZ)
    end = datetime.combine(d, time(21, 0), tzinfo=TZ)
    cur = start
    while cur < end:
        yield cur
        cur += timedelta(minutes=30)

async def dm(user_id: int, text: str, kb: InlineKeyboardMarkup | None = None):
    await bot.send_message(user_id, text, reply_markup=kb)


# ---------------- DB: items/cart/reservations ----------------
def db_add_item(base_caption: str, photo_file_id: str, channel_msg_id: int) -> int:
    with db() as con:
        cur = con.execute(
            "INSERT INTO items(base_caption, photo_file_id, channel_msg_id, status, reserved_until) "
            "VALUES(?,?,?,?,NULL)",
            (base_caption, photo_file_id, channel_msg_id, "free")
        )
        con.commit()
        return int(cur.lastrowid)

def db_get_item(item_id: int):
    with db() as con:
        return con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

def db_set_item_reserved(item_id: int, reserved_until_iso: str):
    with db() as con:
        con.execute("UPDATE items SET status='reserved', reserved_until=? WHERE id=?",
                    (reserved_until_iso, item_id))
        con.commit()

def db_set_item_free(item_id: int):
    with db() as con:
        con.execute("UPDATE items SET status='free', reserved_until=NULL WHERE id=?", (item_id,))
        con.commit()

def db_add_to_cart(user_id: int, item_id: int):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO carts(user_id,item_id) VALUES(?,?)", (user_id, item_id))
        con.commit()

def db_clear_cart(user_id: int):
    with db() as con:
        con.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
        con.commit()

def db_get_cart(user_id: int) -> list[int]:
    with db() as con:
        rows = con.execute("SELECT item_id FROM carts WHERE user_id=?", (user_id,)).fetchall()
        return [int(r["item_id"]) for r in rows]

def db_create_reservation(user_id: int, pickup_iso: str, expires_iso: str) -> int:
    with db() as con:
        cur = con.execute(
            "INSERT INTO reservations(user_id,pickup_dt,expires_at) VALUES(?,?,?)",
            (user_id, pickup_iso, expires_iso)
        )
        con.commit()
        return int(cur.lastrowid)

def db_set_reservation_items(res_id: int, item_ids: list[int]):
    with db() as con:
        con.executemany(
            "INSERT OR IGNORE INTO reservation_items(reservation_id,item_id) VALUES(?,?)",
            [(res_id, i) for i in item_ids]
        )
        con.commit()

def db_get_reservations_all():
    with db() as con:
        return con.execute("SELECT * FROM reservations").fetchall()

def db_get_reservation_items(res_id: int) -> list[int]:
    with db() as con:
        rows = con.execute("SELECT item_id FROM reservation_items WHERE reservation_id=?",
                           (res_id,)).fetchall()
        return [int(r["item_id"]) for r in rows]

def db_delete_reservation(res_id: int):
    with db() as con:
        con.execute("DELETE FROM reservation_items WHERE reservation_id=?", (res_id,))
        con.execute("DELETE FROM reservations WHERE id=?", (res_id,))
        con.commit()

def db_block_slot(slot_iso: str):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO blocked_slots(slot_dt) VALUES(?)", (slot_iso,))
        con.commit()

def db_is_slot_blocked(slot_iso: str) -> bool:
    with db() as con:
        row = con.execute("SELECT 1 FROM blocked_slots WHERE slot_dt=?", (slot_iso,)).fetchone()
        return row is not None


# ---------------- Channel UI ----------------
def channel_kb(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ В корзину", callback_data=f"add_{item_id}")],
        [InlineKeyboardButton(
            text="🛒 ОФОРМИТЬ БРОНЬ (ЗДЕСЬ)",
            url=f"https://t.me/{BOT_USERNAME}?start=cart"
        )],
    ])

def build_channel_caption(item_id: int, base_caption: str, status: str, reserved_until_iso: str | None):
    hint = "👇 Нажмите «🛒 ОФОРМИТЬ БРОНЬ (ЗДЕСЬ)», чтобы выбрать время"
    if status == "free":
        return f"🟢 Свободно\n\n{base_caption}\n\nID: {item_id}\n\n{hint}"
    until = fmt_dt(parse_iso(reserved_until_iso)) if reserved_until_iso else ""
    return f"🟡 Забронировано до {until}\n\n{base_caption}\n\nID: {item_id}\n\n{hint}"

def item_available(item_row) -> bool:
    if not item_row:
        return False
    if item_row["status"] == "free":
        return True
    if item_row["status"] == "reserved" and item_row["reserved_until"]:
        if parse_iso(item_row["reserved_until"]) <= now():
            db_set_item_free(int(item_row["id"]))
            return True
    return False

def slot_taken(slot_dt: datetime) -> bool:
    if db_is_slot_blocked(slot_dt.isoformat()):
        return True
    for r in db_get_reservations_all():
        expires = parse_iso(r["expires_at"])
        if expires <= now():
            continue
        if parse_iso(r["pickup_dt"]) == slot_dt:
            return True
    return False

async def update_channel_post(item_id: int):
    item = db_get_item(item_id)
    caption = build_channel_caption(
        int(item["id"]),
        item["base_caption"],
        item["status"],
        item["reserved_until"]
    )
    await bot.edit_message_caption(
        chat_id=CHANNEL_USERNAME,
        message_id=int(item["channel_msg_id"]),
        caption=caption,
        reply_markup=channel_kb(int(item["id"]))
    )


# ---------------- Commands ----------------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    if m.chat.type != "private":
        return
    await m.answer(
        "Привет 👋\n"
        "Добавляйте товары в корзину из канала и оформляйте бронь здесь.\n\n"
        "Команды:\n"
        "/cart — корзина\n"
        "/add — добавить товар (для тебя)\n"
        "/block — заблокировать слот (для тебя)\n"
        "/iamadmin — включить уведомления сюда (тебе и мужу)\n"
    )

@dp.message(Command("iamadmin"))
async def cmd_iamadmin(m: Message):
    if m.chat.type != "private":
        return
    db_add_admin(m.from_user.id)
    await m.answer("✅ Ок! Теперь сюда будут приходить уведомления о подтверждённых бронях.")

@dp.message(Command("cart"))
async def cmd_cart(m: Message):
    if m.chat.type != "private":
        return
    await show_cart(m.from_user.id)

@dp.message(Command("add"))
async def cmd_add(m: Message):
    if m.chat.type != "private":
        return
    await m.answer("Пришли фото с подписью: название + цена (€) + описание")

@dp.message(Command("block"))
async def cmd_block_help(m: Message):
    if m.chat.type != "private":
        return
    await m.answer("Заблокировать слот:\n/block 2026-02-12 14:30\nФормат: YYYY-MM-DD HH:MM")

@dp.message(F.text.startswith("/block "))
async def cmd_block(m: Message):
    if m.chat.type != "private":
        return
    try:
        _, date_s, time_s = m.text.split()
        dt = datetime.fromisoformat(f"{date_s} {time_s}").replace(tzinfo=TZ)
    except Exception:
        await m.answer("Не понял формат. Пример: /block 2026-02-12 14:30")
        return
    db_block_slot(dt.isoformat())
    await m.answer(f"✅ Слот {fmt_dt(dt)} заблокирован")


# ---------------- Add item ----------------
@dp.message(F.photo)
async def on_photo(m: Message):
    if m.chat.type != "private":
        return
    if not m.caption:
        await m.answer("Нужна подпись к фото ❗")
        return

    msg = await bot.send_photo(
        chat_id=CHANNEL_USERNAME,
        photo=m.photo[-1].file_id,
        caption="Публикую…"
    )

    item_id = db_add_item(m.caption, m.photo[-1].file_id, msg.message_id)
    await update_channel_post(item_id)
    await m.answer(f"✅ Товар добавлен в канал (ID {item_id})")


# ---------------- Cart flow ----------------
async def show_cart(user_id: int):
    item_ids = db_get_cart(user_id)
    if not item_ids:
        await dm(user_id, "Корзина пустая")
        return

    cleaned, lines = [], []
    for item_id in sorted(item_ids):
        it = db_get_item(item_id)
        if it and item_available(it):
            cleaned.append(item_id)
            lines.append(f"• #{item_id} — {it['base_caption']}")

    db_clear_cart(user_id)
    for i in cleaned:
        db_add_to_cart(user_id, i)

    if not cleaned:
        await dm(user_id, "Товары из корзины уже заняты/пропали. Сейчас корзина пустая.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕒 Выбрать время", callback_data="pick_day")],
        [InlineKeyboardButton(text="Очистить корзину", callback_data="clear_cart")]
    ])
    await dm(user_id, "🛒 Ваша корзина:\n\n" + "\n".join(lines), kb)

@dp.callback_query(F.data == "clear_cart")
async def cb_clear_cart(cb: CallbackQuery):
    db_clear_cart(cb.from_user.id)
    await cb.answer("Очищено", show_alert=True)
    await dm(cb.from_user.id, "Корзина очищена ✅")

@dp.callback_query(F.data.startswith("add_"))
async def cb_add_to_cart(cb: CallbackQuery):
    item_id = int(cb.data.split("_")[1])
    it = db_get_item(item_id)

    if not it:
        await cb.answer("Товар не найден (возможно, старый пост).", show_alert=True)
        return
    if not item_available(it):
        await cb.answer("Этот товар уже забронирован/продан", show_alert=True)
        return

    db_add_to_cart(cb.from_user.id, item_id)

    await cb.answer(
        "✅ Добавлено!\n"
        "👇 Нажмите кнопку «🛒 ОФОРМИТЬ БРОНЬ (ЗДЕСЬ)» под товаром,\n"
        "или откройте бота и напишите /cart",
        show_alert=True
    )
    try:
        await show_cart(cb.from_user.id)
    except Exception:
        pass


# ---------------- Pick day/time ----------------
@dp.callback_query(F.data == "pick_day")
async def cb_pick_day(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id

    today = now().date()
    days = [today + timedelta(days=i) for i in range(5)]  # 5 дней вперед

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(d), callback_data=f"day_{d.isoformat()}")]
        for d in days
    ])
    await dm(uid, "Выберите день:", kb)

@dp.callback_query(F.data.startswith("day_"))
async def cb_pick_time(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    d = datetime.fromisoformat(cb.data.split("_", 1)[1]).date()

    rows, row = [], []
    for slot in iter_slots_for_date(d):
        if slot < now() + timedelta(minutes=30):
            continue
        if slot_taken(slot):
            continue
        row.append(InlineKeyboardButton(text=slot.strftime("%H:%M"), callback_data=f"time_{slot.isoformat()}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if not rows:
        await dm(uid, "На этот день свободных слотов нет. Выберите другой день: /cart")
        return

    await dm(uid, f"Выберите время:\nСамовывоз из {PICKUP_AREA_LABEL}",
             InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("time_"))
async def cb_confirm(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id

    slot = parse_iso(cb.data.split("_", 1)[1])
    if slot_taken(slot):
        await dm(uid, "Этот слот уже занят. Начните заново: /cart")
        return

    cart = db_get_cart(uid)
    if not cart:
        await dm(uid, "Корзина пустая.")
        return

    ok_items = []
    for i in cart:
        it = db_get_item(i)
        if it and item_available(it):
            ok_items.append(i)

    if not ok_items:
        db_clear_cart(uid)
        await dm(uid, "Товары уже заняты. Корзина очищена.")
        return

    text = (
        "Подтвердите бронь:\n"
        f"📍 Самовывоз из {PICKUP_AREA_LABEL}\n"
        f"🕒 Время: {fmt_dt(slot)}\n"
        f"📦 Товаров: {len(ok_items)}\n\n"
        "Бронь будет на 24 часа."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{slot.isoformat()}"),
        InlineKeyboardButton(text="Отмена", callback_data="noop")
    ]])
    await dm(uid, text, kb)

@dp.callback_query(F.data.startswith("confirm_"))
async def cb_do_reserve(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id

    slot = parse_iso(cb.data.split("_", 1)[1])
    if slot_taken(slot):
        await dm(uid, "Слот уже занят. Начните заново: /cart")
        return

    cart = db_get_cart(uid)
    if not cart:
        await dm(uid, "Корзина пустая.")
        return

    ok_items, item_texts = [], []
    for i in cart:
        it = db_get_item(i)
        if it and item_available(it):
            ok_items.append(i)
            item_texts.append(f"#{i} — {it['base_caption']}")

    if not ok_items:
        await dm(uid, "Товары уже заняты. /cart")
        return

    expires_at = now() + timedelta(hours=24)
    res_id = db_create_reservation(uid, slot.isoformat(), expires_at.isoformat())
    db_set_reservation_items(res_id, ok_items)

    for item_id in ok_items:
        db_set_item_reserved(item_id, expires_at.isoformat())
        await update_channel_post(item_id)

    db_clear_cart(uid)

    user_tag = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name

    # Уведомления админам — только ПОСЛЕ подтверждения
    await notify_admins(
        "🟡 Подтверждённая бронь\n"
        f"Покупатель: {user_tag}\n"
        f"Самовывоз: {fmt_dt(slot)}\n"
        f"Бронь до: {fmt_dt(expires_at)}\n\n"
        "Товары:\n" + "\n".join(item_texts)
    )

    await dm(
        uid,
        "✅ Готово! Бронь оформлена.\n\n"
        f"📍 Адрес: {PICKUP_ADDRESS_FULL}\n"
        f"🕒 Самовывоз: {fmt_dt(slot)}\n"
        f"⏳ Бронь действует до: {fmt_dt(expires_at)}\n\n"
        f"Когда подъедете — напишите: {ARRIVAL_CONTACT}\n"
        "Пожалуйста, не звоните в домофон, пишите в Telegram."
    )

@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ---------------- expiry loop ----------------
async def expire_loop():
    while True:
        now_dt = now()
        rows = db_get_reservations_all()
        for r in rows:
            expires = parse_iso(r["expires_at"])
            if expires > now_dt:
                continue

            res_id = int(r["id"])
            item_ids = db_get_reservation_items(res_id)

            for item_id in item_ids:
                db_set_item_free(item_id)
                try:
                    await update_channel_post(item_id)
                except Exception:
                    pass

            db_delete_reservation(res_id)
            await notify_admins("⏳ Бронь истекла. Товары снова свободны: " + ", ".join("#"+str(i) for i in item_ids))

        await asyncio.sleep(30)


async def main():
    # polling + no webhook
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    asyncio.create_task(expire_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
