import asyncio
import csv
import io
import logging
import os
import re
import sqlite3
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logger = logging.getLogger(__name__)


# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()
TIMEZONE = os.getenv("TIMEZONE", "Europe/Belgrade").strip()
SUPPORT_TEXT = os.getenv("SUPPORT_TEXT", "Если бот лагает — @liusene").strip()
PICKUP_TEXT = os.getenv(
    "PICKUP_TEXT",
    "📍 Самовывоз из Belgrade Waterfront\nАдрес: BW Sole. Bulevar Vudroa Vilsona, 17\nКак подъедете, напишите в тг @liusene",
).strip()
SLOT_START = os.getenv("SLOT_START", "09:00").strip()
SLOT_END = os.getenv("SLOT_END", "21:00").strip()
SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "30"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty")
if not CHANNEL_USERNAME:
    raise RuntimeError("CHANNEL_USERNAME is empty (example: @my_channel)")


def normalize_channel_target(raw_value: str) -> str | int:
    value = (raw_value or "").strip()
    if not value:
        raise RuntimeError("CHANNEL_USERNAME is empty (example: @my_channel)")
    if value.startswith("-100") and value[1:].isdigit():
        return int(value)
    if value.startswith("http://") or value.startswith("https://"):
        value = value.rstrip("/").rsplit("/", 1)[-1].strip()
    if value.startswith("@"):
        value = value[1:]
    value = value.strip()
    if not value:
        raise RuntimeError("CHANNEL_USERNAME must be @channel, channel, t.me/channel or -100...")
    return f"@{value}"

ADMIN_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", ADMIN_IDS_RAW)
    if x.strip().isdigit()
}
ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW) if ADMIN_CHAT_ID_RAW.isdigit() else None
TZ = ZoneInfo(TIMEZONE)
DB_PATH = "sale.db"
CHANNEL_TARGET = normalize_channel_target(CHANNEL_USERNAME)


def get_runtime_revision() -> str:
    env_rev = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or "").strip()
    if env_rev:
        return env_rev[:12]
    try:
        rev = subprocess.check_output(["git", "rev-parse", "--short=12", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
        if rev:
            return rev
    except Exception:
        pass
    return "unknown"


RUNTIME_REVISION = get_runtime_revision()


# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_local() -> datetime:
    return datetime.now(tz=TZ)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                price TEXT,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                channel_message_id INTEGER
            )
            """
        )
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
                status TEXT NOT NULL DEFAULT 'CONFIRMED'
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slot_blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                slot TEXT NOT NULL,
                reason TEXT,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(day, slot)
            )
            """
        )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_item_photos_item ON item_photos(item_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reservations_day_slot ON reservations(day, slot)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reservations_exp ON reservations(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_blocks_day ON slot_blocks(day)")

        item_cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
        if "status" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN status TEXT DEFAULT 'ACTIVE'")
        if "is_active" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN is_active INTEGER DEFAULT 1")
        if "channel_message_id" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN channel_message_id INTEGER")
        if "created_at" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN created_at TEXT")
        if "updated_at" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN updated_at TEXT")

        now_iso = iso(now_local())
        conn.execute("UPDATE items SET status='ACTIVE' WHERE status IS NULL OR trim(status)='' ")
        conn.execute("UPDATE items SET status=upper(status) WHERE status IS NOT NULL")
        conn.execute("UPDATE items SET is_active=1 WHERE is_active IS NULL")
        conn.execute("UPDATE items SET created_at=? WHERE created_at IS NULL OR trim(created_at)=''", (now_iso,))
        conn.execute("UPDATE items SET updated_at=? WHERE updated_at IS NULL OR trim(updated_at)=''", (now_iso,))

        conn.commit()


def item_text(item: sqlite3.Row) -> str:
    lines = [f"🛍 {item['title']}"]

    status = item["status"]
    if status == "RESERVED":
        lines.append("🟡 Забронировано")
    elif status == "SOLD":
        lines.append("🔴 Нет в наличии")

    if item["price"]:
        lines.append(f"💰 {item['price']}")
    if item["description"]:
        lines.append("")
        lines.append(item["description"])
    lines.append("")
    lines.append(f"ℹ️ {SUPPORT_TEXT}")
    return "\n".join(lines)


def parse_title_block(text: str) -> tuple[Optional[str], str, str]:
    parts = [x.strip() for x in (text or "").split("|", 2)]
    if not parts or not parts[0]:
        return None, "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], parts[1], parts[2]


def parse_photos_field(value: str) -> list[str]:
    if not value:
        return []
    # Telegram file_id может приходить в несколько строк/через разные разделители.
    # Нормализуем наиболее частые форматы: | , ; и любые переводы строк.
    chunks = re.split(r"[|,;\n\r]+", value)
    out: list[str] = []
    for chunk in chunks:
        pid = chunk.strip()
        if not pid:
            continue
        # Защита от случайного мусора в поле (например, комментариев в скобках).
        # Берём только первый токен до пробела.
        pid = pid.split()[0]
        out.append(pid)
    return out


def looks_like_photo_id_list(value: str) -> bool:
    """Heuristic: Telegram file_id(s) are long opaque tokens, not natural descriptions."""
    tokens = parse_photos_field(value)
    if not tokens:
        return False
    for tok in tokens:
        if len(tok) < 20:
            return False
        if not re.fullmatch(r"[A-Za-z0-9_-]+", tok):
            return False
    return True


def extract_photo_from_message_context(m: Message) -> tuple[Optional[str], str]:
    """Return best photo file_id from current message or replied message."""
    if m.photo:
        return m.photo[-1].file_id, "message.photo"
    if m.reply_to_message and m.reply_to_message.photo:
        return m.reply_to_message.photo[-1].file_id, "reply.photo"
    return None, "none"


def add_item(title: str, price: str, description: str, photos: list[str]) -> int:
    now = iso(now_local())
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO items (title, price, description, status, created_at, updated_at, is_active)
            VALUES (?, ?, ?, 'ACTIVE', ?, ?, 1)
            """,
            (title, price, description, now, now),
        )
        item_id = int(cur.lastrowid)
        for idx, photo_id in enumerate(photos):
            conn.execute(
                "INSERT OR IGNORE INTO item_photos (item_id, photo_id, pos, created_at) VALUES (?, ?, ?, ?)",
                (item_id, photo_id, idx, now),
            )
        conn.commit()
        return item_id


def find_item_by_fields(title: str, price: str, description: str) -> Optional[int]:
    with db() as conn:
        row = conn.execute(
            """
            SELECT id
            FROM items
            WHERE lower(trim(title)) = lower(trim(?))
              AND lower(trim(coalesce(price, ''))) = lower(trim(?))
              AND lower(trim(coalesce(description, ''))) = lower(trim(?))
            ORDER BY id DESC
            LIMIT 1
            """,
            (title, price or "", description or ""),
        ).fetchone()
        return int(row["id"]) if row else None


def get_item(item_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def get_item_photos(item_id: int) -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT photo_id FROM item_photos WHERE item_id=? ORDER BY pos, id",
            (item_id,),
        ).fetchall()
        return [r["photo_id"] for r in rows]


def item_is_available(item: sqlite3.Row | None) -> bool:
    if not item:
        return False
    status = str(item["status"] or "ACTIVE").strip().upper()
    is_active = 1 if item["is_active"] is None else int(item["is_active"])
    return status == "ACTIVE" and is_active == 1


def add_photo_to_item(item_id: int, photo_id: str) -> None:
    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM item_photos WHERE item_id=?",
            (item_id,),
        ).fetchone()["c"]
        conn.execute(
            "INSERT OR IGNORE INTO item_photos (item_id, photo_id, pos, created_at) VALUES (?, ?, ?, ?)",
            (item_id, photo_id, int(count), iso(now_local())),
        )
        conn.execute(
            "UPDATE items SET updated_at=? WHERE id=?",
            (iso(now_local()), item_id),
        )
        conn.commit()


def update_item(item_id: int, title: str, price: str, description: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE items SET title=?, price=?, description=?, updated_at=? WHERE id=?",
            (title, price, description, iso(now_local()), item_id),
        )
        conn.commit()


def set_item_status(item_id: int, status: str) -> None:
    is_active = 1 if status in {"ACTIVE", "RESERVED"} else 0
    with db() as conn:
        conn.execute(
            "UPDATE items SET status=?, is_active=?, updated_at=? WHERE id=?",
            (status, is_active, iso(now_local()), item_id),
        )
        conn.commit()


def set_item_channel_message(item_id: int, message_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE items SET channel_message_id=?, updated_at=? WHERE id=?",
            (message_id, iso(now_local()), item_id),
        )
        conn.commit()


def clear_item_channel_message(item_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE items SET channel_message_id=NULL, updated_at=? WHERE id=?",
            (iso(now_local()), item_id),
        )
        conn.commit()


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


def cart_list(user_id: int) -> list[sqlite3.Row]:
    cleanup_expired()
    with db() as conn:
        return conn.execute(
            """
            SELECT i.*
            FROM carts c
            JOIN items i ON i.id = c.item_id
            WHERE c.user_id=? AND coalesce(nullif(trim(i.status), ''), 'ACTIVE')='ACTIVE' AND coalesce(i.is_active, 1)=1
            ORDER BY c.added_at ASC
            """,
            (user_id,),
        ).fetchall()


def cleanup_expired() -> int:
    changed = 0
    with db() as conn:
        rows = conn.execute(
            "SELECT id, expires_at FROM reservations WHERE status='CONFIRMED'"
        ).fetchall()
        now = now_local()
        for row in rows:
            if now >= parse_iso(row["expires_at"]):
                conn.execute("UPDATE reservations SET status='EXPIRED' WHERE id=?", (row["id"],))
                changed += 1
        conn.commit()
    return changed


def item_is_reserved(item_id: int) -> bool:
    cleanup_expired()
    with db() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM reservation_items ri
            JOIN reservations r ON r.id = ri.reservation_id
            WHERE ri.item_id=? AND r.status='CONFIRMED'
            LIMIT 1
            """,
            (item_id,),
        ).fetchone()
    return row is not None


def reservation_slot_taken(day_str: str, slot: str) -> bool:
    cleanup_expired()
    with db() as conn:
        reserved = conn.execute(
            "SELECT 1 FROM reservations WHERE day=? AND slot=? AND status='CONFIRMED' LIMIT 1",
            (day_str, slot),
        ).fetchone()
        blocked = conn.execute(
            "SELECT 1 FROM slot_blocks WHERE day=? AND slot=? LIMIT 1",
            (day_str, slot),
        ).fetchone()
    return reserved is not None or blocked is not None


def create_reservation(user_id: int, day_str: str, slot: str, item_ids: list[int]) -> int:
    created = now_local()
    slot_dt = datetime.fromisoformat(f"{day_str}T{slot}:00").replace(tzinfo=TZ)
    expires = max(created + timedelta(hours=24), slot_dt)

    with db() as conn:
        # transactional slot check
        busy = conn.execute(
            "SELECT 1 FROM reservations WHERE day=? AND slot=? AND status='CONFIRMED' LIMIT 1",
            (day_str, slot),
        ).fetchone()
        blocked = conn.execute(
            "SELECT 1 FROM slot_blocks WHERE day=? AND slot=? LIMIT 1",
            (day_str, slot),
        ).fetchone()
        if busy or blocked:
            raise ValueError("slot_busy")

        cur = conn.execute(
            """
            INSERT INTO reservations (user_id, created_at, expires_at, day, slot, status)
            VALUES (?, ?, ?, ?, ?, 'CONFIRMED')
            """,
            (user_id, iso(created), iso(expires), day_str, slot),
        )
        res_id = int(cur.lastrowid)

        for item_id in item_ids:
            conn.execute(
                "INSERT OR IGNORE INTO reservation_items (reservation_id, item_id) VALUES (?, ?)",
                (res_id, item_id),
            )
            conn.execute(
                "UPDATE items SET status='RESERVED', updated_at=? WHERE id=?",
                (iso(now_local()), item_id),
            )

        conn.commit()
    return res_id


def get_reservation(res_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM reservations WHERE id=?", (res_id,)).fetchone()


def reservation_items(res_id: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            SELECT i.*
            FROM reservation_items ri
            JOIN items i ON i.id = ri.item_id
            WHERE ri.reservation_id=?
            ORDER BY i.id
            """,
            (res_id,),
        ).fetchall()


def user_reservations(user_id: int) -> list[sqlite3.Row]:
    cleanup_expired()
    with db() as conn:
        return conn.execute(
            "SELECT * FROM reservations WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (user_id,),
        ).fetchall()


def cancel_reservation(user_id: int, res_id: int) -> bool:
    with db() as conn:
        r = conn.execute(
            "SELECT * FROM reservations WHERE id=? AND user_id=? AND status='CONFIRMED'",
            (res_id, user_id),
        ).fetchone()
        if not r:
            return False
        conn.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?", (res_id,))
        rows = conn.execute(
            "SELECT item_id FROM reservation_items WHERE reservation_id=?",
            (res_id,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE items SET status='ACTIVE', updated_at=? WHERE id=? AND status='RESERVED'",
                (iso(now_local()), row["item_id"]),
            )
        conn.commit()
    return True


def parse_slot_range(start_slot: str, end_slot: Optional[str]) -> list[str]:
    if not end_slot:
        return [start_slot]

    slots = []
    start = datetime.strptime(start_slot, "%H:%M")
    end = datetime.strptime(end_slot, "%H:%M")
    if end < start:
        raise ValueError("end_before_start")
    cur = start
    while cur <= end:
        slots.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=SLOT_STEP_MINUTES)
    return slots


def parse_slot_selection(start_or_list: str, end_slot: Optional[str]) -> list[str]:
    """Parse either range (10:00 12:00) or explicit list (10:00,11:00,13:00)."""
    explicit_list = any(sep in start_or_list for sep in [",", ";"])
    if explicit_list:
        if end_slot is not None:
            raise ValueError("list_and_range_conflict")
        slots = [x.strip() for x in re.split(r"[,;]", start_or_list) if x.strip()]
    else:
        slots = parse_slot_range(start_or_list, end_slot)

    allowed = set(generate_slots())
    uniq: list[str] = []
    for slot in slots:
        if slot not in allowed:
            raise ValueError(f"invalid_slot:{slot}")
        if slot not in uniq:
            uniq.append(slot)
    if not uniq:
        raise ValueError("empty_slots")
    return uniq


def block_slots(day_str: str, slots: list[str], reason: str, created_by: int) -> int:
    cnt = 0
    with db() as conn:
        for slot in slots:
            conn.execute(
                """
                INSERT OR IGNORE INTO slot_blocks (day, slot, reason, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (day_str, slot, reason, created_by, iso(now_local())),
            )
            cnt += conn.total_changes
        conn.commit()
    return cnt


def unblock_slots(day_str: str, slots: list[str]) -> int:
    with db() as conn:
        before = conn.total_changes
        for slot in slots:
            conn.execute("DELETE FROM slot_blocks WHERE day=? AND slot=?", (day_str, slot))
        conn.commit()
        return conn.total_changes - before


def day_blocks(day_str: str) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT day, slot, reason FROM slot_blocks WHERE day=? ORDER BY slot",
            (day_str,),
        ).fetchall()


# =========================
# Bot + FSM
# =========================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class AddFlow(StatesGroup):
    waiting = State()


class EditFlow(StatesGroup):
    waiting = State()


class AddPhotoFlow(StatesGroup):
    waiting_photo = State()


class CsvFlow(StatesGroup):
    waiting_document = State()


@dataclass
class CsvRow:
    title: str
    price: str
    description: str
    photos: list[str]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or (ADMIN_CHAT_ID is not None and user_id == ADMIN_CHAT_ID)


def kb_item(item_id: int, bot_username: str) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="🛒 В корзину", callback_data=f"cart_add:{item_id}")]
    if bot_username:
        row.append(InlineKeyboardButton(text="✅ Оформить в боте", url=f"https://t.me/{bot_username}?start=checkout"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row
        ]
    )


def kb_cart(items: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"❌ Убрать #{it['id']}", callback_data=f"cart_remove:{it['id']}")]
        for it in items
    ]
    rows.append([InlineKeyboardButton(text="✅ Оформить", callback_data="checkout_start")])
    rows.append([InlineKeyboardButton(text="🧹 Очистить корзину", callback_data="cart_clear")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def generate_slots() -> list[str]:
    start_t = datetime.strptime(SLOT_START, "%H:%M").time()
    end_t = datetime.strptime(SLOT_END, "%H:%M").time()

    slots = []
    cur = datetime.combine(date.today(), start_t)
    end_dt = datetime.combine(date.today(), end_t)
    while cur <= end_dt:
        slots.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=SLOT_STEP_MINUTES)
    return slots


def kb_days() -> InlineKeyboardMarkup:
    today = now_local().date()
    rows = []
    for i in range(7):
        d = today + timedelta(days=i)
        rows.append([InlineKeyboardButton(text=d.strftime("%a %d.%m"), callback_data=f"pick_day:{d.isoformat()}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="cart_show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_slots(day_str: str) -> InlineKeyboardMarkup:
    rows = []
    for slot in generate_slots():
        if reservation_slot_taken(day_str, slot):
            continue
        rows.append([InlineKeyboardButton(text=slot, callback_data=f"pick_slot:{day_str}:{slot}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="— Нет доступных слотов —", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к дням", callback_data="checkout_start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_confirm(day_str: str, slot: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить бронь", callback_data=f"confirm:{day_str}:{slot}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"pick_day:{day_str}")],
        ]
    )


def kb_after_reservation(res_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить бронь", callback_data=f"cancel_res:{res_id}")],
        ]
    )


async def safe_send_message(chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramRetryAfter as e:
        await asyncio.sleep(int(e.retry_after) + 1)
        with suppress(Exception):
            await bot.send_message(chat_id, text)
    except Exception:
        pass


def user_label(user_id: int, user: object | None = None) -> str:
    username = getattr(user, "username", None) if user else None
    full_name = getattr(user, "full_name", None) if user else None

    parts = [f"id={user_id}"]
    if username:
        parts.append(f"@{username}")
    if full_name:
        parts.append(full_name)
    return " | ".join(parts)


async def notify_admin_reservation(res_id: int, buyer: Message | CallbackQuery | None = None) -> None:
    r = get_reservation(res_id)
    if not r:
        return

    user_obj = None
    if buyer and getattr(buyer, "from_user", None):
        user_obj = buyer.from_user

    items = reservation_items(res_id)
    lines = [
        f"📌 Новая бронь #{res_id}",
        f"👤 Покупатель: {user_label(int(r['user_id']), user_obj)}",
        f"📅 {r['day']} {r['slot']}",
        f"⏳ До: {parse_iso(r['expires_at']).astimezone(TZ).strftime('%d.%m.%Y %H:%M')}",
        "",
        "Товары:",
    ]
    for it in items:
        price = f" — {it['price']}" if it["price"] else ""
        lines.append(f"• #{it['id']} {it['title']}{price}")

    text = "\n".join(lines)

    targets = []
    if ADMIN_CHAT_ID:
        targets.append(ADMIN_CHAT_ID)
    targets.extend(ADMIN_IDS)

    for chat_id in set(targets):
        await safe_send_message(chat_id, text)


async def notify_admin_reservation_cancelled(res_id: int, by_user: object | int) -> None:
    by_user_id = int(by_user.id) if hasattr(by_user, "id") else int(by_user)
    r = get_reservation(res_id)
    items = reservation_items(res_id)
    lines = [
        f"❌ Бронь отменена #{res_id}",
        f"👤 Покупатель: {user_label(by_user_id, by_user if hasattr(by_user, 'id') else None)}",
    ]
    if r:
        lines.append(f"📅 {r['day']} {r['slot']}")
    if items:
        lines.append("")
        lines.append("Товары:")
        for it in items:
            price = f" — {it['price']}" if it["price"] else ""
            lines.append(f"• #{it['id']} {it['title']}{price}")

    text = "\n".join(lines)
    targets = []
    if ADMIN_CHAT_ID:
        targets.append(ADMIN_CHAT_ID)
    targets.extend(ADMIN_IDS)
    for chat_id in set(targets):
        await safe_send_message(chat_id, text)


async def publish_item(item_id: int) -> None:
    item = get_item(item_id)
    if not item:
        raise ValueError("item_not_found")

    me = await bot.get_me()
    username = me.username or ""
    text = item_text(item)
    photos = get_item_photos(item_id)

    reply_markup = kb_item(item_id, username) if item["status"] == "ACTIVE" else None
    if photos:
        msg = await bot.send_photo(
            chat_id=CHANNEL_TARGET,
            photo=photos[0],
            caption=text,
            reply_markup=reply_markup,
        )
    else:
        msg = await bot.send_message(
            chat_id=CHANNEL_TARGET,
            text=text,
            reply_markup=reply_markup,
        )
    set_item_channel_message(item_id, msg.message_id)


async def update_channel_post(item_id: int) -> None:
    item = get_item(item_id)
    if not item:
        return

    if not item["channel_message_id"]:
        if item["status"] != "SOLD":
            await publish_item(item_id)
        return

    if item["status"] == "SOLD":
        with suppress(TelegramBadRequest):
            await bot.delete_message(chat_id=CHANNEL_TARGET, message_id=int(item["channel_message_id"]))
        clear_item_channel_message(item_id)
        return

    me = await bot.get_me()
    username = me.username or ""
    reply_markup = kb_item(item_id, username) if item["status"] == "ACTIVE" else None
    has_photo = bool(get_item_photos(item_id))
    try:
        if has_photo:
            await bot.edit_message_caption(
                chat_id=CHANNEL_TARGET,
                message_id=int(item["channel_message_id"]),
                caption=item_text(item),
                reply_markup=reply_markup,
            )
        else:
            await bot.edit_message_text(
                chat_id=CHANNEL_TARGET,
                message_id=int(item["channel_message_id"]),
                text=item_text(item),
                reply_markup=reply_markup,
            )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        # post no longer editable -> publish a fresh one
        await publish_item(item_id)


def parse_csv_rows(content: str) -> list[CsvRow]:
    # авто-определяем разделитель CSV: ; или ,
    sample = content[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,")
    except Exception:
        # Частый кейс: одна строка заголовка/данных в Telegram-файле.
        # Тогда Sniffer не может уверенно определить разделитель.
        dialect = csv.excel_semicolon if ";" in sample.partition("\n")[0] else csv.excel

    # Иногда CSV приходит с BOM в первой ячейке заголовка.
    normalized_content = content.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(normalized_content), dialect=dialect)

    alias_map = {
        "title": {"title", "название", "товар", "наименование", "name"},
        "price": {"price", "цена", "стоимость", "price_rub"},
        "description": {"description", "описание", "desc", "details"},
        "photos": {"photos", "photo", "фото", "фотографии", "photo_id", "photo_ids"},
    }

    rows: list[CsvRow] = []
    if not reader.fieldnames:
        return rows

    # Иногда приходят CSV с разным регистром/пробелами/кавычками в заголовках.
    header_map: dict[str, str] = {}
    for raw_name in reader.fieldnames:
        if not raw_name:
            continue
        key = raw_name.strip().strip('"').lower()
        if key:
            header_map[key] = raw_name

    def resolve_column(logical_name: str) -> Optional[str]:
        for alias in alias_map[logical_name]:
            if alias in header_map:
                return header_map[alias]
        return None

    title_col = resolve_column("title")
    price_col = resolve_column("price")
    description_col = resolve_column("description")
    photos_col = resolve_column("photos")

    # fallback: CSV без заголовка. Например: title;price;description;photos
    headerless_mode = title_col is None
    if headerless_mode:
        raw_reader = csv.reader(io.StringIO(normalized_content), dialect=dialect)
        for raw in raw_reader:
            cols = [(c or "").strip() for c in raw]
            if not cols or not cols[0]:
                continue
            # Пропускаем строку, если она выглядит как заголовок.
            first = cols[0].strip().lower().strip('"')
            if first in alias_map["title"]:
                continue
            rows.append(
                CsvRow(
                    title=cols[0],
                    price=cols[1] if len(cols) > 1 else "",
                    description=cols[2] if len(cols) > 2 else "",
                    photos=parse_photos_field(cols[3] if len(cols) > 3 else ""),
                )
            )
        return rows

    for row in reader:
        title = (row.get(title_col) or "").strip() if title_col else ""
        if not title:
            continue
        price = (row.get(price_col) or "").strip() if price_col else ""
        description = (row.get(description_col) or "").strip() if description_col else ""
        photos = parse_photos_field(
            ((row.get(photos_col) or "").strip() if photos_col else "")
        )
        rows.append(CsvRow(title=title, price=price, description=description, photos=photos))
    return rows

# =========================
# User commands
# =========================
@dp.message(CommandStart())
async def cmd_start(m: Message):
    payload = ""
    if m.text:
        parts = m.text.split(maxsplit=1)
        if len(parts) == 2:
            payload = parts[1].strip()

    if payload == "checkout":
        await show_cart(m.from_user.id)
        return

    await m.answer(
        "Привет! Добавляй товары из канала кнопкой 🛒\n"
        "Команды:\n"
        "/cart — корзина\n"
        "/my — мои брони\n\n"
        f"{SUPPORT_TEXT}"
    )


@dp.message(Command("cart"))
async def cmd_cart(m: Message):
    await show_cart(m.from_user.id)


@dp.message(Command("version"))
async def cmd_version(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer("⛔ Только для админов")
        return
    await m.answer(f"🔖 Revision: {RUNTIME_REVISION}")


@dp.message(Command("my"))
async def cmd_my(m: Message):
    rows = user_reservations(m.from_user.id)
    if not rows:
        await m.answer("У тебя пока нет броней.")
        return

    lines = ["📦 Твои брони:"]
    for r in rows:
        lines.append(f"• #{r['id']} — {r['day']} {r['slot']} ({r['status']})")
    lines.append("\nЧтобы отменить активную бронь: /cancel <id>")
    await m.answer("\n".join(lines))


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Использование: /cancel <reservation_id>")
        return
    res_id = int(parts[1])
    ok = cancel_reservation(m.from_user.id, res_id)
    if not ok:
        await m.answer("Не нашёл активную бронь с таким id.")
        return

    for it in reservation_items(res_id):
        with suppress(Exception):
            await update_channel_post(int(it["id"]))
    await notify_admin_reservation_cancelled(res_id, m.from_user)
    await m.answer("✅ Бронь отменена.")


# =========================
# Admin commands
# =========================
@dp.message(Command("photoid"))
async def cmd_photoid(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer("⛔ Только для админов")
        return
    await m.answer("Пришли фото как фото (не документом) — верну photo_id.")


async def _handle_add_command(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer("⛔ Только для админов")
        return

    command_text = (m.text or m.caption or "").strip()
    raw = ""
    if command_text:
        # Поддерживаем /add и /add@botname в тексте/подписи.
        raw = re.sub(r"^/add(?:@\w+)?", "", command_text, count=1, flags=re.IGNORECASE).strip()

    # /add без данных → показать инструкцию
    if not raw:
        await m.answer(
            "Формат:\n"
            "/add Название | Цена | Описание\n"
            "(можно в подписи к фото)\n\n"
            "Фото можно приложить как вложение к сообщению — photo_id вручную не нужен.\n"
            "Можно и так: ответить командой /add ... на сообщение с фото.\n"
            "Либо добавь photo_id 4-м полем: /add Название | Цена | Описание | photo_id\n"
            "Описание и фото можно пропускать."
        )
        return

    # Делим только на 4 части: title | price | description | photos
    # Так описание не ломается, а блок photos можно передавать как через |, так и через ,.
    parts = [x.strip() for x in raw.split("|", 3)]

    title = parts[0] if len(parts) > 0 else ""
    price = parts[1] if len(parts) > 1 else ""
    description = parts[2] if len(parts) > 2 else ""
    photos = parse_photos_field(parts[3]) if len(parts) > 3 else []

    # Частый реальный ввод: /add Название | Цена | <photo_id>
    # (без отдельного поля описания). В таком случае интерпретируем 3-е поле как photos.
    if len(parts) == 3 and not photos and looks_like_photo_id_list(description):
        description = ""
        photos = parse_photos_field(parts[2])

    context_photo_id, photo_source = extract_photo_from_message_context(m)
    if context_photo_id:
        photos = [context_photo_id]

    caption_add = bool((m.caption or "").strip() and re.match(r"(?i)^/add(?:@\w+)?(?:\s|$)", (m.caption or "").strip()))
    if caption_add and not photos:
        await m.answer("❌ Фото не найдено. Отправь именно фото (не документ) и подписью напиши /add ...")
        return

    if not title:
        await m.answer("Нет названия. Пример: /add Платье | 2500 | описание")
        return

    try:
        logger.info("/add parsed: title=%r price=%r photos=%d source=%s", title, price, len(photos), photo_source if 'photo_source' in locals() else 'n/a')
        item_id = add_item(title=title, price=price, description=description, photos=photos)
        await publish_item(item_id)
    except Exception as e:
        await m.answer(f"❌ Не удалось добавить товар: {e}")
        return

    await m.answer(f"✅ Опубликовано в канал. ID товара: #{item_id}")


@dp.message(Command("add"))
async def cmd_add(m: Message):
    await _handle_add_command(m)


@dp.message(F.photo, F.caption.regexp(r"(?i)^\s*/add(?:@\w+)?(?:\s|$)"))
async def cmd_add_caption(m: Message):
    await _handle_add_command(m)


@dp.message(Command("addphoto"))
async def cmd_addphoto(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Использование: /addphoto <item_id>")
        return
    item_id = int(parts[1])
    if not get_item(item_id):
        await m.answer("Товар не найден")
        return
    await state.set_state(AddPhotoFlow.waiting_photo)
    await state.update_data(item_id=item_id)
    await m.answer(f"Пришли фото для товара #{item_id}")


@dp.message(AddPhotoFlow.waiting_photo, F.photo)
async def addphoto_receive(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    item_id = int(data["item_id"])
    photo_id = m.photo[-1].file_id
    add_photo_to_item(item_id, photo_id)
    await state.clear()
    await m.answer(f"✅ Фото добавлено к товару #{item_id}")


@dp.message(Command("edit"))
async def cmd_edit(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Использование: /edit <item_id>")
        return
    item_id = int(parts[1])
    item = get_item(item_id)
    if not item:
        await m.answer("Товар не найден")
        return

    await state.set_state(EditFlow.waiting)
    await state.update_data(item_id=item_id)
    await m.answer(
        "Пришли новые данные в формате:\n"
        "Название | Цена | Описание"
    )


@dp.message(EditFlow.waiting)
async def edit_receive(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    title, price, description = parse_title_block(m.text or "")
    if not title:
        await m.answer("Неверный формат. Пример: Платье | 2000 | Новое описание")
        return

    data = await state.get_data()
    item_id = int(data["item_id"])
    update_item(item_id, title, price, description)
    await state.clear()
    await update_channel_post(item_id)
    await m.answer(f"✅ Товар #{item_id} обновлён и пост в канале отредактирован.")


@dp.message(Command("sold"))
async def cmd_sold(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Использование: /sold <item_id>")
        return
    item_id = int(parts[1])
    if not get_item(item_id):
        await m.answer("Товар не найден")
        return
    set_item_status(item_id, "SOLD")
    await update_channel_post(item_id)
    await m.answer(f"✅ Товар #{item_id} отмечен как SOLD")


@dp.message(Command("active"))
async def cmd_active(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Использование: /active <item_id>")
        return
    item_id = int(parts[1])
    if not get_item(item_id):
        await m.answer("Товар не найден")
        return
    set_item_status(item_id, "ACTIVE")
    await update_channel_post(item_id)
    await m.answer(f"✅ Товар #{item_id} снова ACTIVE")


@dp.message(Command("csv"))
async def cmd_csv(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.set_state(CsvFlow.waiting_document)
    await m.answer(
        "Пришли CSV файлом (document).\n"
        "Колонки: title,price,description,photos\n"
        "photos: photo_id через |"
    )


@dp.message(CsvFlow.waiting_document, F.document)
async def on_csv(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    name = (m.document.file_name or "").lower()
    mime = (m.document.mime_type or "").lower()
    if not (name.endswith(".csv") or mime in {"text/csv", "application/vnd.ms-excel", "application/csv", "text/plain"}):
        await m.answer("⚠️ Файл не похож на CSV по имени/MIME, пробую прочитать всё равно…")

    file = await bot.get_file(m.document.file_id)
    payload = await bot.download_file(file.file_path)
    raw_bytes = payload.read()
    try:
        content = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw_bytes.decode("cp1251", errors="replace")

    rows = parse_csv_rows(content)
    if not rows:
        await m.answer("В CSV нет валидных строк")
        await state.clear()
        return

    ok = 0
    failed = 0
    skipped = 0
    failed_rows: list[str] = []
    for row in rows:
        try:
            existing_id = find_item_by_fields(row.title, row.price, row.description)
            if existing_id:
                skipped += 1
                continue
            item_id = add_item(row.title, row.price, row.description, row.photos)
            await publish_item(item_id)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            failed += 1
            failed_rows.append(f"{row.title[:40]} — retry_after:{int(e.retry_after)}")
        except Exception as e:
            failed += 1
            failed_rows.append(f"{row.title[:40]} — {type(e).__name__}: {e}")
            logger.exception("CSV import failed for row title=%r", row.title)
        await asyncio.sleep(0.2)

    await state.clear()
    summary = (
        f"✅ Импорт завершён. Добавлено: {ok}. "
        f"Пропущено (уже есть): {skipped}. Ошибок: {failed}."
    )
    if failed_rows:
        preview = "\n".join(f"• {x}" for x in failed_rows[:10])
        await m.answer(summary + "\n\nОшибки:\n" + preview)
    else:
        await m.answer(summary)


@dp.message(Command("block"))
async def cmd_block(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) < 3:
        await m.answer("Использование: /block YYYY-MM-DD HH:MM[,HH:MM,...] [HH:MM] [причина]")
        return

    day_str = parts[1]
    start_slot = parts[2]
    end_slot = None
    reason_start = 3
    if len(parts) >= 4 and re.match(r"^\d{2}:\d{2}$", parts[3]):
        end_slot = parts[3]
        reason_start = 4
    reason = " ".join(parts[reason_start:]).strip() or "Недоступно"

    try:
        slots = parse_slot_selection(start_slot, end_slot)
        added = block_slots(day_str, slots, reason, m.from_user.id)
        await m.answer(f"✅ Заблокировано слотов: {added} ({', '.join(slots)})")
    except ValueError as e:
        msg = str(e)
        if msg == "list_and_range_conflict":
            await m.answer("Ошибка: укажи либо список через запятую, либо диапазон, но не оба варианта сразу.")
            return
        if msg.startswith("invalid_slot:"):
            await m.answer(f"Ошибка: некорректный слот {msg.split(':', 1)[1]}")
            return
        await m.answer(f"Ошибка: {e}")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")


@dp.message(Command("unblock"))
async def cmd_unblock(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) < 3:
        await m.answer("Использование: /unblock YYYY-MM-DD HH:MM[,HH:MM,...] [HH:MM]")
        return

    day_str = parts[1]
    start_slot = parts[2]
    end_slot = parts[3] if len(parts) >= 4 and re.match(r"^\d{2}:\d{2}$", parts[3]) else None
    try:
        slots = parse_slot_selection(start_slot, end_slot)
        removed = unblock_slots(day_str, slots)
        await m.answer(f"✅ Разблокировано слотов: {removed} ({', '.join(slots)})")
    except ValueError as e:
        msg = str(e)
        if msg == "list_and_range_conflict":
            await m.answer("Ошибка: укажи либо список через запятую, либо диапазон, но не оба варианта сразу.")
            return
        if msg.startswith("invalid_slot:"):
            await m.answer(f"Ошибка: некорректный слот {msg.split(':', 1)[1]}")
            return
        await m.answer(f"Ошибка: {e}")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")


@dp.message(Command("slots"))
async def cmd_slots(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2:
        await m.answer("Использование: /slots YYYY-MM-DD")
        return
    day_str = parts[1]
    blocked = {row["slot"]: row["reason"] for row in day_blocks(day_str)}

    lines = [f"Слоты на {day_str}:"]
    for slot in generate_slots():
        if slot in blocked:
            lines.append(f"• {slot} — 🚫 {blocked[slot]}")
            continue
        busy = reservation_slot_taken(day_str, slot)
        lines.append(f"• {slot} — {'🔒 занят' if busy else '✅ свободен'}")
    await m.answer("\n".join(lines))


@dp.message(Command("admin"))
async def cmd_admin_help(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer(
        "Управление слотами бронирования:\n"
        "• /slots YYYY-MM-DD — посмотреть занятость/блокировки\n"
        "• /block YYYY-MM-DD HH:MM[,HH:MM] [HH:MM] [причина] — закрыть один/несколько/диапазон\n"
        "• /unblock YYYY-MM-DD HH:MM[,HH:MM] [HH:MM] — открыть один/несколько/диапазон\n"
        "• /blockday YYYY-MM-DD [причина] — закрыть весь день\n"
        "• /unblockday YYYY-MM-DD — открыть весь день"
    )


@dp.message(Command("blockday"))
async def cmd_blockday(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) < 2:
        await m.answer("Использование: /blockday YYYY-MM-DD [причина]")
        return
    day_str = parts[1]
    reason = " ".join(parts[2:]).strip() or "Недоступно весь день"
    try:
        added = block_slots(day_str, generate_slots(), reason, m.from_user.id)
        await m.answer(f"✅ День {day_str} закрыт. Заблокировано слотов: {added}")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")


@dp.message(Command("unblockday"))
async def cmd_unblockday(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) < 2:
        await m.answer("Использование: /unblockday YYYY-MM-DD")
        return
    day_str = parts[1]
    try:
        removed = unblock_slots(day_str, generate_slots())
        await m.answer(f"✅ День {day_str} открыт. Разблокировано слотов: {removed}")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")


@dp.message(F.photo)
async def photo_id_echo(m: Message):
    if not is_admin(m.from_user.id):
        return
    if (m.caption or "").strip().lower().startswith("/add"):
        return
    await m.answer(f"photo_id:\n{m.photo[-1].file_id}")


# =========================
# Cart & checkout callbacks
# =========================
async def answer_user(user_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)
        return True
    except TelegramForbiddenError:
        logger.warning("Cannot DM user %s: user has not started bot", user_id)
        return False
    except Exception:
        logger.exception("Failed to DM user %s", user_id)
        return False


async def show_cart(user_id: int):
    items = cart_list(user_id)
    if not items:
        await answer_user(user_id, "Корзина пуста. Добавляй товары в канале кнопкой 🛒")
        return

    lines = ["🛒 Твоя корзина:"]
    for item in items:
        price = f" — {item['price']}" if item["price"] else ""
        lines.append(f"• #{item['id']} {item['title']}{price}")
    text = "\n".join(lines)
    await answer_user(user_id, text, reply_markup=kb_cart(items))


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data == "cart_show")
async def cb_cart_show(c: CallbackQuery):
    await c.answer()
    await show_cart(c.from_user.id)


@dp.callback_query(F.data.startswith("cart_add:"))
async def cb_cart_add(c: CallbackQuery):
    item_id = int(c.data.split(":", 1)[1])
    item = get_item(item_id)
    if not item_is_available(item):
        ok = await answer_user(c.from_user.id, "Этот товар недоступен.")
        if not ok:
            await c.answer("Товар недоступен", show_alert=True)
        else:
            await c.answer()
        return
    if item_is_reserved(item_id):
        ok = await answer_user(c.from_user.id, "Этот товар уже забронирован.")
        if not ok:
            await c.answer("Товар уже забронирован", show_alert=True)
        else:
            await c.answer()
        return

    if cart_add(c.from_user.id, item_id):
        ok = await answer_user(c.from_user.id, "✅ Добавлено в корзину. Открой /cart")
        if not ok:
            await c.answer("Открой бота в ЛС: /start", show_alert=True)
        else:
            await c.answer()
    else:
        ok = await answer_user(c.from_user.id, "Не удалось добавить.")
        if not ok:
            await c.answer("Не удалось добавить", show_alert=True)
        else:
            await c.answer()


@dp.callback_query(F.data.startswith("cart_remove:"))
async def cb_cart_remove(c: CallbackQuery):
    await c.answer()
    item_id = int(c.data.split(":", 1)[1])
    cart_remove(c.from_user.id, item_id)
    await show_cart(c.from_user.id)


@dp.callback_query(F.data == "cart_clear")
async def cb_cart_clear(c: CallbackQuery):
    await c.answer()
    cart_clear(c.from_user.id)
    await answer_user(c.from_user.id, "🧹 Корзина очищена")


@dp.callback_query(F.data == "checkout_start")
async def cb_checkout_start(c: CallbackQuery):
    await c.answer()
    items = cart_list(c.from_user.id)
    if not items:
        await answer_user(c.from_user.id, "Корзина пуста")
        return
    await answer_user(
        c.from_user.id,
        "Самовывоз из Belgrade Waterfront\n\nВыбери день:",
        reply_markup=kb_days(),
    )


@dp.callback_query(F.data.startswith("pick_day:"))
async def cb_pick_day(c: CallbackQuery):
    await c.answer()
    day_str = c.data.split(":", 1)[1]
    await answer_user(c.from_user.id, f"Выбери слот на {day_str}", reply_markup=kb_slots(day_str))


@dp.callback_query(F.data.startswith("pick_slot:"))
async def cb_pick_slot(c: CallbackQuery):
    await c.answer()
    _, day_str, slot = c.data.split(":", 2)
    if reservation_slot_taken(day_str, slot):
        await answer_user(c.from_user.id, "Этот слот уже занят, выбери другой.")
        await answer_user(c.from_user.id, f"Слоты на {day_str}:", reply_markup=kb_slots(day_str))
        return
    await answer_user(
        c.from_user.id,
        f"Подтверди бронь:\n📅 {day_str}\n🕒 {slot}",
        reply_markup=kb_confirm(day_str, slot),
    )


@dp.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(c: CallbackQuery):
    await c.answer()
    _, day_str, slot = c.data.split(":", 2)

    items = cart_list(c.from_user.id)
    if not items:
        await answer_user(c.from_user.id, "Корзина пуста.")
        return

    bad = [it for it in items if item_is_reserved(int(it["id"]))]
    if bad:
        await answer_user(c.from_user.id, "Часть товаров уже занята. Удали их из корзины и попробуй снова.")
        await show_cart(c.from_user.id)
        return

    try:
        res_id = create_reservation(c.from_user.id, day_str, slot, [int(it["id"]) for it in items])
    except ValueError:
        await answer_user(c.from_user.id, "Слот уже занят/заблокирован, выбери другой.")
        await answer_user(c.from_user.id, f"Слоты на {day_str}:", reply_markup=kb_slots(day_str))
        return

    cart_clear(c.from_user.id)
    r = get_reservation(res_id)
    exp_str = parse_iso(r["expires_at"]).astimezone(TZ).strftime("%d.%m.%Y %H:%M")

    await answer_user(
        c.from_user.id,
        "✅ Бронь подтверждена\n\n"
        f"📅 {day_str}\n"
        f"🕒 {slot}\n"
        f"⏳ Бронь до: {exp_str}\n\n"
        f"{PICKUP_TEXT}",
        reply_markup=kb_after_reservation(res_id),
    )
    for it in items:
        with suppress(Exception):
            await update_channel_post(int(it["id"]))

    await notify_admin_reservation(res_id, c)


@dp.callback_query(F.data.startswith("cancel_res:"))
async def cb_cancel_res(c: CallbackQuery):
    await c.answer()
    parts = c.data.split(":", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        await answer_user(c.from_user.id, "Не удалось отменить бронь: неверный id.")
        return

    res_id = int(parts[1])
    ok = cancel_reservation(c.from_user.id, res_id)
    if not ok:
        await answer_user(c.from_user.id, "Эту бронь уже нельзя отменить.")
        return

    for it in reservation_items(res_id):
        with suppress(Exception):
            await update_channel_post(int(it["id"]))
    await notify_admin_reservation_cancelled(res_id, c.from_user)
    await answer_user(c.from_user.id, f"✅ Бронь #{res_id} отменена. Товары снова в наличии.")


# =========================
# Runtime
# =========================
async def background_cleanup() -> None:
    while True:
        with suppress(Exception):
            cleanup_expired()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break


async def main() -> None:
    init_db()
    logger.info("Starting bot revision=%s", RUNTIME_REVISION)
    with suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=False)

    cleaner = asyncio.create_task(background_cleanup())
    try:
        await dp.start_polling(bot)
    except TelegramConflictError as e:
        logger.error(
            "Telegram polling conflict: another bot instance is running with the same BOT_TOKEN. "
            "Stop duplicate instances and redeploy. Details: %s",
            e,
        )
        raise RuntimeError("telegram_polling_conflict") from e
    finally:
        cleaner.cancel()
        with suppress(asyncio.CancelledError):
            await cleaner


if __name__ == "__main__":
    asyncio.run(main())
