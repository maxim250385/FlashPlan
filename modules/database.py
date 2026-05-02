import os
import aiosqlite
from datetime import datetime

_ALLOWED_REMINDER_FIELDS = {'rem_7d', 'rem_3d', 'rem_1d', 'rem_2h'}
_ALLOWED_SETTINGS_FIELDS = {'rem_2h', 'rem_1d', 'rem_3d', 'rem_7d'}
_ALLOWED_VAL_FIELDS     = {'rem_2h_val', 'rem_1d_val', 'rem_3d_val', 'rem_7d_val'}

_DEFAULT_SETTINGS = {
    'rem_2h': 1, 'rem_1d': 1, 'rem_3d': 1, 'rem_7d': 1,
    'rem_2h_val': 2, 'rem_1d_val': 1, 'rem_3d_val': 3, 'rem_7d_val': 7,
}

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "events.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event_text TEXT,
                event_time TIMESTAMP,
                rem_7d BOOLEAN DEFAULT 0,
                rem_3d BOOLEAN DEFAULT 0,
                rem_1d BOOLEAN DEFAULT 0,
                rem_2h BOOLEAN DEFAULT 0,
                person TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                rem_2h INTEGER DEFAULT 1,
                rem_1d INTEGER DEFAULT 1,
                rem_3d INTEGER DEFAULT 1,
                rem_7d INTEGER DEFAULT 1,
                rem_2h_val INTEGER DEFAULT 2,
                rem_1d_val INTEGER DEFAULT 1,
                rem_3d_val INTEGER DEFAULT 3,
                rem_7d_val INTEGER DEFAULT 7
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS short_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                trigger_at TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS knowledge_base (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                content TEXT,
                category TEXT,
                tags TEXT,
                created_at TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS checklists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                list_name TEXT,
                item_text TEXT,
                created_at TIMESTAMP
            )
        ''')
        # Миграции для старых баз
        for col, default in [('rem_2h_val', 2), ('rem_1d_val', 1), ('rem_3d_val', 3), ('rem_7d_val', 7)]:
            try:
                await db.execute(f"ALTER TABLE user_settings ADD COLUMN {col} INTEGER DEFAULT {default}")
            except Exception:
                pass
        try:
            await db.execute("ALTER TABLE events ADD COLUMN person TEXT")
        except Exception:
            pass
        await db.commit()


async def add_event(user_id: int, text: str, dt: datetime, person: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO events (user_id, event_text, event_time, person) VALUES (?, ?, ?, ?)",
            (user_id, text, dt.strftime("%Y-%m-%d %H:%M:%S"), person)
        )
        await db.commit()


async def get_events_by_date(user_id: int, date_str: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, event_time, event_text, person FROM events WHERE user_id = ? AND date(event_time) = ?",
            (user_id, date_str)
        ) as cursor:
            return await cursor.fetchall()


async def get_event_days_in_month(user_id: int, year: int, month: int) -> set[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        month_str = f"{year:04d}-{month:02d}"
        async with db.execute(
            "SELECT DISTINCT CAST(strftime('%d', event_time) AS INTEGER) "
            "FROM events WHERE user_id = ? AND strftime('%Y-%m', event_time) = ?",
            (user_id, month_str)
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0] for row in rows}


async def delete_event(event_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        await db.commit()


async def get_pending_reminders():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM events WHERE event_time >= ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),)
        ) as cursor:
            return await cursor.fetchall()


async def mark_reminded(event_id: int, field: str):
    if field not in _ALLOWED_REMINDER_FIELDS:
        raise ValueError(f"Недопустимое поле напоминания: {field}")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE events SET {field} = 1 WHERE id = ?", (event_id,))
        await db.commit()


async def get_user_settings(user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT rem_2h, rem_1d, rem_3d, rem_7d, rem_2h_val, rem_1d_val, rem_3d_val, rem_7d_val "
            "FROM user_settings WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else dict(_DEFAULT_SETTINGS)


async def toggle_user_reminder(user_id: int, field: str) -> bool:
    if field not in _ALLOWED_SETTINGS_FIELDS:
        raise ValueError(f"Недопустимое поле: {field}")
    settings = await get_user_settings(user_id)
    currently_on = bool(settings[field])
    if currently_on:
        others_on = sum(1 for k in _ALLOWED_SETTINGS_FIELDS if k != field and settings[k])
        if others_on == 0:
            return False
    new_value = 0 if currently_on else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
        await db.execute(f"UPDATE user_settings SET {field} = ? WHERE user_id = ?", (new_value, user_id))
        await db.commit()
    return True


async def update_reminder_value(user_id: int, field: str, delta: int) -> int:
    val_field = f"{field}_val"
    if val_field not in _ALLOWED_VAL_FIELDS:
        raise ValueError(f"Недопустимое поле: {val_field}")
    settings = await get_user_settings(user_id)
    current = settings.get(val_field, _DEFAULT_SETTINGS[val_field])
    max_val = 24 if field == 'rem_2h' else 30
    new_val = max(1, min(max_val, current + delta))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
        await db.execute(f"UPDATE user_settings SET {val_field} = ? WHERE user_id = ?", (new_val, user_id))
        await db.commit()
    return new_val


# ── short_reminders ──────────────────────────────────────────────────────────

async def add_reminder(user_id: int, text: str, trigger_at: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO short_reminders (user_id, text, trigger_at) VALUES (?, ?, ?)",
            (user_id, text, trigger_at.strftime("%Y-%m-%d %H:%M:%S"))
        )
        await db.commit()


async def get_due_reminders(now_str: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM short_reminders WHERE trigger_at <= ?", (now_str,)
        ) as cursor:
            return await cursor.fetchall()


async def delete_reminder(reminder_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM short_reminders WHERE id = ?", (reminder_id,))
        await db.commit()


async def get_all_reminders(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM short_reminders WHERE user_id = ? ORDER BY trigger_at",
            (user_id,)
        ) as cursor:
            return await cursor.fetchall()


# ── knowledge_base ───────────────────────────────────────────────────────────

async def add_note(user_id: int, content: str, category: str, tags: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO knowledge_base (user_id, content, category, tags, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, content, category, tags, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        await db.commit()


async def update_note(user_id: int, category: str, new_content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE knowledge_base SET content = ? WHERE user_id = ? AND LOWER(category) = LOWER(?)",
            (new_content, user_id, category)
        )
        await db.commit()


async def get_notes_by_category(user_id: int, category: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM knowledge_base WHERE user_id = ? AND LOWER(category) LIKE LOWER(?)",
            (user_id, f"%{category}%")
        ) as cursor:
            return await cursor.fetchall()


async def delete_note_by_category(user_id: int, category: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM knowledge_base WHERE user_id = ? AND LOWER(category) LIKE LOWER(?)",
            (user_id, f"%{category}%")
        )
        await db.commit()


async def get_note_by_id(user_id: int, note_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM knowledge_base WHERE user_id = ? AND id = ?",
            (user_id, note_id)
        ) as cursor:
            return await cursor.fetchone()


async def delete_note_by_id(note_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM knowledge_base WHERE id = ?", (note_id,))
        await db.commit()


# ── checklists ───────────────────────────────────────────────────────────────

async def add_checklist_items(user_id: int, list_name: str, items: list):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        for item in items:
            await db.execute(
                "INSERT INTO checklists (user_id, list_name, item_text, created_at) VALUES (?, ?, ?, ?)",
                (user_id, list_name.lower(), item, now)
            )
        await db.commit()


async def get_checklist(user_id: int, list_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM checklists WHERE user_id = ? AND LOWER(list_name) LIKE LOWER(?) ORDER BY created_at",
            (user_id, f"%{list_name.lower()}%")
        ) as cursor:
            return await cursor.fetchall()


async def delete_checklist(user_id: int, list_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM checklists WHERE user_id = ? AND LOWER(list_name) LIKE LOWER(?)",
            (user_id, f"%{list_name.lower()}%")
        )
        await db.commit()


async def delete_checklist_item(user_id: int, list_name: str, item_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM checklists WHERE user_id = ? AND LOWER(list_name) LIKE LOWER(?) "
            "AND LOWER(item_text) LIKE LOWER(?)",
            (user_id, f"%{list_name.lower()}%", f"%{item_text.lower()}%")
        )
        await db.commit()
