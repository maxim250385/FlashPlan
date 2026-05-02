import logging
import zoneinfo
from datetime import datetime, timedelta
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from .database import (
    get_pending_reminders, mark_reminded, get_user_settings,
    get_due_reminders, delete_reminder,
)

MSK_TZ = zoneinfo.ZoneInfo("Europe/Moscow")

logger = logging.getLogger(__name__)

_DEFAULTS = {'rem_2h_val': 2, 'rem_1d_val': 1, 'rem_3d_val': 3, 'rem_7d_val': 7}


def _plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 19:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


def _reminder_suffix(field: str, s: dict) -> str:
    val = s.get(f'{field}_val', _DEFAULTS[f'{field}_val'])
    if field == 'rem_2h':
        unit = _plural(val, 'час', 'часа', 'часов')
        return f"(Осталось {val} {unit}!)"
    if val == 1:
        return "(Завтра!)"
    unit = _plural(val, 'день', 'дня', 'дней')
    return f"(Осталось {val} {unit}!)"


async def check_reminders(bot: Bot):
    now = datetime.now(MSK_TZ)
    events = await get_pending_reminders()

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Понял, прочитал", callback_data="read_reminder")
    ]])

    settings_cache: dict[int, dict] = {}

    for e in events:
        try:
            event_time = datetime.strptime(e['event_time'], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK_TZ)
        except ValueError:
            continue

        if event_time < now:
            continue

        delta = event_time - now
        event_msg = f"🔔 Напоминание!\nСобытие: {e['event_text']}\nВремя: {event_time.strftime('%d.%m.%Y в %H:%M')}"

        user_id = e['user_id']
        if user_id not in settings_cache:
            settings_cache[user_id] = await get_user_settings(user_id)
        s = settings_cache[user_id]

        # Динамические пороги из настроек пользователя, от срочного к несрочному
        checks = [
            ('rem_2h', timedelta(hours=s.get('rem_2h_val', 2))),
            ('rem_1d', timedelta(days=s.get('rem_1d_val', 1))),
            ('rem_3d', timedelta(days=s.get('rem_3d_val', 3))),
            ('rem_7d', timedelta(days=s.get('rem_7d_val', 7))),
        ]

        field = None
        reminder_text = None
        for check_field, threshold in checks:
            if delta <= threshold and not e[check_field] and s[check_field]:
                field = check_field
                reminder_text = event_msg + f"\n{_reminder_suffix(check_field, s)}"
                break

        if field is None:
            continue

        try:
            await bot.send_message(user_id, reminder_text, reply_markup=kb)
            await mark_reminded(e['id'], field)
        except TelegramForbiddenError:
            logger.warning(f"Пользователь {user_id} заблокировал бота — напоминание пропущено.")
        except TelegramBadRequest as ex:
            logger.error(f"Ошибка отправки напоминания пользователю {user_id}: {ex}")


async def check_short_reminders(bot: Bot):
    now_str = datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    reminders = await get_due_reminders(now_str)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Понял", callback_data="brain_close")
    ]])

    for rem in reminders:
        try:
            await bot.send_message(
                rem['user_id'],
                f"🔔 *Напоминание:*\n{rem['text']}",
                reply_markup=kb,
                parse_mode="Markdown"
            )
            await delete_reminder(rem['id'])
        except TelegramForbiddenError:
            logger.warning(f"Пользователь {rem['user_id']} заблокировал бота — напоминание удалено.")
            await delete_reminder(rem['id'])
        except TelegramBadRequest as ex:
            logger.error(f"Ошибка отправки напоминания пользователю {rem['user_id']}: {ex}")
