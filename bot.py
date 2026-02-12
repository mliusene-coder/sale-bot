import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# =========================
# CONFIG from Railway Variables (Env)
# =========================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_IDS_RAW = (os.getenv("ADMIN_IDS") or "").strip()  # e.g. "123,456"
CHANNEL_USERNAME = (os.getenv("CHANNEL_USERNAME") or "@bestgaragesale").strip()
BOT_USERNAME = (os.getenv("BOT_USERNAME") or "my_sale_booking_bot").strip().lstrip("@")

TZ_NAME = (os.getenv("TZ") or "Europe/Belgrade").strip()

PICKUP_LABEL = (os.getenv("PICKUP_LABEL") or "Самовывоз из Belgrade Waterfront").strip()
PICKUP_ADDRESS = (os.getenv("PICKUP_ADDRESS") or "BW Sole. Bulevar Vudroa Vilsona, 17").strip()
ARRIVAL_CONTACT = (os.getenv("ARRIVAL_CONTACT") or "@liusene").strip()

DB_PATH = (os.getenv("DB_PATH") or "sale.db").strip()

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is missing")
ADMIN_IDS = [int(x) for x in ADMIN_IDS_RAW.replace(" ", "").split(",") if x]
if not ADMIN_IDS:
    raise RuntimeError('ENV ADMIN_IDS is missing (example: "123,456")')

if not CHANNEL_USERNAME.startswith("@"):
    CHANNEL_USERNAME = "@" + CHANNEL_USERNAME

TZ = ZoneInfo(TZ_NAME)

SUPPORT_TEXT = f"Если бот не отвечает или лагает — напишите в личку {ARRIVAL_CONTACT}"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
@dp.callback_query()
async def debug_all(cb):
    await cb.answer()
    print("CLICK:", cb.data)



# =========================
# DB
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            price TEXT NOT NULL,
            photo_file_id TEXT,
            status TEXT NOT NULL DEFAULT 'free',   -- free/reserved/sold
            reserved_until_iso TEXT,
            reserved_by INTEGER,
            channel_message_id INTEGER
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS cart (
            user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            PRIMARY KEY(user_id, item_id)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            item_ids TEXT NOT NULL,
            pickup_iso TEXT NOT NULL,
            created_iso TEXT NOT NULL,
            expires_iso TEXT NOT NULL
        );
        """)
        conn.commit()


# =========================
# helpers
# =========================
def now() -> datetime:
    return datetime.now(TZ)


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m %H:%M")


def deep_link(payload: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={payload}"


async def safe_dm(user_id: int, text: str, kb: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(user_id, text, reply_markup=kb)
        return True
    except Exception:
        return False


async def notify_admins(text: str):
    for admin_id in ADMIN_IDS:
        await safe_dm(admin_id, text)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# =========================
# channel UI
# =========================
def kb_channel(item_id: int) -> InlineKeyboardMarkup:
    # “Оформить” всегда заметно + ведёт в личку
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛒 В корзину", callback_data=f"add:{item_id}"),
            InlineKeyboardButton(text="✅ Оформить в боте", url=deep_link("cart")),
        ],
    ])


def status_line(item_row: sqlite3.Row) -> str:
    if item_row["status"] == "free":
        return "🟢 Свободно"
    if item_row["status"] == "sold":
        return "🔴 Продано"
    if item_row["status"] == "reserved":
        ru = item_row["reserved_until_iso"]
        if ru:
            try:
                dt = datetime.fromisoformat(ru)
                return f"🟡 Забронировано до {fmt_dt(dt)}"
            except Exception:
                return "🟡 Забронировано"
        return "🟡 Забронировано"
    return item_row["status"]


def build_caption(item_row: sqlite3.Row) -> str:
    hint = f"ℹ️ Если бот не отвечает — пишите {ARRIVAL_CONTACT}"
    return (
        f"{status_line(item_row)}\n\n"
        f"🧾 {item_row['title']}\n"
        f"{item_row['description']}\n\n"
        f"💶 Цена: {item_row['price']}\n\n"
        f"ID: {item_row['id']}\n\n"
        f"{hint}"
    )


async def upsert_channel_post(item_id: int):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not it:
            return
        msg_id = it["channel_message_id"]
        caption = build_caption(it)
        photo_id = it["photo_file_id"]

    if not msg_id:
        # create
        if photo_id:
            msg = await bot.send_photo(
                chat_id=CHANNEL_USERNAME,
                photo=photo_id,
                caption=caption,
                reply_markup=kb_channel(item_id)
            )
        else:
            msg = await bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=caption,
                reply_markup=kb_channel(item_id)
            )
        with db() as conn:
            conn.execute("UPDATE items SET channel_message_id=? WHERE id=?", (msg.message_id, item_id))
            conn.commit()
        return

    # edit
    try:
        if photo_id:
            await bot.edit_message_caption(
                chat_id=CHANNEL_USERNAME,
                message_id=msg_id,
                caption=caption,
                reply_markup=kb_channel(item_id)
            )
        else:
            await bot.edit_message_text(
                chat_id=CHANNEL_USERNAME,
                message_id=msg_id,
                text=caption,
                reply_markup=kb_channel(item_id)
            )
    except Exception:
        # message deleted/old -> recreate
        with db() as conn:
            conn.execute("UPDATE items SET channel_message_id=NULL WHERE id=?", (item_id,))
            conn.commit()
        await upsert_channel_post(item_id)


# =========================
# cart / booking logic
# =========================
def get_cart_items(user_id: int) -> list[sqlite3.Row]:
    with db() as conn:
        rows = conn.execute("""
            SELECT i.* FROM cart c
            JOIN items i ON i.id = c.item_id
            WHERE c.user_id=?
            ORDER BY i.id
        """, (user_id,)).fetchall()
    return list(rows)


def cart_add(user_id: int, item_id: int) -> tuple[bool, str]:
    with db() as conn:
        it = conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
        if not it:
            return False, "Товар не найден."
        if it["status"] != "free":
            return False, "Товар уже недоступен."
        conn.execute("INSERT OR IGNORE INTO cart(user_id,item_id) VALUES(?,?)", (user_id, item_id))
        conn.commit()
    return True, "Добавлено."


def cart_clear(user_id: int):
    with db() as conn:
        conn.execute("DELETE FROM cart WHERE user_id=?", (user_id,))
        conn.commit()


def cart_clean_unavailable(user_id: int) -> int:
    # remove non-free items from cart
    with db() as conn:
        rows = conn.execute("""
            SELECT c.item_id, i.status FROM cart c
            JOIN items i ON i.id=c.item_id
            WHERE c.user_id=?
        """, (user_id,)).fetchall()
        removed = 0
        for r in rows:
            if r["status"] != "free":
                conn.execute("DELETE FROM cart WHERE user_id=? AND item_id=?", (user_id, r["item_id"]))
                removed += 1
        conn.commit()
    return removed


def reserve_cart(user_id: int, pickup_dt: datetime) -> tuple[bool, str, list[int]]:
    items = get_cart_items(user_id)
    if not items:
        return False, "Корзина пустая.", []

    # check availability
    for it in items:
        if it["status"] != "free":
            return False, f"Товар ID {it['id']} уже недоступен.", []

    expires = now() + timedelta(hours=24)
    item_ids = [int(it["id"]) for it in items]

    with db() as conn:
        for iid in item_ids:
            conn.execute("""
                UPDATE items
                SET status='reserved', reserved_until_iso=?, reserved_by=?
                WHERE id=? AND status='free'
            """, (expires.isoformat(), user_id, iid))
        conn.execute("""
            INSERT INTO reservations(user_id, item_ids, pickup_iso, created_iso, expires_iso)
            VALUES(?,?,?,?,?)
        """, (
            user_id,
            ",".join(map(str, item_ids)),
            pickup_dt.isoformat(),
            now().isoformat(),
            expires.isoformat(),
        ))
        conn.execute("DELETE FROM cart WHERE user_id=?", (user_id,))
        conn.commit()

    return True, expires.isoformat(), item_ids


# =========================
# keyboards for private flow
# =========================
def kb_cart() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Выбрать дату", callback_data="pick:day")],
        [InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="cart:clear")],
    ])


def kb_days() -> InlineKeyboardMarkup:
    today = now().date()
    rows = []
    for i in range(5):
        d = today + timedelta(days=i)
        rows.append([InlineKeyboardButton(text=d.strftime("%Y-%m-%d"), callback_data=f"pick:day:{d.strftime('%Y-%m-%d')}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в корзину", callback_data="cart:show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_times(day_str: str) -> InlineKeyboardMarkup:
    day = datetime.strptime(day_str, "%Y-%m-%d").date()
    start = datetime.combine(day, dtime(9, 0))
    end = datetime.combine(day, dtime(20, 30))
    btns = []
    t = start
    while t <= end:
        btns.append(InlineKeyboardButton(text=t.strftime("%H:%M"), callback_data=f"pick:time:{day_str}:{t.strftime('%H:%M')}"))
        t += timedelta(minutes=30)
    rows = [btns[i:i+4] for i in range(0, len(btns), 4)]
    rows.append([InlineKeyboardButton(text="⬅️ Назад к дням", callback_data="pick:day")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_confirm(day_str: str, time_str: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить бронь", callback_data=f"book:confirm:{day_str}:{time_str}"),
            InlineKeyboardButton(text="Отмена", callback_data="cart:show"),
        ]
    ])


# =========================
# commands
# =========================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    if m.chat.type != "private":
        return

    payload = (m.text.split(maxsplit=1)[1].strip() if len(m.text.split()) > 1 else "")
    await m.answer(
        "Привет 👋\n"
        "Добавляйте товары в корзину из канала и оформляйте бронь здесь.\n\n"
        "Команды:\n"
        "/cart — корзина\n"
        "/add — добавить товар (админ)\n\n"
        f"ℹ️ {SUPPORT_TEXT}"
    )

    if payload == "cart":
        await show_cart(m.from_user.id, m)


@dp.message(Command("cart"))
async def cmd_cart(m: Message):
    if m.chat.type != "private":
        return
    await show_cart(m.from_user.id, m)


async def show_cart(user_id: int, m: Message | None = None):
    removed = cart_clean_unavailable(user_id)
    items = get_cart_items(user_id)

    if not items:
        text = "🧺 Корзина пустая.\n\nВернитесь в канал и нажмите 🛒 «В корзину» под товаром."
        if removed:
            text += "\n\n⚠️ Часть товаров стала недоступна и была убрана из корзины."
        text += f"\n\nℹ️ {SUPPORT_TEXT}"
        if m:
            await m.answer(text)
        else:
            await bot.send_message(user_id, text)
        return

    lines = ["🧺 Ваша корзина:\n"]
    for it in items:
        lines.append(f"• {it['title']} (ID {it['id']}) — {it['price']}")
    if removed:
        lines.append("\n⚠️ Некоторые товары стали недоступны и были убраны из корзины.")
    lines.append(f"\n📍 {PICKUP_LABEL}")
    lines.append(f"\nℹ️ {SUPPORT_TEXT}")
    text = "\n".join(lines)

    if m:
        await m.answer(text, reply_markup=kb_cart())
    else:
        await bot.send_message(user_id, text, reply_markup=kb_cart())


# =========================
# admin: add item
# =========================
ADD_FLOW = {}  # user_id -> dict


@dp.message(Command("add"))
async def cmd_add(m: Message):
    if m.chat.type != "private":
        return
    if not is_admin(m.from_user.id):
        await m.answer("⛔️ Команда доступна только админам.")
        return

    ADD_FLOW[m.from_user.id] = {"step": "photo"}
    await m.answer(
        "Добавление товара:\n"
        "1/4: пришлите фото товара (как фото).\n"
        "Если без фото — отправьте текст: -"
    )


@dp.message(F.photo)
async def add_photo(m: Message):
    if m.chat.type != "private":
        return
    st = ADD_FLOW.get(m.from_user.id)
    if not st or st.get("step") != "photo":
        return

    st["photo_file_id"] = m.photo[-1].file_id
    st["step"] = "title"
    await m.answer("2/4: название товара?")


@dp.message(F.text)
async def add_text_steps(m: Message):
    if m.chat.type != "private":
        return

    st = ADD_FLOW.get(m.from_user.id)
    if not st:
        return

    step = st.get("step")

    if step == "photo":
        if m.text.strip() != "-":
            await m.answer("Нужно фото или '-' (без фото).")
            return
        st["photo_file_id"] = None
        st["step"] = "title"
        await m.answer("2/4: название товара?")
        return

    if step == "title":
        st["title"] = m.text.strip()
        st["step"] = "desc"
        await m.answer("3/4: описание (1-3 строки)?")
        return

    if step == "desc":
        st["description"] = m.text.strip()
        st["step"] = "price"
        await m.answer("4/4: цена (например: 20€)?")
        return

    if step == "price":
        st["price"] = m.text.strip()

        with db() as conn:
            conn.execute("""
                INSERT INTO items(title, description, price, photo_file_id, status)
                VALUES(?,?,?,?, 'free')
            """, (st["title"], st["description"], st["price"], st.get("photo_file_id")))
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.commit()

        ADD_FLOW.pop(m.from_user.id, None)
        await m.answer(f"✅ Товар добавлен (ID {item_id}). Публикую в канал…")
        await upsert_channel_post(item_id)
        return


# =========================
# callbacks
# =========================
@dp.callback_query(F.data.startswith("add:"))
async def cb_add(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    user_id = cb.from_user.id
    ok, msg = cart_add(user_id, item_id)

    # if pressed in channel/group: never spam channel, only alert + DM
    if cb.message and cb.message.chat.type in ("channel", "group", "supergroup"):
        if ok:
            await cb.answer(
                "✅ Добавлено в корзину.\n"
                "Дальше нажмите «✅ Оформить в боте» под товаром.\n"
                f"{SUPPORT_TEXT}",
                show_alert=True
            )
        else:
            await cb.answer(f"⚠️ {msg}\n{SUPPORT_TEXT}", show_alert=True)

        dm_ok = await safe_dm(
            user_id,
            "🧺 Товар добавлен в корзину.\n"
            "Оформление (дата/время) — здесь, в личке.\n\n"
            "Откройте корзину: /cart\n\n"
            f"ℹ️ {SUPPORT_TEXT}"
        )
        if not dm_ok:
            await cb.answer(
                "Откройте бота по кнопке «✅ Оформить в боте» и нажмите START.\n"
                f"{SUPPORT_TEXT}",
                show_alert=True
            )
        return

    # private press
    await cb.answer("✅ Добавлено" if ok else f"⚠️ {msg}", show_alert=True)
    await show_cart(user_id)


@dp.callback_query(F.data == "cart:show")
async def cb_cart_show(cb: CallbackQuery):
    if not cb.message or cb.message.chat.type != "private":
        await cb.answer()
        return
    await cb.answer()
    await show_cart(cb.from_user.id)


@dp.callback_query(F.data == "cart:clear")
async def cb_cart_clear(cb: CallbackQuery):
    if not cb.message or cb.message.chat.type != "private":
        await cb.answer()
        return
    cart_clear(cb.from_user.id)
    await cb.answer("Ок")
    await cb.message.edit_text(f"🧺 Корзина очищена.\n\nℹ️ {SUPPORT_TEXT}")


# ===== booking flow (private only)
@dp.callback_query(F.data == "pick:day")
async def cb_pick_day_root(cb: CallbackQuery):
    if not cb.message or cb.message.chat.type != "private":
        await cb.answer()
        return
    await cb.answer()
    # ВАЖНО: label ДО выбора дня (как ты просила)
    text = f"📍 {PICKUP_LABEL}\n\nВыберите день самовывоза:\n\nℹ️ {SUPPORT_TEXT}"
    await cb.message.edit_text(text, reply_markup=kb_days())


@dp.callback_query(F.data.startswith("pick:day:"))
async def cb_pick_day(cb: CallbackQuery):
    if not cb.message or cb.message.chat.type != "private":
        await cb.answer()
        return
    await cb.answer()
    day_str = cb.data.split(":")[2]
    text = f"📍 {PICKUP_LABEL}\n\nВыберите время на {day_str}:"
    await cb.message.edit_text(text, reply_markup=kb_times(day_str))


@dp.callback_query(F.data.startswith("pick:time:"))
async def cb_pick_time(cb: CallbackQuery):
    if not cb.message or cb.message.chat.type != "private":
        await cb.answer()
        return
    await cb.answer()
    _, _, day_str, time_str = cb.data.split(":")
    items = get_cart_items(cb.from_user.id)
    if not items:
        await cb.message.edit_text(f"🧺 Корзина пустая.\n\nℹ️ {SUPPORT_TEXT}")
        return

    pickup_dt = datetime.strptime(f"{day_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    text = (
        f"📍 {PICKUP_LABEL}\n\n"
        f"Подтвердите бронь:\n"
        f"🕒 Самовывоз: {fmt_dt(pickup_dt)}\n"
        f"📦 Товаров: {len(items)}\n\n"
        "Бронь будет на 24 часа."
    )
    await cb.message.edit_text(text, reply_markup=kb_confirm(day_str, time_str))


@dp.callback_query(F.data.startswith("book:confirm:"))
async def cb_book_confirm(cb: CallbackQuery):
    if not cb.message or cb.message.chat.type != "private":
        await cb.answer()
        return
    await cb.answer()
    _, _, day_str, time_str = cb.data.split(":")
    pickup_dt = datetime.strptime(f"{day_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)

    ok, expires_iso, item_ids = reserve_cart(cb.from_user.id, pickup_dt)
    if not ok:
        await cb.message.edit_text(f"⚠️ Не получилось оформить бронь: {expires_iso}\n\nПопробуйте заново.\n\nℹ️ {SUPPORT_TEXT}")
        return

    # update channel posts
    for iid in item_ids:
        try:
            await upsert_channel_post(iid)
        except Exception:
            pass

    # notify admins ONLY after confirm
    username = f"@{cb.from_user.username}" if cb.from_user.username else ""
    buyer = (username + " " + (cb.from_user.full_name or "")).strip() or f"id:{cb.from_user.id}"

    # build items list for admin
    with db() as conn:
        rows = conn.execute(
            f"SELECT id,title,price FROM items WHERE id IN ({','.join(['?']*len(item_ids))})",
            item_ids
        ).fetchall()
    items_text = "\n".join([f"• {r['title']} (ID {r['id']}) — {r['price']}" for r in rows])

    expires_dt = datetime.fromisoformat(expires_iso)
    admin_msg = (
        "✅ Оформлена бронь\n\n"
        f"👤 Покупатель: {buyer}\n"
        f"🕒 Самовывоз: {PICKUP_LABEL} — {fmt_dt(pickup_dt)}\n"
        f"⏳ Бронь до: {fmt_dt(expires_dt)}\n\n"
        f"{items_text}"
    )
    await notify_admins(admin_msg)

    # buyer message with FULL ADDRESS (как ты просила)
    buyer_msg = (
        "✅ Готово! Бронь оформлена.\n\n"
        f"📍 {PICKUP_LABEL}\n"
        f"🕒 Самовывоз: {fmt_dt(pickup_dt)}\n"
        f"⏳ Бронь действует до: {fmt_dt(expires_dt)}\n\n"
        f"📍 Адрес: {PICKUP_ADDRESS}\n"
        f"Когда подъедете — напишите: {ARRIVAL_CONTACT}\n"
        "Пожалуйста, не звоните в домофон, пишите в Telegram.\n\n"
        f"ℹ️ {SUPPORT_TEXT}"
    )
    await cb.message.edit_text(buyer_msg)


# =========================
# expiry safety loop (releases items after expires)
# =========================
async def expiry_loop():
    while True:
        try:
            current = now()
            expired_res = []
            with db() as conn:
                rows = conn.execute("SELECT id, item_ids, expires_iso FROM reservations").fetchall()
                for r in rows:
                    try:
                        exp = datetime.fromisoformat(r["expires_iso"])
                        if exp <= current:
                            expired_res.append((r["id"], r["item_ids"]))
                    except Exception:
                        continue

                for rid, item_ids_csv in expired_res:
                    ids = [int(x) for x in item_ids_csv.split(",") if x]
                    for iid in ids:
                        conn.execute("""
                            UPDATE items
                            SET status='free', reserved_until_iso=NULL, reserved_by=NULL
                            WHERE id=? AND status='reserved'
                        """, (iid,))
                    conn.execute("DELETE FROM reservations WHERE id=?", (rid,))
                conn.commit()

            # update channel posts after releasing
            for _, item_ids_csv in expired_res:
                for iid in [int(x) for x in item_ids_csv.split(",") if x]:
                    try:
                        await upsert_channel_post(iid)
                    except Exception:
                        pass
        except Exception:
            pass

        await asyncio.sleep(30)


# =========================
# main
# =========================
async def main():
    init_db()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    asyncio.create_task(expiry_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
