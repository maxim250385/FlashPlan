import calendar
from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram_calendar import SimpleCalendar
from aiogram_calendar.schemas import SimpleCalendarCallback, SimpleCalAct, highlight, superscript

_BOLD_MAP = str.maketrans('0123456789', '𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗')


def _bold(text: str) -> str:
    return text.translate(_BOLD_MAP)


def _bold_underline(text: str) -> str:
    return f'({_bold(text)})'


class EventCalendar(SimpleCalendar):
    """SimpleCalendar с подсветкой дней с событиями жирными цифрами."""

    async def start_calendar(
        self,
        year: int = datetime.now().year,
        month: int = datetime.now().month,
        event_days: set = None,
    ) -> InlineKeyboardMarkup:
        event_days = event_days or set()
        today = datetime.now()
        now_weekday = self._labels.days_of_week[today.weekday()]
        now_month, now_year, now_day = today.month, today.year, today.day

        def highlight_month():
            s = self._labels.months[month - 1]
            return highlight(s) if now_month == month and now_year == year else s

        def highlight_weekday(weekday):
            return highlight(weekday) if (now_month == month and now_year == year and now_weekday == weekday) else weekday

        def format_day(day):
            dt = datetime(year, month, day)
            if self.min_date and dt < self.min_date:
                return superscript(str(day))
            if self.max_date and dt > self.max_date:
                return superscript(str(day))
            return _bold_underline(str(day)) if day in event_days else str(day)

        def format_day_highlighted(day):
            s = format_day(day)
            return highlight(s) if (now_month == month and now_year == year and now_day == day) else s

        kb = []

        # Строка года
        kb.append([
            InlineKeyboardButton(text="<<", callback_data=SimpleCalendarCallback(act=SimpleCalAct.prev_y, year=year, month=month, day=1).pack()),
            InlineKeyboardButton(text=str(year) if year != now_year else highlight(year), callback_data=self.ignore_callback),
            InlineKeyboardButton(text=">>", callback_data=SimpleCalendarCallback(act=SimpleCalAct.next_y, year=year, month=month, day=1).pack()),
        ])

        # Строка месяца
        kb.append([
            InlineKeyboardButton(text="<", callback_data=SimpleCalendarCallback(act=SimpleCalAct.prev_m, year=year, month=month, day=1).pack()),
            InlineKeyboardButton(text=highlight_month(), callback_data=self.ignore_callback),
            InlineKeyboardButton(text=">", callback_data=SimpleCalendarCallback(act=SimpleCalAct.next_m, year=year, month=month, day=1).pack()),
        ])

        # Дни недели
        kb.append([
            InlineKeyboardButton(text=highlight_weekday(d), callback_data=self.ignore_callback)
            for d in self._labels.days_of_week
        ])

        # Числа месяца
        day = 1
        for week in calendar.monthcalendar(year, month):
            row = []
            for day in week:
                if day == 0:
                    row.append(InlineKeyboardButton(text=" ", callback_data=self.ignore_callback))
                else:
                    row.append(InlineKeyboardButton(
                        text=format_day_highlighted(day),
                        callback_data=SimpleCalendarCallback(act=SimpleCalAct.day, year=year, month=month, day=day).pack(),
                    ))
            kb.append(row)

        # Нижняя строка: Закрыть / пусто / Сегодня
        kb.append([
            InlineKeyboardButton(text=self._labels.cancel_caption, callback_data=SimpleCalendarCallback(act=SimpleCalAct.cancel, year=year, month=month, day=day).pack()),
            InlineKeyboardButton(text=" ", callback_data=self.ignore_callback),
            InlineKeyboardButton(text=self._labels.today_caption, callback_data=SimpleCalendarCallback(act=SimpleCalAct.today, year=year, month=month, day=day).pack()),
        ])

        return InlineKeyboardMarkup(inline_keyboard=kb)

    async def _update_calendar(self, query: CallbackQuery, with_date: datetime):
        from .database import get_event_days_in_month
        event_days = await get_event_days_in_month(query.from_user.id, with_date.year, with_date.month)
        await query.message.edit_reply_markup(
            reply_markup=await self.start_calendar(with_date.year, with_date.month, event_days)
        )
