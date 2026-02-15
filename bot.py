 (cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF' 
diff --git a/bot.py b/bot.py
index 7d85b34d83d1d06555cd99d0457d97c1caad9b69..364f7dd211f1e589570017c6cfd0b30646837264 100644
--- a/bot.py
+++ b/bot.py
@@ -1,926 +1,1175 @@
-import sys
-import traceback
+import asyncio
 import csv
 import io
-
-print("=== BOOT: bot.py started ===", flush=True)
-
-def _excepthook(exc_type, exc, tb):
-    traceback.print_exception(exc_type, exc, tb)
-    sys.stdout.flush()
-    sys.stderr.flush()
-
-sys.excepthook = _excepthook
 import os
 import re
 import sqlite3
-import asyncio
-from aiogram.exceptions import TelegramRetryAfter
-def db() -> sqlite3.Connection:
-    conn = sqlite3.connect(DB_PATH)
-    conn.row_factory = sqlite3.Row
-    return conn
-from datetime import datetime, timedelta, date, time
+from contextlib import suppress
+from dataclasses import dataclass
+from datetime import date, datetime, time, timedelta
+from typing import Optional
 from zoneinfo import ZoneInfo
-from typing import Optional, List, Tuple
 
 from aiogram import Bot, Dispatcher, F
+from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
+from aiogram.filters import Command, CommandStart
+from aiogram.fsm.context import FSMContext
+from aiogram.fsm.state import State, StatesGroup
+from aiogram.fsm.storage.memory import MemoryStorage
 from aiogram.types import (
-    Message,
     CallbackQuery,
-    InlineKeyboardMarkup,
     InlineKeyboardButton,
+    InlineKeyboardMarkup,
+    InputMediaPhoto,
+    Message,
 )
-from aiogram.filters import Command, CommandStart
-from aiogram.fsm.state import State, StatesGroup
-from aiogram.fsm.context import FSMContext
-from aiogram.fsm.storage.memory import MemoryStorage
-from aiogram.types import InputMediaPhoto
 
 
 # =========================
 # ENV
 # =========================
 BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
-WAIT_PHOTO_ID = set()
-WAIT_CSV_IMPORT = set()
-ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
 ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
-
-CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()  # e.g. "@bestgaragesale"
-SUPPORT_TEXT = os.getenv("SUPPORT_TEXT", "Если бот лагает — @liusene").strip()
+ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()
+CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()
 TIMEZONE = os.getenv("TIMEZONE", "Europe/Belgrade").strip()
-
-print("=== ADMIN_CHAT_ID ===", repr(ADMIN_CHAT_ID), flush=True)
-print("=== ADMIN_IDS_RAW ===", repr(ADMIN_IDS_RAW), flush=True)
+SUPPORT_TEXT = os.getenv("SUPPORT_TEXT", "Если бот лагает — @liusene").strip()
+PICKUP_TEXT = os.getenv(
+    "PICKUP_TEXT",
+    "📍 Самовывоз из Belgrade Waterfront\nАдрес: BW Sole. Bulevar Vudroa Vilsona, 17\nКак подъедете, напишите в тг @liusene",
+).strip()
+SLOT_START = os.getenv("SLOT_START", "09:00").strip()
+SLOT_END = os.getenv("SLOT_END", "21:00").strip()
+SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "30"))
 
 if not BOT_TOKEN:
     raise RuntimeError("BOT_TOKEN is empty")
-
-ADMIN_IDS = set()
-for x in re.split(r"[,\s]+", ADMIN_IDS_RAW):
-    x = x.strip()
-    if x.isdigit():
-        ADMIN_IDS.add(int(x))
-
-print("=== ADMIN_IDS ===", ADMIN_IDS, flush=True)
-
-TZ = ZoneInfo(TIMEZONE)
-
-ADMIN_IDS = set()
-for x in re.split(r"[,\s]+", ADMIN_IDS_RAW):
-    x = x.strip()
-    if x.isdigit():
-        ADMIN_IDS.add(int(x))
-
+if not CHANNEL_USERNAME:
+    raise RuntimeError("CHANNEL_USERNAME is empty (example: @my_channel)")
+
+ADMIN_IDS = {
+    int(x)
+    for x in re.split(r"[,\s]+", ADMIN_IDS_RAW)
+    if x.strip().isdigit()
+}
+ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW) if ADMIN_CHAT_ID_RAW.isdigit() else None
 TZ = ZoneInfo(TIMEZONE)
+DB_PATH = "sale.db"
 
 
 # =========================
 # DB
 # =========================
-DB_PATH = "sale.db"
-
-
 def db() -> sqlite3.Connection:
     conn = sqlite3.connect(DB_PATH)
     conn.row_factory = sqlite3.Row
     return conn
 
 
+def now_local() -> datetime:
+    return datetime.now(tz=TZ)
+
+
+def iso(dt: datetime) -> str:
+    return dt.isoformat()
+
+
+def parse_iso(value: str) -> datetime:
+    return datetime.fromisoformat(value)
+
+
 def init_db() -> None:
     with db() as conn:
         conn.execute(
             """
             CREATE TABLE IF NOT EXISTS items (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 title TEXT NOT NULL,
                 price TEXT,
-                photo_id TEXT,
+                description TEXT,
+                status TEXT NOT NULL DEFAULT 'ACTIVE',
+                created_at TEXT NOT NULL,
+                updated_at TEXT NOT NULL,
+                is_active INTEGER NOT NULL DEFAULT 1,
+                channel_message_id INTEGER
+            )
+            """
+        )
+        conn.execute(
+            """
+            CREATE TABLE IF NOT EXISTS item_photos (
+                id INTEGER PRIMARY KEY AUTOINCREMENT,
+                item_id INTEGER NOT NULL,
+                photo_id TEXT NOT NULL,
+                pos INTEGER NOT NULL DEFAULT 0,
                 created_at TEXT NOT NULL,
-                is_active INTEGER NOT NULL DEFAULT 1
+                UNIQUE(item_id, photo_id)
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
-                address TEXT,
-                status TEXT NOT NULL
+                status TEXT NOT NULL DEFAULT 'CONFIRMED'
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
-        conn.execute("CREATE INDEX IF NOT EXISTS idx_res_expires ON reservations(expires_at)")
-        conn.commit()
-
         conn.execute(
             """
-            CREATE TABLE IF NOT EXISTS item_photos (
+            CREATE TABLE IF NOT EXISTS slot_blocks (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
-                item_id INTEGER NOT NULL,
-                photo_id TEXT NOT NULL,
-                pos INTEGER NOT NULL DEFAULT 0,
+                day TEXT NOT NULL,
+                slot TEXT NOT NULL,
+                reason TEXT,
+                created_by INTEGER,
                 created_at TEXT NOT NULL,
-                UNIQUE(item_id, photo_id)
+                UNIQUE(day, slot)
             )
             """
-)
+        )
+
         conn.execute("CREATE INDEX IF NOT EXISTS idx_item_photos_item ON item_photos(item_id)")
+        conn.execute("CREATE INDEX IF NOT EXISTS idx_reservations_day_slot ON reservations(day, slot)")
+        conn.execute("CREATE INDEX IF NOT EXISTS idx_reservations_exp ON reservations(expires_at)")
+        conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_blocks_day ON slot_blocks(day)")
+        conn.commit()
 
 
-def now_local() -> datetime:
-    return datetime.now(tz=TZ)
+def item_text(item: sqlite3.Row) -> str:
+    lines = [f"🛍 {item['title']}"]
+    if item["price"]:
+        lines.append(f"💰 {item['price']}")
+    if item["description"]:
+        lines.append("")
+        lines.append(item["description"])
+    lines.append("")
+    lines.append(f"ℹ️ {SUPPORT_TEXT}")
+    return "\n".join(lines)
+
+
+def parse_title_block(text: str) -> tuple[Optional[str], str, str]:
+    parts = [x.strip() for x in (text or "").split("|", 2)]
+    if not parts or not parts[0]:
+        return None, "", ""
+    if len(parts) == 1:
+        return parts[0], "", ""
+    if len(parts) == 2:
+        return parts[0], parts[1], ""
+    return parts[0], parts[1], parts[2]
+
+
+def parse_photos_field(value: str) -> list[str]:
+    if not value:
+        return []
+    out = []
+    for chunk in value.replace(",", "|").split("|"):
+        pid = chunk.strip()
+        if pid:
+            out.append(pid)
+    return out
 
 
-def iso(dt: datetime) -> str:
-    return dt.isoformat()
+def add_item(title: str, price: str, description: str, photos: list[str]) -> int:
+    now = iso(now_local())
+    with db() as conn:
+        cur = conn.execute(
+            """
+            INSERT INTO items (title, price, description, status, created_at, updated_at, is_active)
+            VALUES (?, ?, ?, 'ACTIVE', ?, ?, 1)
+            """,
+            (title, price, description, now, now),
+        )
+        item_id = int(cur.lastrowid)
+        for idx, photo_id in enumerate(photos):
+            conn.execute(
+                "INSERT OR IGNORE INTO item_photos (item_id, photo_id, pos, created_at) VALUES (?, ?, ?, ?)",
+                (item_id, photo_id, idx, now),
+            )
+        conn.commit()
+        return item_id
 
 
-def parse_iso(s: str) -> datetime:
-    return datetime.fromisoformat(s)
+def get_item(item_id: int) -> Optional[sqlite3.Row]:
+    with db() as conn:
+        return conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
 
 
-def cleanup_expired() -> int:
-    n = 0
+def get_item_photos(item_id: int) -> list[str]:
     with db() as conn:
         rows = conn.execute(
-            "SELECT id, expires_at, status FROM reservations WHERE status='CONFIRMED'"
+            "SELECT photo_id FROM item_photos WHERE item_id=? ORDER BY pos, id",
+            (item_id,),
         ).fetchall()
-        for r in rows:
-            exp = parse_iso(r["expires_at"])
-            if now_local() >= exp:
-                conn.execute(
-                    "UPDATE reservations SET status='EXPIRED' WHERE id=?",
-                    (r["id"],),
-                )
-                n += 1
-        conn.commit()
-    return n
+        return [r["photo_id"] for r in rows]
 
 
-def item_is_reserved(item_id: int) -> bool:
-    cleanup_expired()
+def add_photo_to_item(item_id: int, photo_id: str) -> None:
     with db() as conn:
-        r = conn.execute(
-            """
-            SELECT 1
-            FROM reservation_items ri
-            JOIN reservations r ON r.id = ri.reservation_id
-            WHERE ri.item_id = ? AND r.status = 'CONFIRMED'
-            """,
+        count = conn.execute(
+            "SELECT COUNT(*) AS c FROM item_photos WHERE item_id=?",
             (item_id,),
-        ).fetchone()
-        return r is not None
+        ).fetchone()["c"]
+        conn.execute(
+            "INSERT OR IGNORE INTO item_photos (item_id, photo_id, pos, created_at) VALUES (?, ?, ?, ?)",
+            (item_id, photo_id, int(count), iso(now_local())),
+        )
+        conn.execute(
+            "UPDATE items SET updated_at=? WHERE id=?",
+            (iso(now_local()), item_id),
+        )
+        conn.commit()
 
 
-def get_item(item_id: int):
+def update_item(item_id: int, title: str, price: str, description: str) -> None:
     with db() as conn:
-        return conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
+        conn.execute(
+            "UPDATE items SET title=?, price=?, description=?, updated_at=? WHERE id=?",
+            (title, price, description, iso(now_local()), item_id),
+        )
+        conn.commit()
 
 
-def add_item_to_db(title: str, price: str, photo_id: Optional[str]) -> int:
+def set_item_status(item_id: int, status: str) -> None:
+    is_active = 1 if status in {"ACTIVE", "RESERVED"} else 0
     with db() as conn:
-        cur = conn.execute(
-            "INSERT INTO items (title, price, photo_id, created_at, is_active) VALUES (?, ?, ?, ?, 1)",
-            (title, price, photo_id, iso(now_local())),
+        conn.execute(
+            "UPDATE items SET status=?, is_active=?, updated_at=? WHERE id=?",
+            (status, is_active, iso(now_local()), item_id),
         )
         conn.commit()
-        return int(cur.lastrowid)
 
-def add_item_photo(item_id: int, photo_id: str, pos: int = 0) -> None:
+
+def set_item_channel_message(item_id: int, message_id: int) -> None:
     with db() as conn:
         conn.execute(
-            "INSERT OR IGNORE INTO item_photos (item_id, photo_id, pos, created_at) VALUES (?, ?, ?, ?)",
-            (item_id, photo_id, pos, iso(now_local())),
+            "UPDATE items SET channel_message_id=?, updated_at=? WHERE id=?",
+            (message_id, iso(now_local()), item_id),
         )
         conn.commit()
 
-def get_item_photos(item_id: int) -> list[str]:
-    with db() as conn:
-        rows = conn.execute(
-            "SELECT photo_id FROM item_photos WHERE item_id=? ORDER BY pos, id",
-            (item_id,),
-        ).fetchall()
-    return [r["photo_id"] for r in rows]
-
-def parse_csv_items(content: str) -> list[tuple[str, str, list[str]]]:
-    import csv
-    import io
-
-    f = io.StringIO(content)
-    reader = csv.DictReader(f)
-
-    out = []
-
-    for row in reader:
-        title = (row.get("title") or "").strip()
-        price = (row.get("price") or "").strip()
-        photo_raw = (row.get("photo") or "").strip()
-
-        if not title:
-            continue
-
-        photo_ids = [p.strip() for p in photo_raw.split(",") if p.strip()]
-
-        out.append((title, price, photo_ids))
 
-    return out
-    
 def cart_add(user_id: int, item_id: int) -> bool:
     if item_is_reserved(item_id):
         return False
     with db() as conn:
         conn.execute(
             "INSERT OR IGNORE INTO carts (user_id, item_id, added_at) VALUES (?, ?, ?)",
             (user_id, item_id, iso(now_local())),
         )
         conn.commit()
-        return True
-        
+    return True
+
 
 def cart_remove(user_id: int, item_id: int) -> None:
     with db() as conn:
         conn.execute("DELETE FROM carts WHERE user_id=? AND item_id=?", (user_id, item_id))
         conn.commit()
 
 
 def cart_clear(user_id: int) -> None:
     with db() as conn:
         conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
         conn.commit()
 
 
-def cart_list(user_id: int) -> List[sqlite3.Row]:
+def cart_list(user_id: int) -> list[sqlite3.Row]:
     cleanup_expired()
     with db() as conn:
         return conn.execute(
             """
             SELECT i.*
             FROM carts c
             JOIN items i ON i.id = c.item_id
-            WHERE c.user_id = ? AND i.is_active = 1
+            WHERE c.user_id=? AND i.status='ACTIVE' AND i.is_active=1
             ORDER BY c.added_at ASC
             """,
             (user_id,),
         ).fetchall()
 
 
-def create_reservation(user_id: int, day_str: str, slot_str: str, item_ids: List[int]) -> int:
+def cleanup_expired() -> int:
+    changed = 0
+    with db() as conn:
+        rows = conn.execute(
+            "SELECT id, expires_at FROM reservations WHERE status='CONFIRMED'"
+        ).fetchall()
+        now = now_local()
+        for row in rows:
+            if now >= parse_iso(row["expires_at"]):
+                conn.execute("UPDATE reservations SET status='EXPIRED' WHERE id=?", (row["id"],))
+                changed += 1
+        conn.commit()
+    return changed
+
+
+def item_is_reserved(item_id: int) -> bool:
+    cleanup_expired()
+    with db() as conn:
+        row = conn.execute(
+            """
+            SELECT 1
+            FROM reservation_items ri
+            JOIN reservations r ON r.id = ri.reservation_id
+            WHERE ri.item_id=? AND r.status='CONFIRMED'
+            LIMIT 1
+            """,
+            (item_id,),
+        ).fetchone()
+    return row is not None
+
+
+def reservation_slot_taken(day_str: str, slot: str) -> bool:
     cleanup_expired()
+    with db() as conn:
+        reserved = conn.execute(
+            "SELECT 1 FROM reservations WHERE day=? AND slot=? AND status='CONFIRMED' LIMIT 1",
+            (day_str, slot),
+        ).fetchone()
+        blocked = conn.execute(
+            "SELECT 1 FROM slot_blocks WHERE day=? AND slot=? LIMIT 1",
+            (day_str, slot),
+        ).fetchone()
+    return reserved is not None or blocked is not None
+
+
+def create_reservation(user_id: int, day_str: str, slot: str, item_ids: list[int]) -> int:
     created = now_local()
-    slot_dt = datetime.fromisoformat(f"{day_str}T{slot_str}:00").replace(tzinfo=TZ)
+    slot_dt = datetime.fromisoformat(f"{day_str}T{slot}:00").replace(tzinfo=TZ)
     expires = max(created + timedelta(hours=24), slot_dt)
+
     with db() as conn:
+        # transactional slot check
+        busy = conn.execute(
+            "SELECT 1 FROM reservations WHERE day=? AND slot=? AND status='CONFIRMED' LIMIT 1",
+            (day_str, slot),
+        ).fetchone()
+        blocked = conn.execute(
+            "SELECT 1 FROM slot_blocks WHERE day=? AND slot=? LIMIT 1",
+            (day_str, slot),
+        ).fetchone()
+        if busy or blocked:
+            raise ValueError("slot_busy")
+
         cur = conn.execute(
             """
-            INSERT INTO reservations (user_id, created_at, expires_at, day, slot, address, status)
-            VALUES (?, ?, ?, ?, ?, NULL, 'CONFIRMED')
+            INSERT INTO reservations (user_id, created_at, expires_at, day, slot, status)
+            VALUES (?, ?, ?, ?, ?, 'CONFIRMED')
             """,
-            (user_id, iso(created), iso(expires), day_str, slot_str),
+            (user_id, iso(created), iso(expires), day_str, slot),
         )
         res_id = int(cur.lastrowid)
-        for it in item_ids:
+
+        for item_id in item_ids:
             conn.execute(
                 "INSERT OR IGNORE INTO reservation_items (reservation_id, item_id) VALUES (?, ?)",
-                (res_id, it),
+                (res_id, item_id),
+            )
+            conn.execute(
+                "UPDATE items SET status='RESERVED', updated_at=? WHERE id=?",
+                (iso(now_local()), item_id),
             )
-        conn.commit()
-        return res_id
-
 
-def set_reservation_address(res_id: int, address: str) -> None:
-    with db() as conn:
-        conn.execute("UPDATE reservations SET address=? WHERE id=?", (address, res_id))
         conn.commit()
+    return res_id
 
 
-def get_reservation(res_id: int):
+def get_reservation(res_id: int) -> Optional[sqlite3.Row]:
     with db() as conn:
         return conn.execute("SELECT * FROM reservations WHERE id=?", (res_id,)).fetchone()
 
 
-def reservation_items(res_id: int) -> List[sqlite3.Row]:
+def reservation_items(res_id: int) -> list[sqlite3.Row]:
     with db() as conn:
         return conn.execute(
             """
             SELECT i.*
             FROM reservation_items ri
             JOIN items i ON i.id = ri.item_id
-            WHERE ri.reservation_id = ?
+            WHERE ri.reservation_id=?
+            ORDER BY i.id
             """,
             (res_id,),
         ).fetchall()
 
 
+def user_reservations(user_id: int) -> list[sqlite3.Row]:
+    cleanup_expired()
+    with db() as conn:
+        return conn.execute(
+            "SELECT * FROM reservations WHERE user_id=? ORDER BY id DESC LIMIT 10",
+            (user_id,),
+        ).fetchall()
+
+
+def cancel_reservation(user_id: int, res_id: int) -> bool:
+    with db() as conn:
+        r = conn.execute(
+            "SELECT * FROM reservations WHERE id=? AND user_id=? AND status='CONFIRMED'",
+            (res_id, user_id),
+        ).fetchone()
+        if not r:
+            return False
+        conn.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?", (res_id,))
+        rows = conn.execute(
+            "SELECT item_id FROM reservation_items WHERE reservation_id=?",
+            (res_id,),
+        ).fetchall()
+        for row in rows:
+            conn.execute(
+                "UPDATE items SET status='ACTIVE', updated_at=? WHERE id=? AND status='RESERVED'",
+                (iso(now_local()), row["item_id"]),
+            )
+        conn.commit()
+    return True
+
+
+def parse_slot_range(start_slot: str, end_slot: Optional[str]) -> list[str]:
+    if not end_slot:
+        return [start_slot]
+
+    slots = []
+    start = datetime.strptime(start_slot, "%H:%M")
+    end = datetime.strptime(end_slot, "%H:%M")
+    if end < start:
+        raise ValueError("end_before_start")
+    cur = start
+    while cur <= end:
+        slots.append(cur.strftime("%H:%M"))
+        cur += timedelta(minutes=SLOT_STEP_MINUTES)
+    return slots
+
+
+def block_slots(day_str: str, slots: list[str], reason: str, created_by: int) -> int:
+    cnt = 0
+    with db() as conn:
+        for slot in slots:
+            conn.execute(
+                """
+                INSERT OR IGNORE INTO slot_blocks (day, slot, reason, created_by, created_at)
+                VALUES (?, ?, ?, ?, ?)
+                """,
+                (day_str, slot, reason, created_by, iso(now_local())),
+            )
+            cnt += conn.total_changes
+        conn.commit()
+    return cnt
+
+
+def unblock_slots(day_str: str, slots: list[str]) -> int:
+    with db() as conn:
+        before = conn.total_changes
+        for slot in slots:
+            conn.execute("DELETE FROM slot_blocks WHERE day=? AND slot=?", (day_str, slot))
+        conn.commit()
+        return conn.total_changes - before
+
+
+def day_blocks(day_str: str) -> list[sqlite3.Row]:
+    with db() as conn:
+        return conn.execute(
+            "SELECT day, slot, reason FROM slot_blocks WHERE day=? ORDER BY slot",
+            (day_str,),
+        ).fetchall()
+
+
 # =========================
-# UI helpers
+# Bot + FSM
 # =========================
+bot = Bot(token=BOT_TOKEN)
+dp = Dispatcher(storage=MemoryStorage())
+
+
+class AddFlow(StatesGroup):
+    waiting = State()
+
+
+class EditFlow(StatesGroup):
+    waiting = State()
+
+
+class AddPhotoFlow(StatesGroup):
+    waiting_photo = State()
+
+
+class CsvFlow(StatesGroup):
+    waiting_document = State()
+
+
+@dataclass
+class CsvRow:
+    title: str
+    price: str
+    description: str
+    photos: list[str]
+
+
+def is_admin(user_id: int) -> bool:
+    return user_id in ADMIN_IDS
+
+
 def kb_item(item_id: int, bot_username: str) -> InlineKeyboardMarkup:
     return InlineKeyboardMarkup(
         inline_keyboard=[
             [
                 InlineKeyboardButton(text="🛒 В корзину", callback_data=f"cart_add:{item_id}"),
-                InlineKeyboardButton(
-                    text="✅ Оформить в боте",
-                    url=f"https://t.me/{bot_username}?start=checkout"
-                ),
+                InlineKeyboardButton(text="✅ Оформить в боте", url=f"https://t.me/{bot_username}?start=checkout"),
             ]
         ]
     )
 
 
-def kb_cart(items: List[sqlite3.Row]) -> InlineKeyboardMarkup:
-    rows = []
-    for it in items:
-        rows.append(
-            [InlineKeyboardButton(text=f"❌ Убрать: #{it['id']}", callback_data=f"cart_remove:{it['id']}")]
-        )
+def kb_cart(items: list[sqlite3.Row]) -> InlineKeyboardMarkup:
+    rows = [
+        [InlineKeyboardButton(text=f"❌ Убрать #{it['id']}", callback_data=f"cart_remove:{it['id']}")]
+        for it in items
+    ]
     rows.append([InlineKeyboardButton(text="✅ Оформить", callback_data="checkout_start")])
     rows.append([InlineKeyboardButton(text="🧹 Очистить корзину", callback_data="cart_clear")])
     return InlineKeyboardMarkup(inline_keyboard=rows)
 
 
-def kb_days() -> InlineKeyboardMarkup:
-    today = now_local().date()
-    buttons = []
-    for i in range(5):
-        d = today + timedelta(days=i)
-        label = d.strftime("%a %d.%m")
-        buttons.append([InlineKeyboardButton(text=label, callback_data=f"pick_day:{d.isoformat()}")])
-    buttons.append([InlineKeyboardButton(text="⬅️ Назад к корзине", callback_data="cart_show")])
-    return InlineKeyboardMarkup(inline_keyboard=buttons)
-
+def generate_slots() -> list[str]:
+    start_t = datetime.strptime(SLOT_START, "%H:%M").time()
+    end_t = datetime.strptime(SLOT_END, "%H:%M").time()
 
-def generate_slots() -> List[str]:
     slots = []
-    t = datetime.combine(date.today(), time(9, 0))
-    end = datetime.combine(date.today(), time(21, 0))
-    while t <= end:
-        slots.append(t.strftime("%H:%M"))
-        t += timedelta(minutes=30)
+    cur = datetime.combine(date.today(), start_t)
+    end_dt = datetime.combine(date.today(), end_t)
+    while cur <= end_dt:
+        slots.append(cur.strftime("%H:%M"))
+        cur += timedelta(minutes=SLOT_STEP_MINUTES)
     return slots
 
 
+def kb_days() -> InlineKeyboardMarkup:
+    today = now_local().date()
+    rows = []
+    for i in range(7):
+        d = today + timedelta(days=i)
+        rows.append([InlineKeyboardButton(text=d.strftime("%a %d.%m"), callback_data=f"pick_day:{d.isoformat()}")])
+    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="cart_show")])
+    return InlineKeyboardMarkup(inline_keyboard=rows)
+
+
 def kb_slots(day_str: str) -> InlineKeyboardMarkup:
-    slots = generate_slots()
     rows = []
-    for s in slots:
-        rows.append([InlineKeyboardButton(text=s, callback_data=f"pick_slot:{day_str}:{s}")])
+    for slot in generate_slots():
+        if reservation_slot_taken(day_str, slot):
+            continue
+        rows.append([InlineKeyboardButton(text=slot, callback_data=f"pick_slot:{day_str}:{slot}")])
+    if not rows:
+        rows.append([InlineKeyboardButton(text="— Нет доступных слотов —", callback_data="noop")])
     rows.append([InlineKeyboardButton(text="⬅️ Назад к дням", callback_data="checkout_start")])
     return InlineKeyboardMarkup(inline_keyboard=rows)
 
 
-def kb_confirm(day_str: str, slot_str: str) -> InlineKeyboardMarkup:
+def kb_confirm(day_str: str, slot: str) -> InlineKeyboardMarkup:
     return InlineKeyboardMarkup(
         inline_keyboard=[
-            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm:{day_str}:{slot_str}")],
-            [InlineKeyboardButton(text="⬅️ Назад к слотам", callback_data=f"pick_day:{day_str}")],
+            [InlineKeyboardButton(text="✅ Подтвердить бронь", callback_data=f"confirm:{day_str}:{slot}")],
+            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"pick_day:{day_str}")],
         ]
     )
 
 
-def kb_checkout_from_start() -> InlineKeyboardMarkup:
-    return InlineKeyboardMarkup(
-        inline_keyboard=[[InlineKeyboardButton(text="✅ Оформить", callback_data="checkout_start")]]
-    )
-
-
-# =========================
-# FSM
-# =========================
-class AddItemFlow(StatesGroup):
-    waiting_photo = State()
-
+async def safe_send_message(chat_id: int, text: str) -> None:
+    try:
+        await bot.send_message(chat_id, text)
+    except TelegramRetryAfter as e:
+        await asyncio.sleep(int(e.retry_after) + 1)
+        with suppress(Exception):
+            await bot.send_message(chat_id, text)
+    except Exception:
+        pass
 
-class AddressFlow(StatesGroup):
-    waiting_address = State()   
-class AddPhotoFlow(StatesGroup):
-    waiting_photo = State()
 
-# =========================
-# Bot
-# =========================
-bot = Bot(token=BOT_TOKEN)
-dp = Dispatcher(storage=MemoryStorage())
+async def notify_admin_reservation(res_id: int, buyer: Message | CallbackQuery | None = None) -> None:
+    r = get_reservation(res_id)
+    if not r:
+        return
 
+    items = reservation_items(res_id)
+    lines = [
+        f"📌 Новая бронь #{res_id}",
+        f"👤 user_id: {r['user_id']}",
+        f"📅 {r['day']} {r['slot']}",
+        f"⏳ До: {parse_iso(r['expires_at']).astimezone(TZ).strftime('%d.%m.%Y %H:%M')}",
+        "",
+        "Товары:",
+    ]
+    for it in items:
+        price = f" — {it['price']}" if it["price"] else ""
+        lines.append(f"• #{it['id']} {it['title']}{price}")
 
-async def background_cleanup():
-    while True:
-        try:
-            cleanup_expired()
-        except Exception:
-            pass
-        await asyncio.sleep(60)
+    text = "\n".join(lines)
 
+    targets = []
+    if ADMIN_CHAT_ID:
+        targets.append(ADMIN_CHAT_ID)
+    targets.extend(ADMIN_IDS)
 
-def parse_title_price(txt: str) -> Tuple[Optional[str], str]:
-    txt = (txt or "").strip()
-    if not txt:
-        return None, ""
-    if "|" in txt:
-        t, p = [x.strip() for x in txt.split("|", 1)]
-        return (t or None), (p or "")
-    return (txt.strip() or None), ""
+    for chat_id in set(targets):
+        await safe_send_message(chat_id, text)
 
-async def publish_item_to_channel(item_id: int, title: str, price: str, photo_id: Optional[str]):
-    print("=== POST TO CHANNEL: START ===", flush=True)
-    print("CHANNEL_USERNAME =", CHANNEL_USERNAME, flush=True)
-    print("item_id =", item_id, "has_photo =", bool(photo_id), flush=True)
 
-    try:
-        me = await bot.get_me()
-        bot_username = me.username or ""
+async def publish_item(item_id: int) -> None:
+    item = get_item(item_id)
+    if not item:
+        raise ValueError("item_not_found")
 
-        post_text = f"🛍 {title}"
-        if price:
-            post_text += f"\n💰 {price}"
-        post_text += f"\n\nℹ️ {SUPPORT_TEXT}"
+    me = await bot.get_me()
+    username = me.username or ""
+    text = item_text(item)
+    photos = get_item_photos(item_id)
 
-        kb = kb_item(item_id, bot_username)
+    if photos:
+        media = [InputMediaPhoto(media=p) for p in photos[:10]]
+        await bot.send_media_group(chat_id=CHANNEL_USERNAME, media=media)
 
-# ---- MULTI PHOTO BLOCK ----
-        photos = get_item_photos(item_id)
+    msg = await bot.send_message(
+        chat_id=CHANNEL_USERNAME,
+        text=text,
+        reply_markup=kb_item(item_id, username),
+    )
+    set_item_channel_message(item_id, msg.message_id)
 
-        if not photos and photo_id:
-            photos = [photo_id]
 
-        if photos:
-            media = [InputMediaPhoto(media=pid) for pid in photos[:10]]
-            await bot.send_media_group(
-            chat_id=CHANNEL_USERNAME,
-            media=media
-   )
-            await bot.send_message(
-            chat_id=CHANNEL_USERNAME,
-            text=post_text,
-            reply_markup=kb
-    )
+async def update_channel_post(item_id: int) -> None:
+    item = get_item(item_id)
+    if not item or not item["channel_message_id"]:
+        return
 
-        else:
-            await bot.send_message(
+    me = await bot.get_me()
+    username = me.username or ""
+    try:
+        await bot.edit_message_text(
             chat_id=CHANNEL_USERNAME,
-            text=post_text,
-            reply_markup=kb
-    )
+            message_id=int(item["channel_message_id"]),
+            text=item_text(item),
+            reply_markup=kb_item(item_id, username),
+        )
+    except TelegramBadRequest:
+        # post no longer editable -> publish a fresh one
+        await publish_item(item_id)
 
 
-        print("=== POST TO CHANNEL: OK ===", flush=True)
+def parse_csv_rows(content: str) -> list[CsvRow]:
+    reader = csv.DictReader(io.StringIO(content))
+    rows = []
+    for row in reader:
+        title = (row.get("title") or "").strip()
+        if not title:
+            continue
+        price = (row.get("price") or "").strip()
+        description = (row.get("description") or "").strip()
+        photos = parse_photos_field((row.get("photos") or row.get("photo") or "").strip())
+        rows.append(CsvRow(title=title, price=price, description=description, photos=photos))
+    return rows
 
-    except Exception as e:
-        import traceback
-        print("=== POST TO CHANNEL: FAIL ===", flush=True)
-        traceback.print_exc()
-        raise
 
 # =========================
-# Commands
+# User commands
 # =========================
 @dp.message(CommandStart())
 async def cmd_start(m: Message):
-    # поддерживаем deep link: /start checkout
     payload = ""
     if m.text:
         parts = m.text.split(maxsplit=1)
         if len(parts) == 2:
             payload = parts[1].strip()
 
     if payload == "checkout":
         await show_cart(m.from_user.id, m)
-        items = cart_list(m.from_user.id)
-        if items:
-            await m.answer("Нажми, чтобы оформить:", reply_markup=kb_checkout_from_start())
         return
 
     await m.answer(
-        "Привет! Тут корзина и бронирование товаров.\n\n"
-        "Добавляй товары из канала кнопкой 🛒\n"
-        "Открыть корзину: /cart\n\n"
+        "Привет! Добавляй товары из канала кнопкой 🛒\n"
+        "Команды:\n"
+        "/cart — корзина\n"
+        "/my — мои брони\n\n"
         f"{SUPPORT_TEXT}"
     )
 
+
+@dp.message(Command("cart"))
+async def cmd_cart(m: Message):
+    await show_cart(m.from_user.id, m)
+
+
+@dp.message(Command("my"))
+async def cmd_my(m: Message):
+    rows = user_reservations(m.from_user.id)
+    if not rows:
+        await m.answer("У тебя пока нет броней.")
+        return
+
+    lines = ["📦 Твои брони:"]
+    for r in rows:
+        lines.append(f"• #{r['id']} — {r['day']} {r['slot']} ({r['status']})")
+    lines.append("\nЧтобы отменить активную бронь: /cancel <id>")
+    await m.answer("\n".join(lines))
+
+
+@dp.message(Command("cancel"))
+async def cmd_cancel(m: Message):
+    parts = (m.text or "").split()
+    if len(parts) != 2 or not parts[1].isdigit():
+        await m.answer("Использование: /cancel <reservation_id>")
+        return
+    res_id = int(parts[1])
+    ok = cancel_reservation(m.from_user.id, res_id)
+    if not ok:
+        await m.answer("Не нашёл активную бронь с таким id.")
+        return
+    await m.answer("✅ Бронь отменена.")
+
+
+# =========================
+# Admin commands
+# =========================
 @dp.message(Command("photoid"))
 async def cmd_photoid(m: Message):
-    if m.from_user.id not in ADMIN_IDS:
-        await m.answer("⛔️ Только для админов.")
+    if not is_admin(m.from_user.id):
+        await m.answer("⛔ Только для админов")
         return
+    await m.answer("Пришли фото как фото (не документом) — верну photo_id.")
 
-    WAIT_PHOTO_ID.add(m.from_user.id)
-    await m.answer("Ок. Пришли ОДНО фото (как фото, не документом). Я пришлю photo_id.")
-    
-@dp.message(Command("csv"))
-async def cmd_csv(m: Message):
-    if m.from_user.id not in ADMIN_IDS:
-        await m.answer("⛔️ Только для админов.")
-        return
 
-    WAIT_CSV_IMPORT.add(m.from_user.id)
+@dp.message(Command("add"))
+async def cmd_add(m: Message):
+    if not is_admin(m.from_user.id):
+        await m.answer("⛔ Только для админов")
+        return
     await m.answer(
-        "Ок. Пришли CSV ФАЙЛОМ (как document).\n\n"
-        "Колонки: title, price, photo\n"
-        "photo — это photo_id (можно пусто)."
+        "Формат:\n"
+        "/add Название | Цена | Описание | photo_id1|photo_id2\n\n"
+        "Если фото нет, можно без последнего блока."
     )
+
+
+@dp.message(F.text.startswith("/add "))
+async def cmd_add_inline(m: Message):
+    if not is_admin(m.from_user.id):
+        return
+
+    payload = m.text[5:].strip()
+    parts = [x.strip() for x in payload.split("|")]
+    if len(parts) < 1 or not parts[0]:
+        await m.answer("Ошибка формата. Пример: /add Платье | 2500 | Отличное состояние | <photo_id>")
+        return
+
+    title = parts[0]
+    price = parts[1] if len(parts) > 1 else ""
+    description = parts[2] if len(parts) > 2 else ""
+    photos = parts[3:] if len(parts) > 3 else []
+    photos = [p.strip() for p in photos if p.strip()]
+
+    item_id = add_item(title=title, price=price, description=description, photos=photos)
+    try:
+        await publish_item(item_id)
+    except Exception as e:
+        await m.answer(f"Товар сохранён #{item_id}, но не удалось опубликовать: {type(e).__name__}: {e}")
+        return
+    await m.answer(f"✅ Опубликовано в канал. ID товара: #{item_id}")
+
+
 @dp.message(Command("addphoto"))
 async def cmd_addphoto(m: Message, state: FSMContext):
-    if m.from_user.id not in ADMIN_IDS:
-        await m.answer("Только для админов.")
+    if not is_admin(m.from_user.id):
         return
-
-    parts = m.text.split()
+    parts = (m.text or "").split()
     if len(parts) != 2 or not parts[1].isdigit():
         await m.answer("Использование: /addphoto <item_id>")
         return
-
     item_id = int(parts[1])
-
+    if not get_item(item_id):
+        await m.answer("Товар не найден")
+        return
     await state.set_state(AddPhotoFlow.waiting_photo)
     await state.update_data(item_id=item_id)
+    await m.answer(f"Пришли фото для товара #{item_id}")
+
 
-    await m.answer(f"Ок. Пришли фото для товара #{item_id}")
-    
 @dp.message(AddPhotoFlow.waiting_photo, F.photo)
 async def addphoto_receive(m: Message, state: FSMContext):
-    if m.from_user.id not in ADMIN_IDS:
+    if not is_admin(m.from_user.id):
         return
-
     data = await state.get_data()
-    item_id = data["item_id"]
-
+    item_id = int(data["item_id"])
     photo_id = m.photo[-1].file_id
-
-    # позиция = текущее количество фото
-    photos = get_item_photos(item_id)
-    pos = len(photos)
-
-    add_item_photo(item_id, photo_id, pos)
-
+    add_photo_to_item(item_id, photo_id)
     await state.clear()
     await m.answer(f"✅ Фото добавлено к товару #{item_id}")
 
-@dp.message(Command("addphoto"))
-async def cmd_addphoto(m: Message):
-    if m.from_user.id not in ADMIN_IDS:
-        return
 
+@dp.message(Command("edit"))
+async def cmd_edit(m: Message, state: FSMContext):
+    if not is_admin(m.from_user.id):
+        return
     parts = (m.text or "").split()
     if len(parts) != 2 or not parts[1].isdigit():
-        await m.answer("Используй: /addphoto <item_id>")
+        await m.answer("Использование: /edit <item_id>")
         return
-
     item_id = int(parts[1])
-    WAIT_PHOTO_ID.add((m.from_user.id, item_id))
+    item = get_item(item_id)
+    if not item:
+        await m.answer("Товар не найден")
+        return
+
+    await state.set_state(EditFlow.waiting)
+    await state.update_data(item_id=item_id)
+    await m.answer(
+        "Пришли новые данные в формате:\n"
+        "Название | Цена | Описание"
+    )
 
-    await m.answer("Пришли фото — добавлю к товару.")
 
-@dp.message(F.document)
-async def on_csv_document(m: Message):
-    if not m.document.file_name.lower().endswith(".csv"):
+@dp.message(EditFlow.waiting)
+async def edit_receive(m: Message, state: FSMContext):
+    if not is_admin(m.from_user.id):
+        return
+    title, price, description = parse_title_block(m.text or "")
+    if not title:
+        await m.answer("Неверный формат. Пример: Платье | 2000 | Новое описание")
         return
 
-    file = await bot.get_file(m.document.file_id)
-    data = await bot.download_file(file.file_path)
-    content = data.read().decode("utf-8")
+    data = await state.get_data()
+    item_id = int(data["item_id"])
+    update_item(item_id, title, price, description)
+    await state.clear()
+    await update_channel_post(item_id)
+    await m.answer(f"✅ Товар #{item_id} обновлён и пост в канале отредактирован.")
 
-    items = parse_csv_items(content)
 
-    ok = 0
-    fail = 0
+@dp.message(Command("sold"))
+async def cmd_sold(m: Message):
+    if not is_admin(m.from_user.id):
+        return
+    parts = (m.text or "").split()
+    if len(parts) != 2 or not parts[1].isdigit():
+        await m.answer("Использование: /sold <item_id>")
+        return
+    item_id = int(parts[1])
+    if not get_item(item_id):
+        await m.answer("Товар не найден")
+        return
+    set_item_status(item_id, "SOLD")
+    await update_channel_post(item_id)
+    await m.answer(f"✅ Товар #{item_id} отмечен как SOLD")
 
-    for title, price, photo_id in items:
-    try:
-        # создаём товар
-        item_id = add_item_to_db(
-            title=title,
-            price=price,
-            photo_id=photo_id
-        )
 
-        # если есть фото — кладём в таблицу фото
-        if photo_id:
-            add_item_photo(item_id=item_id, photo_id=photo_id, pos=0)
+@dp.message(Command("active"))
+async def cmd_active(m: Message):
+    if not is_admin(m.from_user.id):
+        return
+    parts = (m.text or "").split()
+    if len(parts) != 2 or not parts[1].isdigit():
+        await m.answer("Использование: /active <item_id>")
+        return
+    item_id = int(parts[1])
+    if not get_item(item_id):
+        await m.answer("Товар не найден")
+        return
+    set_item_status(item_id, "ACTIVE")
+    await update_channel_post(item_id)
+    await m.answer(f"✅ Товар #{item_id} снова ACTIVE")
 
-        # публикуем
-        await publish_item_to_channel(
-            item_id=item_id,
-            title=title,
-            price=price,
-            photo_id=photo_id
-        )
 
-        ok += 1
-        await asyncio.sleep(1.2)
+@dp.message(Command("csv"))
+async def cmd_csv(m: Message, state: FSMContext):
+    if not is_admin(m.from_user.id):
+        return
+    await state.set_state(CsvFlow.waiting_document)
+    await m.answer(
+        "Пришли CSV файлом (document).\n"
+        "Колонки: title,price,description,photos\n"
+        "photos: photo_id через |"
+    )
 
-    except Exception as e:
-        import traceback
-        print("CSV IMPORT FAIL:", title, price, photo_id, flush=True)
-        traceback.print_exc()
-        fail += 1
 
-        await m.answer(f"Готово. Добавлено: {ok}. Ошибок: {fail}.")
+@dp.message(CsvFlow.waiting_document, F.document)
+async def on_csv(m: Message, state: FSMContext):
+    if not is_admin(m.from_user.id):
+        return
+    if not (m.document.file_name or "").lower().endswith(".csv"):
+        await m.answer("Это не CSV файл")
+        return
 
-@dp.message(Command("cart"))
-async def cmd_cart(m: Message):
-    await show_cart(m.from_user.id, m)
+    file = await bot.get_file(m.document.file_id)
+    payload = await bot.download_file(file.file_path)
+    content = payload.read().decode("utf-8-sig")
 
-@dp.message(F.photo)
-async def on_photo_get_id(m: Message):
-    if m.from_user.id not in ADMIN_IDS:
+    rows = parse_csv_rows(content)
+    if not rows:
+        await m.answer("В CSV нет валидных строк")
+        await state.clear()
         return
 
-    # проверяем — ждём ли мы фото для addphoto
-    target = None
-    for pair in list(WAIT_PHOTO_ID):
-        if pair[0] == m.from_user.id:
-            target = pair
-            break
+    ok = 0
+    failed = 0
+    for row in rows:
+        try:
+            item_id = add_item(row.title, row.price, row.description, row.photos)
+            await publish_item(item_id)
+            ok += 1
+        except TelegramRetryAfter as e:
+            await asyncio.sleep(int(e.retry_after) + 1)
+            failed += 1
+        except Exception:
+            failed += 1
+        await asyncio.sleep(0.2)
 
-    if target:
-        WAIT_PHOTO_ID.discard(target)
-        _, item_id = target
+    await state.clear()
+    await m.answer(f"✅ Импорт завершён. Успешно: {ok}. Ошибок: {failed}.")
 
-        photo_id = m.photo[-1].file_id
-        add_item_photo(item_id=item_id, photo_id=photo_id, pos=99)
 
-        await m.answer(f"✅ Фото добавлено к товару #{item_id}")
+@dp.message(Command("block"))
+async def cmd_block(m: Message):
+    if not is_admin(m.from_user.id):
         return
-
-    # иначе — обычный режим: просто показать photo_id
-    if m.from_user.id in WAIT_PHOTO_ID:
-        photo_id = m.photo[-1].file_id
-        await m.answer(f"photo_id:\n{photo_id}")
-
-@dp.message(Command("add"))
-async def cmd_add(m: Message, state: FSMContext):
-    if not m.from_user or m.from_user.id not in ADMIN_IDS:
-        await m.answer("⛔ Только для админов.")
+    parts = (m.text or "").split()
+    if len(parts) < 3:
+        await m.answer("Использование: /block YYYY-MM-DD HH:MM [HH:MM] [причина]")
         return
 
-    await state.clear()
-    await m.answer(
-        "Ок. Пришли ОДНИМ сообщением: фото + подпись в формате:\n"
-        "<название> | <цена>\n\n"
-        "Пример:\n"
-        "Футболка белая | 2500 RSD\n\n"
-        "Если отправишь сначала текст — я попрошу фото."
-    )
-    await state.set_state(AddItemFlow.waiting_photo)
+    day_str = parts[1]
+    start_slot = parts[2]
+    end_slot = None
+    reason_start = 3
+    if len(parts) >= 4 and re.match(r"^\d{2}:\d{2}$", parts[3]):
+        end_slot = parts[3]
+        reason_start = 4
+    reason = " ".join(parts[reason_start:]).strip() or "Недоступно"
 
-@dp.message(AddItemFlow.waiting_photo)
-async def add_item_flow(m: Message, state: FSMContext):
-    if not m.from_user or m.from_user.id not in ADMIN_IDS:
-        await state.clear()
-        return
+    try:
+        slots = parse_slot_range(start_slot, end_slot)
+        added = block_slots(day_str, slots, reason, m.from_user.id)
+        await m.answer(f"✅ Заблокировано слотов: {added}")
+    except Exception as e:
+        await m.answer(f"Ошибка: {e}")
 
-    # 1) забираем текст из caption/text
-    txt = (m.caption or m.text or "").strip()
 
-    # 2) если пришёл текст без фото — запомним и попросим фото
-    if not m.photo:
-        title, price = parse_title_price(txt)
-        if not title:
-            await m.answer("Пусто. Пришли: <название> | <цена> (лучше сразу с фото).")
-            return
-        await state.update_data(title=title, price=price)
-        await m.answer("Ок, теперь пришли фото этого товара (можно без текста).")
+@dp.message(Command("unblock"))
+async def cmd_unblock(m: Message):
+    if not is_admin(m.from_user.id):
+        return
+    parts = (m.text or "").split()
+    if len(parts) < 3:
+        await m.answer("Использование: /unblock YYYY-MM-DD HH:MM [HH:MM]")
         return
 
-    # 3) если пришло фото — берём title/price из подписи, либо из state
-    title, price = parse_title_price(txt)
-    if not title:
-        data = await state.get_data()
-        title = data.get("title")
-        price = data.get("price", "")
+    day_str = parts[1]
+    start_slot = parts[2]
+    end_slot = parts[3] if len(parts) >= 4 and re.match(r"^\d{2}:\d{2}$", parts[3]) else None
+    try:
+        slots = parse_slot_range(start_slot, end_slot)
+        removed = unblock_slots(day_str, slots)
+        await m.answer(f"✅ Разблокировано слотов: {removed}")
+    except Exception as e:
+        await m.answer(f"Ошибка: {e}")
 
-    if not title:
-        await m.answer("К фото добавь подпись: <название> | <цена> (или сначала пришли текст, потом фото).")
+
+@dp.message(Command("slots"))
+async def cmd_slots(m: Message):
+    if not is_admin(m.from_user.id):
+        return
+    parts = (m.text or "").split()
+    if len(parts) != 2:
+        await m.answer("Использование: /slots YYYY-MM-DD")
         return
+    day_str = parts[1]
+    blocked = {row["slot"]: row["reason"] for row in day_blocks(day_str)}
 
-    photo_id = m.photo[-1].file_id
+    lines = [f"Слоты на {day_str}:"]
+    for slot in generate_slots():
+        if slot in blocked:
+            lines.append(f"• {slot} — 🚫 {blocked[slot]}")
+            continue
+        busy = reservation_slot_taken(day_str, slot)
+        lines.append(f"• {slot} — {'🔒 занят' if busy else '✅ свободен'}")
+    await m.answer("\n".join(lines))
 
-    item_id = add_item_to_db(title=title, price=price, photo_id=photo_id)
 
-    try:
-        await publish_item_to_channel(item_id, title, price, None)
-        await m.answer(f"✅ Опубликовано в канал. ID товара: #{item_id}")
-        await state.clear()
-    except Exception as e:
-        import traceback
-        traceback.print_exc()
-        await m.answer(f"❌ Не смог отправить в канал. Ошибка: {type(e).__name__}: {e}")
-        
-@dp.message(Command("import"))
-async def cmd_import(m: Message):
-    if m.from_user.id not in ADMIN_IDS:
-        await m.answer("⛔️ Только для админов.")
+@dp.message(F.photo)
+async def photo_id_echo(m: Message):
+    if not is_admin(m.from_user.id):
         return
-    await m.answer("Ок. Теперь пришли CSV-файл (items.csv) документом.")
+    await m.answer(f"photo_id:\n{m.photo[-1].file_id}")
+
 
 # =========================
-# Cart & Checkout
+# Cart & checkout callbacks
 # =========================
-async def show_cart(user_id: int, target):
+async def show_cart(user_id: int, target: Message):
     items = cart_list(user_id)
     if not items:
-        await target.answer("Корзина пуста. Добавляй товары из канала кнопкой 🛒")
+        await target.answer("Корзина пуста. Добавляй товары в канале кнопкой 🛒")
         return
 
     lines = ["🛒 Твоя корзина:"]
-    for it in items:
-        reserved = " (уже забронировано кем-то)" if item_is_reserved(int(it["id"])) else ""
-        price = it["price"] or ""
-        lines.append(f"• #{it['id']} — {it['title']} {('— ' + price) if price else ''}{reserved}")
-
+    for item in items:
+        price = f" — {item['price']}" if item["price"] else ""
+        lines.append(f"• #{item['id']} {item['title']}{price}")
     await target.answer("\n".join(lines), reply_markup=kb_cart(items))
 
 
+@dp.callback_query(F.data == "noop")
+async def cb_noop(c: CallbackQuery):
+    await c.answer()
+
+
 @dp.callback_query(F.data == "cart_show")
 async def cb_cart_show(c: CallbackQuery):
     await c.answer()
     await show_cart(c.from_user.id, c.message)
 
 
 @dp.callback_query(F.data.startswith("cart_add:"))
 async def cb_cart_add(c: CallbackQuery):
     await c.answer()
     item_id = int(c.data.split(":", 1)[1])
-    it = get_item(item_id)
-    if not it or int(it["is_active"]) != 1:
+    item = get_item(item_id)
+    if not item or item["status"] != "ACTIVE" or int(item["is_active"]) != 1:
         await c.message.answer("Этот товар недоступен.")
         return
     if item_is_reserved(item_id):
-        await c.message.answer("Этот товар уже забронирован кем-то.")
+        await c.message.answer("Этот товар уже забронирован.")
         return
-    ok = cart_add(c.from_user.id, item_id)
-    if ok:
-        await c.message.answer("✅ Добавлено в корзину. Открой /cart чтобы оформить.")
+
+    if cart_add(c.from_user.id, item_id):
+        await c.message.answer("✅ Добавлено в корзину. Открой /cart")
     else:
-        await c.message.answer("Не смог добавить (возможно уже в корзине).")
+        await c.message.answer("Не удалось добавить.")
 
 
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
-    await c.message.answer("🧹 Корзина очищена.")
-    await notify_admin_reservation(
-        res_id,
-        c.from_user.id,
-        day_str,
-        slot_str,
-        exp_str,
-        items
-)
-
+    await c.message.answer("🧹 Корзина очищена")
 
 
 @dp.callback_query(F.data == "checkout_start")
-async def cb_checkout_start(c: CallbackQuery, state: FSMContext):
+async def cb_checkout_start(c: CallbackQuery):
     await c.answer()
     items = cart_list(c.from_user.id)
     if not items:
-        await c.message.answer("Корзина пуста. Добавляй товары из канала 🛒")
-        return
-
-    bad = [it for it in items if item_is_reserved(int(it["id"]))]
-    if bad:
-        await c.message.answer("Некоторые товары уже забронированы кем-то. Удали их из корзины и попробуй снова.")
-        await show_cart(c.from_user.id, c.message)
+        await c.message.answer("Корзина пуста")
         return
-
-    await state.clear()
-    await c.message.answer(
-    "Самовывоз из Belgrade Waterfront\n\nВыбери день (следующие 5 дней):",
-    reply_markup=kb_days()
-)
+    await c.message.answer("Выбери день:", reply_markup=kb_days())
 
 
 @dp.callback_query(F.data.startswith("pick_day:"))
 async def cb_pick_day(c: CallbackQuery):
     await c.answer()
     day_str = c.data.split(":", 1)[1]
-    await c.message.answer(f"Выбери время на {day_str}:", reply_markup=kb_slots(day_str))
+    await c.message.answer(f"Выбери слот на {day_str}", reply_markup=kb_slots(day_str))
 
 
 @dp.callback_query(F.data.startswith("pick_slot:"))
-async def cb_pick_slot(c: CallbackQuery, state: FSMContext):
+async def cb_pick_slot(c: CallbackQuery):
     await c.answer()
-    _, day_str, slot_str = c.data.split(":", 2)
-
-    await state.update_data(day=day_str, time=slot_str)
-
+    _, day_str, slot = c.data.split(":", 2)
+    if reservation_slot_taken(day_str, slot):
+        await c.message.answer("Этот слот уже занят, выбери другой.")
+        await c.message.answer(f"Слоты на {day_str}:", reply_markup=kb_slots(day_str))
+        return
     await c.message.answer(
-        f"Подтверди бронь:\n📅 {day_str}\n🕒 {slot_str}\n\n",
-        reply_markup=kb_confirm(day_str, slot_str),
+        f"Подтверди бронь:\n📅 {day_str}\n🕒 {slot}",
+        reply_markup=kb_confirm(day_str, slot),
     )
-    
+
+
 @dp.callback_query(F.data.startswith("confirm:"))
-async def cb_confirm(c: CallbackQuery, state: FSMContext):
+async def cb_confirm(c: CallbackQuery):
     await c.answer()
-
-    _, day_str, slot_str = c.data.split(":", 2)
+    _, day_str, slot = c.data.split(":", 2)
 
     items = cart_list(c.from_user.id)
     if not items:
-        await c.message.answer("Корзина пуста. /cart")
+        await c.message.answer("Корзина пуста.")
         return
 
     bad = [it for it in items if item_is_reserved(int(it["id"]))]
     if bad:
-        await c.message.answer(
-            "Упс: кто-то уже забронировал часть товаров. Удали их из корзины и попробуй снова."
-        )
+        await c.message.answer("Часть товаров уже занята. Удали их из корзины и попробуй снова.")
         await show_cart(c.from_user.id, c.message)
         return
 
-    item_ids = [int(it["id"]) for it in items]
+    try:
+        res_id = create_reservation(c.from_user.id, day_str, slot, [int(it["id"]) for it in items])
+    except ValueError:
+        await c.message.answer("Слот уже занят/заблокирован, выбери другой.")
+        await c.message.answer(f"Слоты на {day_str}:", reply_markup=kb_slots(day_str))
+        return
 
-    # создаём бронь
-    res_id = create_reservation(c.from_user.id, day_str, slot_str, item_ids)
+    cart_clear(c.from_user.id)
     r = get_reservation(res_id)
-
-    # считаем красивое время окончания
     exp_str = parse_iso(r["expires_at"]).astimezone(TZ).strftime("%d.%m.%Y %H:%M")
 
-    # очищаем корзину\
-    cart_clear(c.from_user.id)
-    
-    await notify_admin_reservation(res_id, c.from_user.id, day_str, slot_str, exp_str, items)
-
-    # финальное сообщение — БЕЗ шага "введите адрес"
     await c.message.answer(
-        "✅ Бронь подтверждена.\n\n"
+        "✅ Бронь подтверждена\n\n"
         f"📅 {day_str}\n"
-        f"🕒 {slot_str}\n"
+        f"🕒 {slot}\n"
         f"⏳ Бронь до: {exp_str}\n\n"
-        "📍 Самовывоз из Belgrade Waterfront\n"
-        "Адрес: BW Sole. Bulevar Vudroa Vilsona, 17\n"
-        "Как подъедете, напишите в тг @liusene"
-)
-
-    
-@dp.message(AddressFlow.waiting_address)
-async def address_flow(m: Message, state: FSMContext):
-    address = (m.text or "").strip()
-
-    if not address:
-        await m.answer("Пришли адрес текстом одним сообщением.")
-        return
+        f"{PICKUP_TEXT}"
+    )
+    await notify_admin_reservation(res_id)
 
-    data = await state.get_data()
-    day = data.get("day")
-    time_str = data.get("time")
 
-    tz = ZoneInfo("Europe/Belgrade")
-    until = datetime.now(tz) + timedelta(hours=24)
+# =========================
+# Runtime
+# =========================
+async def background_cleanup() -> None:
+    while True:
+        with suppress(Exception):
+            cleanup_expired()
+        await asyncio.sleep(60)
 
-    until_str = until.strftime("%d.%m.%Y %H:%M")
 
-    await m.answer(
-        f"✅ Бронь создана\n\n"
-        f"📅 {day}\n"
-        f"⏰ {time_str}\n\n"
-        f"Бронь до: {until_str}\n\n"
-        f"📍 Самовывоз из Belgrade Waterfront\n"
-        f"Адрес: BW Sole. Bulevar Vudroa Vilsona, 17\n\n"
-        f"Как подъедете, напишите в тг @liusene"
-    )
-async def main():
+async def main() -> None:
     init_db()
-    print("=== DB INIT DONE ===", flush=True)
-    await dp.start_polling(bot)
+    cleaner = asyncio.create_task(background_cleanup())
+    try:
+        await dp.start_polling(bot)
+    finally:
+        cleaner.cancel()
+        with suppress(Exception):
+            await cleaner
+
 
 if __name__ == "__main__":
-    import asyncio
     asyncio.run(main())
 
EOF
)
