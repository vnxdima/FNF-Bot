"""FNF Chart Editor — Telegram-бот (v0.1).

Редактор чартов по ТЗ docs/chart-editor-bot-v0.1.md:
одно сообщение-редактор, inline-кнопки, чарт как структура данных.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

LANES = 4
LANE_ARROWS = ["←", "↓", "↑", "→"]
STEP_MS = 250          # длительность одной строки
WINDOW_ROWS = 15       # сколько строк поля показывать вокруг курсора


@dataclass
class Chart:
    step_ms: int = STEP_MS
    rows: int = 1
    # notes[row] -> множество занятых дорожек (0..3)
    notes: dict[int, set[int]] = field(default_factory=dict)

    def toggle(self, row: int, lane: int) -> None:
        lanes = self.notes.setdefault(row, set())
        if lane in lanes:
            lanes.discard(lane)
            if not lanes:
                del self.notes[row]
        else:
            lanes.add(lane)

    def clear_row(self, row: int) -> None:
        self.notes.pop(row, None)

    def note_count(self) -> int:
        return sum(len(lanes) for lanes in self.notes.values())


@dataclass
class Session:
    chart: Chart = field(default_factory=Chart)
    cursor: int = 0
    message_id: int | None = None


sessions: dict[int, Session] = {}


def format_time(row: int, step_ms: int) -> str:
    total_ms = row * step_ms
    minutes, rest = divmod(total_ms, 60_000)
    seconds, ms = divmod(rest, 1000)
    return f"{minutes:02d}:{seconds:02d}.{ms:03d}"


def render(chart: Chart, cursor: int) -> str:
    """Чистая функция отрисовки: чарт + курсор -> текст сообщения."""
    half = WINDOW_ROWS // 2
    start = max(0, min(cursor - half, chart.rows - WINDOW_ROWS))
    end = min(chart.rows, start + WINDOW_ROWS)

    lines = ["  " + "  ".join(LANE_ARROWS)]
    for row in range(start, end):
        lanes = chart.notes.get(row, set())
        cells = "  ".join("*" if lane in lanes else "·" for lane in range(LANES))
        prefix = "▶" if row == cursor else " "
        lines.append(f"{prefix} {cells}")
    lines.append("─" * 14)
    lines.append(f"Строка {cursor + 1} / {chart.rows}")
    lines.append(f"⏱ {format_time(cursor, chart.step_ms)}")
    return "<pre>" + "\n".join(lines) + "</pre>"


def editor_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=arrow, callback_data=f"lane:{lane}")
                for lane, arrow in enumerate(LANE_ARROWS)
            ],
            [
                InlineKeyboardButton(text="▲ Назад", callback_data="up"),
                InlineKeyboardButton(text="▼ Далее", callback_data="down"),
            ],
            [
                InlineKeyboardButton(text="\U0001f5d1 Строка", callback_data="clear"),
                InlineKeyboardButton(text="✅ Готово", callback_data="done"),
            ],
        ]
    )


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f3bc Новый чарт", callback_data="new")]
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, сбросить", callback_data="new_confirm"),
                InlineKeyboardButton(text="Отмена", callback_data="new_cancel"),
            ]
        ]
    )


dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Это редактор чартов FNF.\n"
        "Нажми кнопку, чтобы начать новый чарт.",
        reply_markup=start_keyboard(),
    )


async def open_editor(bot: Bot, chat_id: int, user_id: int) -> None:
    session = Session()
    sessions[user_id] = session
    sent = await bot.send_message(
        chat_id,
        render(session.chart, session.cursor),
        reply_markup=editor_keyboard(),
    )
    session.message_id = sent.message_id


@dp.callback_query(F.data == "new")
async def cb_new(query: CallbackQuery) -> None:
    if query.from_user.id in sessions:
        await query.message.answer(
            "У тебя уже есть активный чарт. Сбросить и начать новый?",
            reply_markup=confirm_keyboard(),
        )
    else:
        await open_editor(query.bot, query.message.chat.id, query.from_user.id)
    await query.answer()


@dp.callback_query(F.data == "new_confirm")
async def cb_new_confirm(query: CallbackQuery) -> None:
    await query.message.delete()
    await open_editor(query.bot, query.message.chat.id, query.from_user.id)
    await query.answer()


@dp.callback_query(F.data == "new_cancel")
async def cb_new_cancel(query: CallbackQuery) -> None:
    await query.message.delete()
    await query.answer()


@dp.callback_query(F.data.startswith("lane:"))
async def cb_lane(query: CallbackQuery) -> None:
    session = sessions.get(query.from_user.id)
    if session is None:
        await query.answer("Сессия не найдена — нажми /start", show_alert=True)
        return
    session.chart.toggle(session.cursor, int(query.data.split(":")[1]))
    await refresh(query, session)


@dp.callback_query(F.data.in_({"up", "down", "clear"}))
async def cb_move(query: CallbackQuery) -> None:
    session = sessions.get(query.from_user.id)
    if session is None:
        await query.answer("Сессия не найдена — нажми /start", show_alert=True)
        return
    if query.data == "up":
        if session.cursor == 0:
            await query.answer()
            return
        session.cursor -= 1
    elif query.data == "down":
        session.cursor += 1
        if session.cursor >= session.chart.rows:
            session.chart.rows = session.cursor + 1
    elif query.data == "clear":
        session.chart.clear_row(session.cursor)
    await refresh(query, session)


async def refresh(query: CallbackQuery, session: Session) -> None:
    try:
        await query.message.edit_text(
            render(session.chart, session.cursor),
            reply_markup=editor_keyboard(),
        )
    except Exception:
        # Telegram отвергает редактирование без изменений — это не ошибка
        pass
    await query.answer()


@dp.callback_query(F.data == "done")
async def cb_done(query: CallbackQuery) -> None:
    session = sessions.pop(query.from_user.id, None)
    if session is None:
        await query.answer()
        return
    chart = session.chart
    await query.message.edit_text(render(chart, session.cursor))
    await query.message.answer(
        f"✅ Чарт готов!\n"
        f"Строк: {chart.rows}\n"
        f"Нот: {chart.note_count()}\n"
        f"Длительность: {format_time(chart.rows, chart.step_ms)}",
        reply_markup=start_keyboard(),
    )
    await query.answer()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан (создай файл .env)")
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
