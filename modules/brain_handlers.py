import asyncio
import logging
import zoneinfo
from datetime import datetime
from io import BytesIO

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from .ai_handler import analyze_intent
from .database import (
    add_reminder,
    add_note, update_note, get_notes_by_category, delete_note_by_category,
    add_checklist_items, get_checklist, delete_checklist, delete_checklist_item,
)

brain_router = Router()
logger = logging.getLogger(__name__)
MSK_TZ = zoneinfo.ZoneInfo("Europe/Moscow")


class BrainStates(StatesGroup):
    waiting_confirmation = State()
    waiting_rerecord = State()
    waiting_delete = State()


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Согласен", callback_data="brain_confirm"),
            InlineKeyboardButton(text="🎤 Перезапись", callback_data="brain_rerecord"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="brain_cancel")],
    ])


def _close_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✕ Закрыть", callback_data="brain_close"),
    ]])


def _delete_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить", callback_data="brain_del_ok"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="brain_del_cancel"),
    ]])


async def _delete_after(bot: Bot, chat_id: int, delay: int, *msg_ids: int):
    await asyncio.sleep(delay)
    for msg_id in msg_ids:
        if msg_id:
            try:
                await bot.delete_message(chat_id, msg_id)
            except Exception:
                pass


def _format_checklist(items, list_name: str) -> str:
    if not items:
        return f"🛒 Список «{list_name}» пуст."
    lines = "\n".join(f"• {item['item_text']}" for item in items)
    return f"🛒 Список «{list_name}»:\n{lines}"


def _format_all_checklists(items) -> str:
    from collections import defaultdict
    grouped: dict[str, list] = defaultdict(list)
    for item in items:
        grouped[item['list_name']].append(item['item_text'])
    parts = []
    for name, texts in grouped.items():
        lines = "\n".join(f"• {t}" for t in texts)
        parts.append(f"🛒 Список «{name}»:\n{lines}")
    return "\n\n".join(parts)


async def dispatch_brain(msg: Message, parsed: dict, state: FSMContext, original_msg_id: int):
    intent = parsed.get("intent")
    params = parsed.get("params") or {}
    user_id = msg.chat.id

    # ── reminder ─────────────────────────────────────────────────────────────

    if intent == "reminder":
        trigger_str = params.get("trigger_at")
        text = params.get("text") or ""
        if not trigger_str:
            await msg.edit_text("Не смог рассчитать время. Попробуй иначе: «напомни через 2 часа...»")
            return
        try:
            dt = datetime.strptime(trigger_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK_TZ)
        except ValueError:
            await msg.edit_text("Некорректный формат времени от ИИ. Попробуй ещё раз.")
            return
        await state.update_data(action="reminder", trigger_at=trigger_str, text=text,
                                 original_msg_id=original_msg_id)
        await state.set_state(BrainStates.waiting_confirmation)
        await msg.edit_text(
            f"Поставить напоминание?\n⏰ {dt.strftime('%d.%m.%Y в %H:%M')}\n📝 {text}",
            reply_markup=_confirm_kb()
        )

    # ── note_add ──────────────────────────────────────────────────────────────

    elif intent == "note_add":
        content = params.get("content") or ""
        category = params.get("category") or "общее"
        await state.update_data(action="note_add", content=content, category=category,
                                 original_msg_id=original_msg_id)
        await state.set_state(BrainStates.waiting_confirmation)
        await msg.edit_text(
            f"Сохранить заметку?\n🏷 #{category}\n📝 {content}",
            reply_markup=_confirm_kb()
        )

    # ── note_edit ─────────────────────────────────────────────────────────────

    elif intent == "note_edit":
        content = params.get("content") or ""
        category = params.get("category") or ""
        await state.update_data(action="note_edit", content=content, category=category,
                                 original_msg_id=original_msg_id)
        await state.set_state(BrainStates.waiting_confirmation)
        await msg.edit_text(
            f"Обновить заметку?\n🏷 #{category}\n📝 {content}",
            reply_markup=_confirm_kb()
        )

    # ── note_query ────────────────────────────────────────────────────────────

    elif intent == "note_query":
        category = params.get("category") or ""
        notes = await get_notes_by_category(user_id, category)
        if not notes and category:
            notes = await get_notes_by_category(user_id, "")
        if not notes:
            await msg.edit_text("Заметок не найдено.", reply_markup=_close_kb())
        else:
            lines = "\n".join(f"🏷 #{n['category']}: {n['content']}" for n in notes)
            await msg.edit_text(lines, reply_markup=_close_kb())
        if original_msg_id:
            asyncio.create_task(_delete_after(msg.bot, msg.chat.id, 0, original_msg_id))

    # ── note_delete ───────────────────────────────────────────────────────────

    elif intent == "note_delete":
        category = params.get("category") or ""
        if not category:
            await msg.edit_text("Не понял что удалить. Уточни категорию заметки.", reply_markup=_close_kb())
            return
        await state.update_data(action="note_delete", category=category,
                                 original_msg_id=original_msg_id)
        await state.set_state(BrainStates.waiting_delete)
        await msg.edit_text(
            f"Удалить заметку «{category}»?",
            reply_markup=_delete_confirm_kb()
        )

    # ── checklist_add ─────────────────────────────────────────────────────────

    elif intent == "checklist_add":
        list_name = params.get("list_name") or "покупки"
        raw_items = params.get("items") or []
        items = raw_items if isinstance(raw_items, list) else [raw_items]
        items = [str(i) for i in items if i]
        if not items:
            await msg.edit_text("Не понял что добавить в список. Попробуй ещё раз.")
            return
        await state.update_data(action="checklist_add", list_name=list_name, items=items,
                                 original_msg_id=original_msg_id)
        await state.set_state(BrainStates.waiting_confirmation)
        items_text = "\n".join(f"• {item}" for item in items)
        await msg.edit_text(
            f"Добавить в список «{list_name}»?\n{items_text}",
            reply_markup=_confirm_kb()
        )

    # ── checklist_query ───────────────────────────────────────────────────────

    elif intent == "checklist_query":
        list_name = params.get("list_name") or ""
        items = await get_checklist(user_id, list_name)
        if not items and list_name:
            all_items = await get_checklist(user_id, "")
            text = _format_all_checklists(all_items) if all_items else "Списков нет."
            await msg.edit_text(text, reply_markup=_close_kb())
        else:
            await msg.edit_text(_format_checklist(items, list_name), reply_markup=_close_kb())
        if original_msg_id:
            asyncio.create_task(_delete_after(msg.bot, msg.chat.id, 0, original_msg_id))

    # ── checklist_delete ──────────────────────────────────────────────────────

    elif intent == "checklist_delete":
        list_name = params.get("list_name") or ""
        item_delete = params.get("item_delete")
        if not list_name:
            await msg.edit_text("Не понял какой список удалить. Уточни название.", reply_markup=_close_kb())
            return
        if item_delete:
            await state.update_data(action="checklist_item_delete", list_name=list_name,
                                     item_delete=str(item_delete), original_msg_id=original_msg_id)
            await state.set_state(BrainStates.waiting_delete)
            await msg.edit_text(
                f"Удалить «{item_delete}» из списка «{list_name}»?",
                reply_markup=_delete_confirm_kb()
            )
        else:
            await state.update_data(action="checklist_delete", list_name=list_name,
                                     original_msg_id=original_msg_id)
            await state.set_state(BrainStates.waiting_delete)
            await msg.edit_text(
                f"Очистить список «{list_name}»?\nВсе позиции будут удалены.",
                reply_markup=_delete_confirm_kb()
            )

    else:
        await msg.edit_text(
            "Не смог разобрать запрос. Попробуй переформулировать.",
            reply_markup=_close_kb()
        )
        if original_msg_id:
            asyncio.create_task(_delete_after(msg.bot, msg.chat.id, 0, original_msg_id))


# ── Подтверждение ─────────────────────────────────────────────────────────────

@brain_router.callback_query(F.data == "brain_confirm", BrainStates.waiting_confirmation)
async def brain_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    action = data.get("action")
    user_id = call.from_user.id

    if action == "reminder":
        try:
            dt = datetime.strptime(data["trigger_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK_TZ)
        except (ValueError, KeyError):
            await state.clear()
            await call.message.edit_text("Ошибка: некорректное время напоминания. Попробуй заново.", reply_markup=_close_kb())
            await call.answer()
            return
        await add_reminder(user_id, data["text"], dt)
        await call.message.edit_text(
            f"⏰ Напоминание установлено!\n📝 {data['text']}\n"
            f"🕐 {dt.strftime('%d.%m.%Y в %H:%M')}",
            reply_markup=_close_kb()
        )
        await state.clear()
        await call.answer()
        if data.get("original_msg_id"):
            asyncio.create_task(_delete_after(call.bot, call.message.chat.id, 0, data["original_msg_id"]))

    elif action == "note_add":
        await add_note(user_id, data["content"], data["category"])
        await call.message.edit_text(
            f"💾 Сохранено!\n🏷 #{data['category']}\n📝 {data['content']}",
            reply_markup=_close_kb()
        )
        await state.clear()
        await call.answer()
        if data.get("original_msg_id"):
            asyncio.create_task(_delete_after(call.bot, call.message.chat.id, 0, data["original_msg_id"]))

    elif action == "note_edit":
        await update_note(user_id, data["category"], data["content"])
        await call.message.edit_text(
            f"✏️ Обновлено!\n🏷 #{data['category']}\n📝 {data['content']}",
            reply_markup=_close_kb()
        )
        await state.clear()
        await call.answer()
        if data.get("original_msg_id"):
            asyncio.create_task(_delete_after(call.bot, call.message.chat.id, 0, data["original_msg_id"]))

    elif action == "checklist_add":
        list_name = data["list_name"]
        await add_checklist_items(user_id, list_name, data["items"])
        all_items = await get_checklist(user_id, list_name)
        await state.clear()
        await call.answer()
        await call.message.edit_text(
            _format_checklist(all_items, list_name),
            reply_markup=_close_kb()
        )
        if data.get("original_msg_id"):
            asyncio.create_task(_delete_after(call.bot, call.message.chat.id, 0, data["original_msg_id"]))

    else:
        await state.clear()
        await call.answer()


# ── Подтверждение удаления ────────────────────────────────────────────────────

@brain_router.callback_query(F.data == "brain_del_ok", BrainStates.waiting_delete)
async def brain_del_ok(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    action = data.get("action")
    user_id = call.from_user.id

    if action == "note_delete":
        await delete_note_by_category(user_id, data["category"])
        await call.message.edit_text(
            f"🗑 Заметка «{data['category']}» удалена.",
            reply_markup=_close_kb()
        )
    elif action == "checklist_delete":
        await delete_checklist(user_id, data["list_name"])
        await call.message.edit_text(
            f"🗑 Список «{data['list_name']}» очищен.",
            reply_markup=_close_kb()
        )
    elif action == "checklist_item_delete":
        await delete_checklist_item(user_id, data["list_name"], data["item_delete"])
        items = await get_checklist(user_id, data["list_name"])
        await call.message.edit_text(
            _format_checklist(items, data["list_name"]),
            reply_markup=_close_kb()
        )

    await state.clear()
    await call.answer()
    if data.get("original_msg_id"):
        asyncio.create_task(_delete_after(call.bot, call.message.chat.id, 0, data["original_msg_id"]))


@brain_router.callback_query(F.data == "brain_del_cancel", BrainStates.waiting_delete)
async def brain_del_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Удаление отменено.", reply_markup=_close_kb())
    await call.answer()


# ── Перезапись ────────────────────────────────────────────────────────────────

@brain_router.callback_query(F.data == "brain_rerecord", BrainStates.waiting_confirmation)
async def brain_rerecord(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="brain_cancel"),
    ]])
    await call.message.edit_text("🎤 Запишите голосовое заново.", reply_markup=kb)
    await state.set_state(BrainStates.waiting_rerecord)
    await call.answer()


@brain_router.message(BrainStates.waiting_rerecord, F.voice)
async def brain_rerecord_voice(message: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    original_msg_id = data.get("original_msg_id", message.message_id)
    msg = await message.answer("🎙 Слушаю и анализирую...")
    try:
        file = await bot.get_file(message.voice.file_id)
        audio_bytes = BytesIO()
        await bot.download_file(file.file_path, audio_bytes)
        audio_bytes.seek(0)
        parsed = await analyze_intent(audio_bytes=audio_bytes.read())
    except Exception as e:
        logger.error(f"Ошибка при перезаписи: {e}")
        await state.clear()
        await msg.edit_text("Не удалось обработать голосовое. Попробуй начать заново.", reply_markup=_close_kb())
        return
    if not parsed:
        await state.clear()
        await msg.edit_text("Не смог разобрать запрос. Попробуй начать заново.", reply_markup=_close_kb())
        return
    await dispatch_brain(msg, parsed, state, original_msg_id)


@brain_router.message(BrainStates.waiting_rerecord, ~F.voice, ~F.text.startswith('/'))
async def brain_rerecord_wrong(message: Message):
    await message.answer("Отправьте голосовое сообщение или /cancel для отмены.")


# ── Отмена ───────────────────────────────────────────────────────────────────

@brain_router.callback_query(F.data == "brain_cancel")
async def brain_cancel(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await call.message.edit_text("Действие отменено.", reply_markup=_close_kb())
    await call.answer()
    if data.get("original_msg_id"):
        asyncio.create_task(_delete_after(call.bot, call.message.chat.id, 3, data["original_msg_id"]))


# ── Закрыть ───────────────────────────────────────────────────────────────────

@brain_router.callback_query(F.data == "brain_close")
async def brain_close(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()


# ── Fallback для протухших кнопок ────────────────────────────────────────────

@brain_router.callback_query(F.data.in_({"brain_confirm", "brain_del_ok", "brain_rerecord", "brain_del_cancel"}))
async def brain_stale_callback(call: CallbackQuery):
    await call.answer("Действие устарело. Начните заново.", show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
