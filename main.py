"""FNF Chart Editor — Telegram-бот (v0.2).

v0.1: редактор чартов (одно сообщение-редактор, inline-кнопки).
v0.2: режим игры — сыграй свой чарт, тапая стрелки в ритм.
Спецификации: docs/chart-editor-bot-v0.1.md, docs/play-mode-v0.2.md
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
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
STEP_MS = 250          # длительность одной строки в чарте
WINDOW_ROWS = 15       # окно показа поля в редакторе

# --- Параметры режима игры ---
PLAY_STEP_MS = 750     # темп воспроизведения: одна строка = 750 мс
LEAD_MS = 1500         # пауза после "GO!" до первой строки
PERFECT_MS = 300       # окно "идеально" (± мс)
GOOD_MS = 700          # окно "хорошо" (± мс)
PLAY_WINDOW = 10       # сколько строк показывать вперёд
REDRAW_S = 1.5         # период перерисовки поля (лимиты Telegram)


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


@dataclass
class PlayNote:
    row: int
    lane: int
    t_ms: int
    hit: bool = False


@dataclass
class Game:
    chart: Chart
    notes: list[PlayNote]
    chat_id: int
    message_id: int
    bot: Bot
    start: float | None = None   # monotonic-время момента "GO!"
    perfect: int = 0
    good: int = 0
    stray: int = 0               # тапы мимо нот
    task: asyncio.Task | None = None


sessions: dict[int, Session] = {}
last_charts: dict[int, Chart] = {}
games: dict[int, Game] = {}


def format_time(row: int, step_ms: int) -> str:
    total_ms = row * step_ms
    minutes, rest = divmod(total_ms, 60_000)
    seconds, ms = divmod(rest, 1000)
    return f"{minutes:02d}:{seconds:02d}.{ms:03d}"


# ============================ Редактор ============================

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


def start_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="\U0001f3bc Новый чарт", callback_data="new")]]
    if user_id is not None and user_id in last_charts:
        rows.append(
            [InlineKeyboardButton(text="▶️ Играть последний чарт", callback_data="play")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def after_done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Играть", callback_data="play"),
                InlineKeyboardButton(text="\U0001f3bc Новый чарт", callback_data="new"),
            ]
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


# ============================ Игра ============================

def play_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=arrow, callback_data=f"hit:{lane}")
                for lane, arrow in enumerate(LANE_ARROWS)
            ],
            [InlineKeyboardButton(text="⏹ Стоп", callback_data="play_stop")],
        ]
    )


def build_notes(chart: Chart) -> list[PlayNote]:
    notes = [
        PlayNote(row=row, lane=lane, t_ms=LEAD_MS + row * PLAY_STEP_MS)
        for row, lanes in sorted(chart.notes.items())
        for lane in sorted(lanes)
    ]
    return notes


def render_play(game: Game, elapsed_ms: float) -> str:
    cur = int((elapsed_ms - LEAD_MS) // PLAY_STEP_MS)
    row_notes: dict[int, set[int]] = {}
    for n in game.notes:
        if not n.hit:
            row_notes.setdefault(n.row, set()).add(n.lane)

    lines = ["  " + "  ".join(LANE_ARROWS)]
    for row in range(cur, cur + PLAY_WINDOW):
        if 0 <= row < game.chart.rows:
            lanes = row_notes.get(row, set())
            cells = "  ".join("*" if l in lanes else "·" for l in range(LANES))
        else:
            cells = "  ".join(" " for _ in range(LANES))
        prefix = "▶" if row == cur else " "
        lines.append(f"{prefix} {cells}")
    missed = sum(
        1 for n in game.notes if not n.hit and elapsed_ms - n.t_ms > GOOD_MS
    )
    lines.append("─" * 14)
    lines.append(f"✨ {game.perfect}  ✅ {game.good}  ❌ {missed}")
    lines.append("Тапай стрелку, когда её нота на линии ▶")
    return "<pre>" + "\n".join(lines) + "</pre>"


async def safe_edit(game: Game, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    try:
        await game.bot.edit_message_text(
            text, chat_id=game.chat_id, message_id=game.message_id,
            reply_markup=keyboard,
        )
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
    except TelegramBadRequest:
        pass  # "message is not modified" и подобное — не ошибка


def rating(perfect: int, good: int, total: int) -> tuple[str, int]:
    if total == 0:
        return "—", 0
    acc = round(100 * (perfect + 0.5 * good) / total)
    for threshold, rank in ((95, "🌟 S"), (85, "🅰 A"), (70, "🅱 B"), (50, "🆑 C")):
        if acc >= threshold:
            return rank, acc
    return "💀 D", acc


async def finish_game(user_id: int, stopped: bool = False) -> None:
    game = games.pop(user_id, None)
    if game is None:
        return
    if game.task and not game.task.done():
        game.task.cancel()
    total = len(game.notes)
    missed = sum(1 for n in game.notes if not n.hit)
    rank, acc = rating(game.perfect, game.good, total)
    header = "⏹ Игра остановлена" if stopped else "🏁 Финиш!"
    await safe_edit(
        game,
        f"{header}\n\n"
        f"✨ Идеально: {game.perfect}\n"
        f"✅ Хорошо: {game.good}\n"
        f"❌ Мимо: {missed}\n"
        f"💨 Лишние тапы: {game.stray}\n\n"
        f"Точность: {acc}%\nРанг: {rank}",
        after_done_keyboard(),
    )


async def run_game(user_id: int) -> None:
    game = games[user_id]
    try:
        for n in (3, 2, 1):
            await safe_edit(game, f"<pre>▶️ Старт через {n}…</pre>", play_keyboard())
            await asyncio.sleep(1)
        game.start = time.monotonic()
        total_ms = LEAD_MS + game.chart.rows * PLAY_STEP_MS + GOOD_MS
        while True:
            elapsed = (time.monotonic() - game.start) * 1000
            if elapsed > total_ms:
                break
            await safe_edit(game, render_play(game, elapsed), play_keyboard())
            await asyncio.sleep(REDRAW_S)
        await finish_game(user_id)
    except asyncio.CancelledError:
        pass


def cancel_game(user_id: int) -> None:
    game = games.pop(user_id, None)
    if game and game.task and not game.task.done():
        game.task.cancel()


# ============================ Хендлеры ============================

dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Это редактор чартов FNF.\n"
        "Создай чарт — а потом сыграй в него!",
        reply_markup=start_keyboard(message.from_user.id),
    )


async def open_editor(bot: Bot, chat_id: int, user_id: int) -> None:
    cancel_game(user_id)
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
    sessions.pop(query.from_user.id, None)
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
    except TelegramBadRequest:
        pass  # редактирование без изменений — не ошибка
    await query.answer()


@dp.callback_query(F.data == "done")
async def cb_done(query: CallbackQuery) -> None:
    session = sessions.pop(query.from_user.id, None)
    if session is None:
        await query.answer()
        return
    chart = session.chart
    last_charts[query.from_user.id] = chart
    await query.message.edit_text(render(chart, session.cursor))
    await query.message.answer(
        f"✅ Чарт готов!\n"
        f"Строк: {chart.rows}\n"
        f"Нот: {chart.note_count()}\n"
        f"Длительность: {format_time(chart.rows, chart.step_ms)}\n\n"
        f"А теперь — сыграй в него! ▶️",
        reply_markup=after_done_keyboard(),
    )
    await query.answer()


@dp.callback_query(F.data == "play")
async def cb_play(query: CallbackQuery) -> None:
    user_id = query.from_user.id
    chart = last_charts.get(user_id)
    if chart is None:
        await query.answer("Сначала создай чарт 🎼", show_alert=True)
        return
    if chart.note_count() == 0:
        await query.answer("В чарте нет ни одной ноты — добавь их в редакторе", show_alert=True)
        return
    cancel_game(user_id)
    sent = await query.bot.send_message(
        query.message.chat.id, "<pre>▶️ Приготовься…</pre>",
        reply_markup=play_keyboard(),
    )
    game = Game(
        chart=chart,
        notes=build_notes(chart),
        chat_id=query.message.chat.id,
        message_id=sent.message_id,
        bot=query.bot,
    )
    games[user_id] = game
    game.task = asyncio.create_task(run_game(user_id))
    await query.answer()


@dp.callback_query(F.data.startswith("hit:"))
async def cb_hit(query: CallbackQuery) -> None:
    game = games.get(query.from_user.id)
    if game is None or game.start is None:
        await query.answer()
        return
    elapsed = (time.monotonic() - game.start) * 1000
    lane = int(query.data.split(":")[1])
    best: PlayNote | None = None
    for note in game.notes:
        if note.lane == lane and not note.hit:
            diff = abs(elapsed - note.t_ms)
            if diff <= GOOD_MS and (best is None or diff < abs(elapsed - best.t_ms)):
                best = note
    if best is None:
        game.stray += 1
        await query.answer("💨 мимо")
    elif abs(elapsed - best.t_ms) <= PERFECT_MS:
        best.hit = True
        game.perfect += 1
        await query.answer("✨ идеально!")
    else:
        best.hit = True
        game.good += 1
        await query.answer("✅ хорошо")


@dp.callback_query(F.data == "play_stop")
async def cb_play_stop(query: CallbackQuery) -> None:
    await finish_game(query.from_user.id, stopped=True)
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
