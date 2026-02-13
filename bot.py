import sys
import traceback
import csv
import io

print("=== BOOT: bot.py started ===", flush=True)

def _excepthook(exc_type, exc, tb):
    traceback.print_exception(exc_type, exc, tb)
    sys.stdout.flush()
    sys.stderr.flush()

sys.excepthook = _excepthook
import os
import re
import sqlite3
import asyncio
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InputMediaPhoto


# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WAIT_PHOTO_ID = set()
WAIT_CSV_IMPORT = set()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()  # e.g. "@bestgaragesale"
SUPPORT_TEXT = os.getenv("SUPPORT_TEXT", "Если бот лагает — @liusene").strip()
TIMEZONE = os.getenv("TIMEZONE", "Europe/Belgrade").strip()

print("=== ADMIN_CHAT_ID ===", repr(ADMIN_CHAT_ID), flush=True)
print("=== ADMIN_IDS_RAW ===", repr(ADMIN_IDS_RAW), flush=True)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty")

ADMIN_IDS = set()
for x in re.split(r"[,\s]+", ADMIN_IDS_RAW):
    x = x.strip()
    if x.isdigit():
        ADMIN_IDS.add(int(x))

print("=== ADMIN_IDS ===", ADMIN_IDS, flush=True)

TZ = ZoneInfo(TIMEZONE)

ADMIN_IDS = set()
for x in re.split(r"[,\s]+", ADMIN_IDS_RAW):
    x = x.strip()
    if x.isdigit():
        ADMIN_IDS.add(int(x))

TZ = ZoneInfo(TIMEZONE)


# =========================
# DB
# =========================
DB_PATH = "sale.db"


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                price TEXT,
                photo_id TEXT,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS carts (
                user_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                added_at TEXT NOT NULL,
                UNIQUE(user_id, item_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                day TEXT NOT NULL,
                slot TEXT NOT NULL,
                address TEXT,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reservation_items (
                reservation_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                UNIQUE(reservation_id, item_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_res_expires ON reservations(expires_at)")
        conn.commit()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                photo_id TEXT NOT NULL,
                pos INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(item_id, photo_id)
            )
            """
)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_item_photos_item ON item_photos(item_id)")


def now_local() -> datetime:
    return datetime.now(tz=TZ)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def cleanup_expired() -> int:
    n = 0
    with db() as conn:
        rows = conn.execute(
            "SELECT id, expires_at, status FROM reservations WHERE status='CONFIRMED'"
        ).fetchall()
        for r in rows:
            exp = parse_iso(r["expires_at"])
            if now_local() >= exp:
                conn.execute(
                    "UPDATE reservations SET status='EXPIRED' WHERE id=?",
                    (r["id"],),
                )
                n += 1
        conn.commit()
    return n


def item_is_reserved(item_id: int) -> bool:
    cleanup_expired()
    with db() as conn:
        r = conn.execute(
            """
            SELECT 1
            FROM reservation_items ri
            JOIN reservations r ON r.id = ri.reservation_id
            WHERE ri.item_id = ? AND r.status = 'CONFIRMED'
            """,
            (item_id,),
        ).fetchone()
        return r is not None


def get_item(item_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def add_item_to_db(title: str, price: str, photo_id: Optional[str]) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO items (title, price, photo_id, created_at, is_active) VALUES (?, ?, ?, ?, 1)",
            (title, price, photo_id, iso(now_local())),
        )
        conn.commit()
        return int(cur.lastrowid)

def add_item_photo(item_id: int, photo_id: str, pos: int = 0) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO item_photos (item_id, photo_id, pos, created_at) VALUES (?, ?, ?, ?)",
            (item_id, photo_id, pos, iso(now_local())),
        )
        conn.commit()

def get_item_photos(item_id: int) -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT photo_id FROM item_photos WHERE item_id=? ORDER BY pos, id",
            (item_id,),
        ).fetchall()
    return [r["photo_id"] for r in rows]

def parse_csv_items(content: str) -> list[tuple[str, str, str | None]]:
    """
    Возвращает список (title, price, photo_id)
    """
    f = io.StringIO(content)
    reader = csv.DictReader(f)
    out = []
    for row in reader:
        title = (row.get("title") or "").strip()
        price = (row.get("price") or "").strip()
        photo = (row.get("photo") or "").strip() or None
        if not title:
            continue
        out.append((title, price, photo))
    return out

def cart_add(user_id: int, item_id: int) -> bool:
    if item_is_reserved(item_id):
        return False
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO carts (user_id, item_id, added_at) VALUES (?, ?, ?)",
            (user_id, item_id, iso(now_local())),
        )
        conn.commit()
        return True
        

def cart_remove(user_id: int, item_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM carts WHERE user_id=? AND item_id=?", (user_id, item_id))
        conn.commit()


def cart_clear(user_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
        conn.commit()


def cart_list(user_id: int) -> List[sqlite3.Row]:
    cleanup_expired()
    with db() as conn:
        return conn.execute(
            """
            SELECT i.*
            FROM carts c
            JOIN items i ON i.id = c.item_id
            WHERE c.user_id = ? AND i.is_active = 1
            ORDER BY c.added_at ASC
            """,
            (user_id,),
        ).fetchall()


def create_reservation(user_id: int, day_str: str, slot_str: str, item_ids: List[int]) -> int:
    cleanup_expired()
    created = now_local()
    slot_dt = datetime.fromisoformat(f"{day_str}T{slot_str}:00").replace(tzinfo=TZ)
    expires = max(created + timedelta(hours=24), slot_dt)
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO reservations (user_id, created_at, expires_at, day, slot, address, status)
            VALUES (?, ?, ?, ?, ?, NULL, 'CONFIRMED')
            """,
            (user_id, iso(created), iso(expires), day_str, slot_str),
        )
        res_id = int(cur.lastrowid)
        for it in item_ids:
            conn.execute(
                "INSERT OR IGNORE INTO reservation_items (reservation_id, item_id) VALUES (?, ?)",
                (res_id, it),
            )
        conn.commit()
        return res_id


def set_reservation_address(res_id: int, address: str) -> None:
    with db() as conn:
        conn.execute("UPDATE reservations SET address=? WHERE id=?", (address, res_id))
        conn.commit()


def get_reservation(res_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM reservations WHERE id=?", (res_id,)).fetchone()


def reservation_items(res_id: int) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            SELECT i.*
            FROM reservation_items ri
            JOIN items i ON i.id = ri.item_id
            WHERE ri.reservation_id = ?
            """,
            (res_id,),
        ).fetchall()


# =========================
# UI helpers
# =========================
def kb_item(item_id: int, bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛒 В корзину", callback_data=f"cart_add:{item_id}"),
                InlineKeyboardButton(
                    text="✅ Оформить в боте",
                    url=f"https://t.me/{bot_username}?start=checkout"
                ),
            ]
        ]
    )


def kb_cart(items: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        rows.append(
            [InlineKeyboardButton(text=f"❌ Убрать: #{it['id']}", callback_data=f"cart_remove:{it['id']}")]
        )
    rows.append([InlineKeyboardButton(text="✅ Оформить", callback_data="checkout_start")])
    rows.append([InlineKeyboardButton(text="🧹 Очистить корзину", callback_data="cart_clear")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_days() -> InlineKeyboardMarkup:
    today = now_local().date()
    buttons = []
    for i in range(5):
        d = today + timedelta(days=i)
        label = d.strftime("%a %d.%m")
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"pick_day:{d.isoformat()}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад к корзине", callback_data="cart_show")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def generate_slots() -> List[str]:
    slots = []
    t = datetime.combine(date.today(), time(9, 0))
    end = datetime.combine(date.today(), time(21, 0))
    while t <= end:
        slots.append(t.strftime("%H:%M"))
        t += timedelta(minutes=30)
    return slots


def kb_slots(day_str: str) -> InlineKeyboardMarkup:
    slots = generate_slots()
    rows = []
    for s in slots:
        rows.append([InlineKeyboardButton(text=s, callback_data=f"pick_slot:{day_str}:{s}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к дням", callback_data="checkout_start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_confirm(day_str: str, slot_str: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm:{day_str}:{slot_str}")],
            [InlineKeyboardButton(text="⬅️ Назад к слотам", callback_data=f"pick_day:{day_str}")],
        ]
    )


def kb_checkout_from_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Оформить", callback_data="checkout_start")]]
    )


# =========================
# FSM
# =========================
class AddItemFlow(StatesGroup):
    waiting_photo = State()


class AddressFlow(StatesGroup):
    waiting_address = State()


# =========================
# Bot
# =========================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


async def background_cleanup():
    while True:
        try:
            cleanup_expired()
        except Exception:
            pass
        await asyncio.sleep(60)


def parse_title_price(txt: str) -> Tuple[Optional[str], str]:
    txt = (txt or "").strip()
    if not txt:
        return None, ""
    if "|" in txt:
        t, p = [x.strip() for x in txt.split("|", 1)]
        return (t or None), (p or "")
    return (txt.strip() or None), ""

async def publish_item_to_channel(item_id: int, title: str, price: str, photo_id: Optional[str]):
    print("=== POST TO CHANNEL: START ===", flush=True)
    print("CHANNEL_USERNAME =", CHANNEL_USERNAME, flush=True)
    print("item_id =", item_id, "has_photo =", bool(photo_id), flush=True)

    try:
        me = await bot.get_me()
        bot_username = me.username or ""

        post_text = f"🛍 {title}"
        if price:
            post_text += f"\n💰 {price}"
        post_text += f"\n\nℹ️ {SUPPORT_TEXT}"

        kb = kb_item(item_id, bot_username)

        # ---- MULTI PHOTO BLOCK ----
        photos = get_item_photos(item_id)

        if not photos and photo_id:
            photos = [photo_id]

        if photos:
            media = [InputMediaPhoto(media=pid) for pid in photos[:10]]
            await bot.send_media_group(
                chat_id=CHANNEL_USERNAME,
                media=media
            )

            await bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=post_text,
                reply_markup=kb
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=post_text,
                reply_markup=kb
            )

        print("=== POST TO CHANNEL: OK ===", flush=True)

    except Exception as e:
        import traceback
        print("=== POST TO CHANNEL: FAIL ===", flush=True)
        traceback.print_exc()
        raise

# =========================
# Commands
# =========================
@dp.message(CommandStart())
async def cmd_start(m: Message):
    # поддерживаем deep link: /start checkout
    payload = ""
    if m.text:
        parts = m.text.split(maxsplit=1)
        if len(parts) == 2:
            payload = parts[1].strip()

    if payload == "checkout":
        await show_cart(m.from_user.id, m)
        items = cart_list(m.from_user.id)
        if items:
            await m.answer("Нажми, чтобы оформить:", reply_markup=kb_checkout_from_start())
        return

    await m.answer(
        "Привет! Тут корзина и бронирование товаров.\n\n"
        "Добавляй товары из канала кнопкой 🛒\n"
        "Открыть корзину: /cart\n\n"
        f"{SUPPORT_TEXT}"
    )

@dp.message(Command("photoid"))
async def cmd_photoid(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("⛔️ Только для админов.")
        return

    WAIT_PHOTO_ID.add(m.from_user.id)
    await m.answer("Ок. Пришли ОДНО фото (как фото, не документом). Я пришлю photo_id.")
@dp.message(Command("csv"))
async def cmd_csv(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("⛔️ Только для админов.")
        return

    WAIT_CSV_IMPORT.add(m.from_user.id)
    await m.answer(
        "Ок. Пришли CSV ФАЙЛОМ (как document).\n\n"
        "Колонки: title, price, photo\n"
        "photo — это photo_id (можно пусто)."
    )

@dp.message(F.document)
async def on_csv_document(m: Message):
    # реагируем только если админ и только после /csv
    if m.from_user.id not in ADMIN_IDS:
        return
    if m.from_user.id not in WAIT_CSV_IMPORT:
        return

    doc = m.document
    filename = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()

    if not (filename.endswith(".csv") or mime == "text/csv"):
        await m.answer("Это не CSV. Пришли файл с расширением .csv")
        return

    WAIT_CSV_IMPORT.discard(m.from_user.id)

    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    content = buf.getvalue().decode("utf-8-sig", errors="ignore")

    items = parse_csv_items(content)
    if not items:
        await m.answer("В файле не нашла ни одной строки с title. Проверь заголовки: title,price,photo")
        return

    ok = 0
    fail = 0

    for title, price, photo_id in items:
        try:
            item_id = add_item_to_db(title=title, price=price, photo_id=photo_id)
            await publish_item_to_channel(item_id=item_id, title=title, price=price, photo_id=photo_id)
            ok += 1
        except Exception:
            fail += 1

    await m.answer(f"Готово. Добавлено: {ok}. Ошибок: {fail}.")

@dp.message(Command("cart"))
async def cmd_cart(m: Message):
    await show_cart(m.from_user.id, m)

@dp.message(F.photo)
async def on_photo_get_id(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return

    if m.from_user.id not in WAIT_PHOTO_ID:
        return

    WAIT_PHOTO_ID.discard(m.from_user.id)

    photo_id = m.photo[-1].file_id  # самое качественное фото
    await m.answer(f"photo_id:\n{photo_id}\n\nСкопируй и вставь в колонку photo в CSV.")

@dp.message(Command("add"))
async def cmd_add(m: Message, state: FSMContext):
    if not m.from_user or m.from_user.id not in ADMIN_IDS:
        await m.answer("⛔ Только для админов.")
        return

    await state.clear()
    await m.answer(
        "Ок. Пришли ОДНИМ сообщением: фото + подпись в формате:\n"
        "<название> | <цена>\n\n"
        "Пример:\n"
        "Футболка белая | 2500 RSD\n\n"
        "Если отправишь сначала текст — я попрошу фото."
    )
    await state.set_state(AddItemFlow.waiting_photo)

@dp.message(AddItemFlow.waiting_photo)
async def add_item_flow(m: Message, state: FSMContext):
    if not m.from_user or m.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    # 1) забираем текст из caption/text
    txt = (m.caption or m.text or "").strip()

    # 2) если пришёл текст без фото — запомним и попросим фото
    if not m.photo:
        title, price = parse_title_price(txt)
        if not title:
            await m.answer("Пусто. Пришли: <название> | <цена> (лучше сразу с фото).")
            return
        await state.update_data(title=title, price=price)
        await m.answer("Ок, теперь пришли фото этого товара (можно без текста).")
        return

    # 3) если пришло фото — берём title/price из подписи, либо из state
    title, price = parse_title_price(txt)
    if not title:
        data = await state.get_data()
        title = data.get("title")
        price = data.get("price", "")

    if not title:
        await m.answer("К фото добавь подпись: <название> | <цена> (или сначала пришли текст, потом фото).")
        return

    photo_id = m.photo[-1].file_id
    item_id = add_item_to_db(title=title, price=price, photo_id=photo_id)

    try:
        await publish_item_to_channel(item_id, title, price, photo_id)
        await m.answer(f"✅ Опубликовано в канал. ID товара: #{item_id}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        await m.answer(f"❌ Не смог отправить в канал. Ошибка: {type(e).__name__}: {e}")
        
@dp.message(Command("import"))
async def cmd_import(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("⛔️ Только для админов.")
        return
        await m.answer("Ок. Теперь пришли CSV-файл (items.csv) документом.")

# =========================
# Cart & Checkout
# =========================
async def show_cart(user_id: int, target):
    items = cart_list(user_id)
    if not items:
        await target.answer("Корзина пуста. Добавляй товары из канала кнопкой 🛒")
        return

    lines = ["🛒 Твоя корзина:"]
    for it in items:
        reserved = " (уже забронировано кем-то)" if item_is_reserved(int(it["id"])) else ""
        price = it["price"] or ""
        lines.append(f"• #{it['id']} — {it['title']} {('— ' + price) if price else ''}{reserved}")

    await target.answer("\n".join(lines), reply_markup=kb_cart(items))


@dp.callback_query(F.data == "cart_show")
async def cb_cart_show(c: CallbackQuery):
    await c.answer()
    await show_cart(c.from_user.id, c.message)


@dp.callback_query(F.data.startswith("cart_add:"))
async def cb_cart_add(c: CallbackQuery):
    await c.answer()
    item_id = int(c.data.split(":", 1)[1])
    it = get_item(item_id)
    if not it or int(it["is_active"]) != 1:
        await c.message.answer("Этот товар недоступен.")
        return
    if item_is_reserved(item_id):
        await c.message.answer("Этот товар уже забронирован кем-то.")
        return
    ok = cart_add(c.from_user.id, item_id)
    if ok:
        await c.message.answer("✅ Добавлено в корзину. Открой /cart чтобы оформить.")
    else:
        await c.message.answer("Не смог добавить (возможно уже в корзине).")


@dp.callback_query(F.data.startswith("cart_remove:"))
async def cb_cart_remove(c: CallbackQuery):
    await c.answer()
    item_id = int(c.data.split(":", 1)[1])
    cart_remove(c.from_user.id, item_id)
    await show_cart(c.from_user.id, c.message)


@dp.callback_query(F.data == "cart_clear")
async def cb_cart_clear(c: CallbackQuery):
    await c.answer()
    cart_clear(c.from_user.id)
    await c.message.answer("🧹 Корзина очищена.")
    await notify_admin_reservation(
        res_id,
        c.from_user.id,
        day_str,
        slot_str,
        exp_str,
        items
)



@dp.callback_query(F.data == "checkout_start")
async def cb_checkout_start(c: CallbackQuery, state: FSMContext):
    await c.answer()
    items = cart_list(c.from_user.id)
    if not items:
        await c.message.answer("Корзина пуста. Добавляй товары из канала 🛒")
        return

    bad = [it for it in items if item_is_reserved(int(it["id"]))]
    if bad:
        await c.message.answer("Некоторые товары уже забронированы кем-то. Удали их из корзины и попробуй снова.")
        await show_cart(c.from_user.id, c.message)
        return

    await state.clear()
    await c.message.answer(
    "Самовывоз из Belgrade Waterfront\n\nВыбери день (следующие 5 дней):",
    reply_markup=kb_days()
)


@dp.callback_query(F.data.startswith("pick_day:"))
async def cb_pick_day(c: CallbackQuery):
    await c.answer()
    day_str = c.data.split(":", 1)[1]
    await c.message.answer(f"Выбери время на {day_str}:", reply_markup=kb_slots(day_str))


@dp.callback_query(F.data.startswith("pick_slot:"))
async def cb_pick_slot(c: CallbackQuery, state: FSMContext):
    await c.answer()
    _, day_str, slot_str = c.data.split(":", 2)

    await state.update_data(day=day_str, time=slot_str)

    await c.message.answer(
        f"Подтверди бронь:\n📅 {day_str}\n🕒 {slot_str}\n\n",
        reply_markup=kb_confirm(day_str, slot_str),
    )
    
@dp.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(c: CallbackQuery, state: FSMContext):
    await c.answer()

    _, day_str, slot_str = c.data.split(":", 2)

    items = cart_list(c.from_user.id)
    if not items:
        await c.message.answer("Корзина пуста. /cart")
        return

    bad = [it for it in items if item_is_reserved(int(it["id"]))]
    if bad:
        await c.message.answer(
            "Упс: кто-то уже забронировал часть товаров. Удали их из корзины и попробуй снова."
        )
        await show_cart(c.from_user.id, c.message)
        return

    item_ids = [int(it["id"]) for it in items]

    # создаём бронь
    res_id = create_reservation(c.from_user.id, day_str, slot_str, item_ids)
    r = get_reservation(res_id)

    # считаем красивое время окончания
    exp_str = parse_iso(r["expires_at"]).astimezone(TZ).strftime("%d.%m.%Y %H:%M")

    # очищаем корзину\
    cart_clear(c.from_user.id)
    
    await notify_admin_reservation(res_id, c.from_user.id, day_str, slot_str, exp_str, items)

    # финальное сообщение — БЕЗ шага "введите адрес"
    await c.message.answer(
        "✅ Бронь подтверждена.\n\n"
        f"📅 {day_str}\n"
        f"🕒 {slot_str}\n"
        f"⏳ Бронь до: {exp_str}\n\n"
        "📍 Самовывоз из Belgrade Waterfront\n"
        "Адрес: BW Sole. Bulevar Vudroa Vilsona, 17\n"
        "Как подъедете, напишите в тг @liusene"
)

    
@dp.message(AddressFlow.waiting_address)
async def address_flow(m: Message, state: FSMContext):
    address = (m.text or "").strip()

    if not address:
        await m.answer("Пришли адрес текстом одним сообщением.")
        return

    data = await state.get_data()
    day = data.get("day")
    time_str = data.get("time")

    tz = ZoneInfo("Europe/Belgrade")
    until = datetime.now(tz) + timedelta(hours=24)

    until_str = until.strftime("%d.%m.%Y %H:%M")

    await m.answer(
        f"✅ Бронь создана\n\n"
        f"📅 {day}\n"
        f"⏰ {time_str}\n\n"
        f"Бронь до: {until_str}\n\n"
        f"📍 Самовывоз из Belgrade Waterfront\n"
        f"Адрес: BW Sole. Bulevar Vudroa Vilsona, 17\n\n"
        f"Как подъедете, напишите в тг @liusene"
    )
async def main():
    init_db()
    print("=== DB INIT DONE ===", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
