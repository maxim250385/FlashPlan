import asyncio
import logging
import zoneinfo
from collections import defaultdict
from datetime import date, datetime
from io import BytesIO

from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from aiogram_calendar import SimpleCalendarCallback, CalendarLabels
from aiogram_calendar.schemas import SimpleCalAct

from .ai_handler import analyze_intent
from .brain_handlers import dispatch_brain
from .calendar_view import EventCalendar
from .database import (
    add_event, get_events_by_date, delete_event,
    get_user_settings, toggle_user_reminder, update_reminder_value,
    get_event_days_in_month,
    get_notes_by_category, get_checklist, get_all_reminders,
    get_note_by_id, delete_note_by_id,
)

router = Router()
logger = logging.getLogger(__name__)

_RU_LABELS = CalendarLabels(
    days_of_week=["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    months=["январь", "февраль", "март", "апрель", "май", "июнь",
            "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"],
    cancel_caption="Закрыть",
    today_caption="Сегодня",
)


def _make_calendar() -> EventCalendar:
    cal = EventCalendar()
    cal._labels = _RU_LABELS
    return cal


MSK_TZ = zoneinfo.ZoneInfo("Europe/Moscow")

_REPLY_KB = ReplyKeyboardMarkup(
    keyboard=[[
        KeyboardButton(text="📅 Календарь"),
        KeyboardButton(text="📝 Заметки"),
        KeyboardButton(text="⚙️ Настройки"),
    ]],
    resize_keyboard=True,
    is_persistent=True,
)

_START_TEXT = (
    "Привет! Я твой личный ассистент.\n\n"
    "Просто напиши или запиши голосовое — разберусь сам:\n"
    "📅 *Событие с датой* — попадёт в календарь\n"
    "⏰ *«Напомни через...»* — разовое напоминание\n"
    "📝 *Заметка* — фио, номер, пароль и т.д.\n"
    "🛒 *Список* — покупки и дела\n\n"
    "/help — список команд"
)

# (field, иконка, единица)
_REMINDER_CONFIG = [
    ('rem_2h', '⏰', 'ч'),
    ('rem_1d', '📅', 'д'),
    ('rem_3d', '📅', 'д'),
    ('rem_7d', '📅', 'д'),
]


class EventStates(StatesGroup):
    waiting_for_confirmation = State()
    waiting_for_rerecord = State()


def _close_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✕ Закрыть", callback_data="close_msg"),
    ]])


def _event_kb(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✓ OK", callback_data="ev_ok"),
        InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{event_id}"),
    ]])


def _event_confirm_del_kb(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delok_{event_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"delno_{event_id}"),
    ]])


async def _delete_after(bot: Bot, chat_id: int, delay: int, *msg_ids: int):
    await asyncio.sleep(delay)
    for msg_id in msg_ids:
        if msg_id:
            try:
                await bot.delete_message(chat_id, msg_id)
            except Exception:
                pass


async def build_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    s = await get_user_settings(user_id)
    enabled_count = sum(1 for field, _, _ in _REMINDER_CONFIG if s[field])

    buttons = []
    for field, icon, unit in _REMINDER_CONFIG:
        is_on = bool(s[field])
        is_last = is_on and enabled_count == 1
        val = s[f'{field}_val']
        status = '✅🔒' if is_last else ('✅' if is_on else '—')
        middle = f"{icon} {val}{unit}  {status}"
        buttons.append([
            InlineKeyboardButton(text="−", callback_data=f"dec_{field}"),
            InlineKeyboardButton(text=middle, callback_data=f"toggle_{field}"),
            InlineKeyboardButton(text="+", callback_data=f"inc_{field}"),
        ])

    buttons.append([InlineKeyboardButton(text="✕ Закрыть", callback_data="close_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _send_calendar(chat_id: int, bot: Bot, user_id: int, year: int = None, month: int = None):
    today = date.today()
    year = year or today.year
    month = month or today.month
    event_days = await get_event_days_in_month(user_id, year, month)
    kb = await _make_calendar().start_calendar(year, month, event_days)
    await bot.send_message(chat_id, "Выберите дату:", reply_markup=kb)


# ──────────────────────────── Команды ────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(_START_TEXT, reply_markup=_REPLY_KB, parse_mode="Markdown")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "*Команды:*\n"
        "/start — начало работы\n"
        "/cancel — отменить текущее действие\n\n"
        "*Примеры:*\n"
        "«Встреча с Колей 15 мая в 10:00» → 📅 календарь\n"
        "«Напомни через 2 часа выключить плиту» → ⏰ напоминание\n"
        "«Фио доктора Иванов Иван» → 📝 заметка\n"
        "«Купить молоко, хлеб, масло» → 🛒 список\n"
        "«Фио доктора напомни» → показывает заметку\n"
        "«Добавь колбасу в покупки» → добавляет в список\n"
        "«Удали список покупки» → удаляет список\n\n"
        "*Календарь, заметки и настройки:*\n"
        "Кнопки на панели внизу.",
        parse_mode="Markdown"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    await state.clear()
    await message.answer("Действие отменено." if current else "Нечего отменять.")


# ──────────────────────────── Кнопки панели ────────────────────────────

@router.message(F.text == "📅 Календарь", StateFilter(None))
async def kb_calendar(message: Message, bot: Bot):
    await message.delete()
    await _send_calendar(message.chat.id, bot, message.from_user.id)


async def _build_notes_content(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    notes = await get_notes_by_category(user_id, "")
    checklist_items = await get_checklist(user_id, "")
    reminders = await get_all_reminders(user_id)

    parts = []
    buttons = []

    if notes:
        lines = "\n".join(f"🏷 #{n['category']}: {n['content']}" for n in notes)
        parts.append(f"📝 *Заметки:*\n{lines}")
        for n in notes:
            buttons.append([InlineKeyboardButton(
                text=f"🗑 #{n['category']}",
                callback_data=f"del_note_{n['id']}",
            )])

    if checklist_items:
        grouped: dict = defaultdict(list)
        for item in checklist_items:
            grouped[item['list_name']].append(item['item_text'])
        cl_parts = []
        for name, texts in grouped.items():
            cl_parts.append(f"🛒 Список «{name}»:\n" + "\n".join(f"• {t}" for t in texts))
        parts.append("*Списки:*\n" + "\n\n".join(cl_parts))

    if reminders:
        lines = "\n".join(
            f"⏰ {r['trigger_at'][:16].replace('-', '.')} — {r['text']}"
            for r in reminders
        )
        parts.append(f"*Напоминания:*\n{lines}")

    text = "\n\n".join(parts) if parts else "Заметок, напоминаний и списков пока нет."
    buttons.append([InlineKeyboardButton(text="✕ Закрыть", callback_data="close_msg")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(F.text == "📝 Заметки", StateFilter(None))
async def kb_notes(message: Message, bot: Bot):
    await message.delete()
    text, kb = await _build_notes_content(message.from_user.id)
    await bot.send_message(message.chat.id, text, reply_markup=kb, parse_mode="Markdown")


@router.message(F.text == "⚙️ Настройки", StateFilter(None))
async def kb_settings(message: Message):
    await message.delete()
    kb = await build_settings_keyboard(message.from_user.id)
    await message.answer(
        "⚙️ *Настройки напоминаний*\n\n"
        "— / + меняют значение (ч = часы, д = дни)\n"
        "Нажмите на значение чтобы включить/выключить\n"
        "🔒 — последнее активное, нельзя выключить",
        reply_markup=kb, parse_mode="Markdown"
    )


# ──────────────────────────── Закрыть ────────────────────────────

@router.callback_query(F.data == "close_msg")
async def close_msg_cb(call: CallbackQuery):
    await call.message.delete()
    await call.answer()


# ──────────────────────────── Настройки ────────────────────────────

@router.callback_query(F.data.startswith("toggle_rem_"))
async def toggle_reminder_handler(call: CallbackQuery):
    field = call.data[len("toggle_"):]
    toggled = await toggle_user_reminder(call.from_user.id, field)
    if not toggled:
        await call.answer("Должно быть активно хотя бы одно напоминание!", show_alert=True)
        return
    kb = await build_settings_keyboard(call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("dec_rem_"))
async def dec_reminder_val(call: CallbackQuery):
    field = call.data[len("dec_"):]
    await update_reminder_value(call.from_user.id, field, -1)
    kb = await build_settings_keyboard(call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("inc_rem_"))
async def inc_reminder_val(call: CallbackQuery):
    field = call.data[len("inc_"):]
    await update_reminder_value(call.from_user.id, field, +1)
    kb = await build_settings_keyboard(call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "close_settings")
async def close_settings(call: CallbackQuery):
    await call.message.delete()
    await call.answer()


# ──────────────────────────── Удаление заметок ────────────────────────────

@router.callback_query(F.data.regexp(r'^del_note_\d+$'), StateFilter(None))
async def del_note_ask(call: CallbackQuery):
    note_id = int(call.data[len("del_note_"):])
    note = await get_note_by_id(call.from_user.id, note_id)
    if not note:
        await call.answer("Заметка не найдена.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"del_note_ok_{note_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="del_note_cancel"),
    ]])
    await call.message.edit_text(
        f"Точно удалить заметку?\n🏷 #{note['category']}: {note['content']}",
        reply_markup=kb, parse_mode="Markdown"
    )
    await call.answer()


@router.callback_query(F.data.regexp(r'^del_note_ok_\d+$'), StateFilter(None))
async def del_note_confirm(call: CallbackQuery):
    note_id = int(call.data[len("del_note_ok_"):])
    await delete_note_by_id(note_id)
    text, kb = await _build_notes_content(call.from_user.id)
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await call.answer("🗑 Удалено")


@router.callback_query(F.data == "del_note_cancel", StateFilter(None))
async def del_note_cancel(call: CallbackQuery):
    text, kb = await _build_notes_content(call.from_user.id)
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await call.answer()


# ──────────────────────────── Добавление событий ────────────────────────────

@router.message(F.voice, StateFilter(None))
async def handle_voice(message: Message, bot: Bot, state: FSMContext):
    await state.update_data(original_msg_id=message.message_id)
    msg = await message.answer("🎙 Слушаю и анализирую...")
    try:
        file = await bot.get_file(message.voice.file_id)
        audio_bytes = BytesIO()
        await bot.download_file(file.file_path, audio_bytes)
        audio_bytes.seek(0)
        parsed = await analyze_intent(audio_bytes=audio_bytes.read())
    except Exception as e:
        logger.error(f"Ошибка при обработке голосового: {e}")
        await msg.edit_text("Не удалось обработать голосовое. Попробуй ещё раз.")
        return
    if not parsed:
        await msg.edit_text("Не смог разобрать запрос. Попробуй ещё раз.")
        return
    await _route_intent(msg, parsed, state, message.message_id)


@router.message(F.text & ~F.text.startswith('/'), StateFilter(None))
async def handle_text(message: Message, state: FSMContext):
    await state.update_data(original_msg_id=message.message_id)
    msg = await message.answer("🧠 Анализирую...")
    parsed = await analyze_intent(text=message.text)
    if not parsed:
        await msg.edit_text("Не смог разобрать запрос. Попробуй переформулировать.")
        return
    await _route_intent(msg, parsed, state, message.message_id)


async def _route_intent(msg: Message, parsed: dict, state: FSMContext, original_msg_id: int):
    if parsed.get("intent") == "calendar_event":
        await process_parsed_data(msg, parsed.get("params") or {}, state)
    else:
        await dispatch_brain(msg, parsed, state, original_msg_id)


async def process_parsed_data(msg: Message, params: dict, state: FSMContext):
    if not params or not params.get('date') or not params.get('time'):
        await msg.edit_text("Не смог разобрать дату и время. Попробуй переформулировать.")
        return

    dt_str = f"{params['date']} {params['time']}"
    text = params.get('event', 'Событие')
    person = params.get('person') or None

    await state.update_data(dt_str=dt_str, text=text, person=person)
    await state.set_state(EventStates.waiting_for_confirmation)

    person_line = f"👤 {person}\n" if person else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Согласен", callback_data="confirm_event"),
            InlineKeyboardButton(text="🎤 Перезапись", callback_data="rerecord_event"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_event")],
    ])
    dt_display = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").strftime('%d.%m.%Y в %H:%M')
    await msg.edit_text(f"Я понял так:\n{person_line}📅 {dt_display}\n📝 {text}\nВсё верно?", reply_markup=kb)


@router.message(EventStates.waiting_for_confirmation, ~F.text.startswith('/'))
async def confirmation_fallback(message: Message):
    await message.answer("Нажмите ✅ или 🎤 выше, либо /cancel для отмены.")


@router.callback_query(F.data == "confirm_event", EventStates.waiting_for_confirmation)
async def confirm_event(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    dt = datetime.strptime(data['dt_str'], "%Y-%m-%d %H:%M").replace(tzinfo=MSK_TZ)
    await add_event(call.from_user.id, data['text'], dt, person=data.get('person'))
    await call.message.edit_text("✅ Событие сохранено!", reply_markup=_close_kb())
    await state.clear()
    await call.answer()
    if data.get('original_msg_id'):
        asyncio.create_task(_delete_after(call.bot, call.message.chat.id, 0, data['original_msg_id']))


@router.callback_query(F.data == "rerecord_event", EventStates.waiting_for_confirmation)
async def rerecord_event(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_event"),
    ]])
    await call.message.edit_text("🎤 Запишите голосовое заново.", reply_markup=kb)
    await state.set_state(EventStates.waiting_for_rerecord)
    await call.answer()


@router.callback_query(F.data == "cancel_event")
async def cancel_event(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await call.message.edit_text("Действие отменено.", reply_markup=_close_kb())
    await call.answer()
    if data.get("original_msg_id"):
        asyncio.create_task(_delete_after(call.bot, call.message.chat.id, 3, data["original_msg_id"]))


@router.message(EventStates.waiting_for_rerecord, F.voice)
async def rerecord_voice(message: Message, bot: Bot, state: FSMContext):
    await state.update_data(original_msg_id=message.message_id)
    msg = await message.answer("🎙 Слушаю и анализирую...")
    try:
        file = await bot.get_file(message.voice.file_id)
        audio_bytes = BytesIO()
        await bot.download_file(file.file_path, audio_bytes)
        audio_bytes.seek(0)
        parsed = await analyze_intent(audio_bytes=audio_bytes.read())
    except Exception as e:
        logger.error(f"Ошибка при перезаписи голосового: {e}")
        await state.clear()
        await msg.edit_text("Не удалось обработать голосовое. Попробуй начать заново.", reply_markup=_close_kb())
        return
    if not parsed:
        await state.clear()
        await msg.edit_text("Не смог разобрать запрос. Попробуй начать заново.", reply_markup=_close_kb())
        return
    await process_parsed_data(msg, parsed.get("params") or {}, state)


@router.message(EventStates.waiting_for_rerecord, ~F.voice, ~F.text.startswith('/'))
async def rerecord_wrong_type(message: Message):
    await message.answer("Отправьте голосовое сообщение или /cancel для отмены.")


# ──────────────────────────── Календарь ────────────────────────────

@router.callback_query(SimpleCalendarCallback.filter())
async def cv_calendar_action(call: CallbackQuery, callback_data: SimpleCalendarCallback):
    if callback_data.act == SimpleCalAct.cancel:
        await call.message.delete()
        await call.answer()
        return

    selected, selected_date = await _make_calendar().process_selection(call, callback_data)

    if selected:
        year, month, day = selected_date.year, selected_date.month, selected_date.day
        date_str = f"{year}-{month:02d}-{day:02d}"
        events = await get_events_by_date(call.from_user.id, date_str)

        if not events:
            await call.answer(f"На {day:02d}.{month:02d}.{year} событий нет.", show_alert=True)
            return

        for e in events:
            person_line = f"👤 {e[3]}\n" if e[3] else ""
            dt_display = datetime.strptime(e[1], "%Y-%m-%d %H:%M:%S").strftime('%d.%m.%Y в %H:%M')
            await call.message.answer(
                f"{person_line}🕒 {dt_display}\n📝 {e[2]}",
                reply_markup=_event_kb(e[0])
            )
        asyncio.create_task(_delete_after(call.bot, call.message.chat.id, 5, call.message.message_id))


@router.callback_query(F.data == "ev_ok")
async def ev_ok(call: CallbackQuery):
    await call.message.delete()
    await call.answer()


@router.callback_query(F.data.regexp(r'^del_\d+$'))
async def del_event_ask(call: CallbackQuery):
    try:
        event_id = int(call.data.split("_")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный запрос.", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=_event_confirm_del_kb(event_id))
    await call.answer()


@router.callback_query(F.data.startswith("delok_"))
async def del_event_ok(call: CallbackQuery):
    try:
        event_id = int(call.data.split("_")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный запрос.", show_alert=True)
        return
    await delete_event(event_id)
    await call.message.edit_text("🗑 Событие удалено.", reply_markup=_close_kb())
    await call.answer()


@router.callback_query(F.data.startswith("delno_"))
async def del_event_no(call: CallbackQuery):
    try:
        event_id = int(call.data.split("_")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный запрос.", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=_event_kb(event_id))
    await call.answer()


@router.callback_query(F.data == "read_reminder")
async def read_reminder(call: CallbackQuery):
    await call.message.delete()
    await call.answer()
