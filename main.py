"""FNF Chart Editor — Telegram-бот (v0.2).

v0.1: редактор чартов (одно сообщение-редактор, inline-кнопки).
v0.2: режим игры — сыграй свой чарт, тапая стрелки в ритм.
Спецификации: docs/chart-editor-bot-v0.1.md, docs/play-mode-v0.2.md
"""

import asyncio
import logging
import os
import random
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
# Цвета дорожек как в FNF: ← фиолетовый, ↓ синий, ↑ зелёный, → красный
LANE_COLORS = ["🟣", "🔵", "🟢", "🔴"]
EMPTY_CELL = "⚫"
STEP_MS = 250          # длительность одной строки в чарте
WINDOW_ROWS = 15       # окно показа поля в редакторе

# --- Параметры режима игры: (строка_мс, лид_мс, идеально_мс, хорошо_мс) ---
# 📱 телефон: пальцы на всех кнопках; 🖥 комп: курсор надо доводить мышкой
PLAY_SPEEDS = {
    "mobile": (1000, 2000, 300, 700),
    "desktop": (2500, 3000, 900, 1900),
}
PLAY_STEP_MS = 1000    # темп по умолчанию (регрессия для старых вызовов)
LEAD_MS = 2000         # пауза после "GO!" до первой строки
PERFECT_MS = 300       # окно "идеально" (± мс)
GOOD_MS = 700          # окно "хорошо" (± мс)
PLAY_WINDOW = 10       # сколько строк показывать вперёд
EMPTY_SLOT = "⚪"      # пустой приёмник на линии ловли


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
    step_ms: int = PLAY_STEP_MS
    lead_ms: int = LEAD_MS
    perfect_ms: int = PERFECT_MS
    good_ms: int = GOOD_MS
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
    rows.append([InlineKeyboardButton(text="🎮 Мини-игры", callback_data="games")])
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
                InlineKeyboardButton(
                    text=f"{LANE_COLORS[lane]} {arrow}", callback_data=f"hit:{lane}"
                )
                for lane, arrow in enumerate(LANE_ARROWS)
            ],
            [InlineKeyboardButton(text="⏹ Стоп", callback_data="play_stop")],
        ]
    )


def build_notes(chart: Chart, step_ms: int = PLAY_STEP_MS, lead_ms: int = LEAD_MS) -> list[PlayNote]:
    notes = [
        PlayNote(row=row, lane=lane, t_ms=lead_ms + row * step_ms)
        for row, lanes in sorted(chart.notes.items())
        for lane in sorted(lanes)
    ]
    return notes


def render_play(game: Game, elapsed_ms: float) -> str:
    cur = int((elapsed_ms - game.lead_ms) // game.step_ms)
    row_notes: dict[int, set[int]] = {}
    for n in game.notes:
        if not n.hit:
            row_notes.setdefault(n.row, set()).add(n.lane)

    # Единственная линия ловли: пустые приёмники ⚪, доехавшая нота
    # загорается своим цветом прямо в этой строке.
    hit_line = "".join(
        LANE_COLORS[l] if l in row_notes.get(cur, set()) else EMPTY_SLOT
        for l in range(LANES)
    )
    lines = [f"▶{hit_line}◀ ЛОВИ"]
    for row in range(cur + 1, cur + PLAY_WINDOW):
        if 0 <= row < game.chart.rows:
            lanes = row_notes.get(row, set())
            cells = "".join(
                LANE_COLORS[l] if l in lanes else EMPTY_CELL for l in range(LANES)
            )
        else:
            cells = EMPTY_CELL * LANES
        lines.append(f" {cells}")
    missed = sum(
        1 for n in game.notes if not n.hit and elapsed_ms - n.t_ms > game.good_ms
    )
    lines.append("─" * 14)
    lines.append(f"✨ {game.perfect}  ✅ {game.good}  ❌ {missed}")
    lines.append("Цвет загорелся в линии ЛОВИ — жми кнопку этого цвета!")
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
        total_ms = game.lead_ms + game.chart.rows * game.step_ms + game.good_ms
        row = -game.lead_ms // game.step_ms  # стартуем до первой строки
        while True:
            elapsed = (time.monotonic() - game.start) * 1000
            if elapsed > total_ms:
                break
            await safe_edit(game, render_play(game, elapsed), play_keyboard())
            # Спим ровно до момента следующей строки — кадр на каждый шаг,
            # без рассинхрона перерисовки и темпа
            row += 1
            next_t = (game.lead_ms + row * game.step_ms) / 1000
            delay = next_t - (time.monotonic() - game.start)
            if delay > 0:
                await asyncio.sleep(delay)
        await finish_game(user_id)
    except asyncio.CancelledError:
        pass


def cancel_game(user_id: int) -> None:
    game = games.pop(user_id, None)
    if game and game.task and not game.task.done():
        game.task.cancel()


# ============================ Мини-игры ============================

dp = Dispatcher()

SIMON_START_LEN = 3     # стартовая длина последовательности
SIMON_SHOW_BASE_S = 1.5  # базовое время показа + 0.4с за стрелку
TA_ROUNDS = 6           # раундов тайм-атаки
BATTLE_HP = 3
# Цикл "камень-ножницы": ← бьёт ↓, ↓ бьёт ↑, ↑ бьёт →, → бьёт ←
BEATS = {0: 1, 1: 2, 2: 3, 3: 0}
BATTLE_RULE = "← бьёт ↓, ↓ бьёт ↑, ↑ бьёт →, → бьёт ←"


@dataclass
class MiniGame:
    kind: str            # "simon" | "ta" | "battle"
    chat_id: int
    message_id: int
    bot: Bot
    # Саймон
    seq: list[int] = field(default_factory=list)
    input_pos: int = 0
    round: int = 0
    accepting: bool = False
    # Тайм-атака
    target: int = -1
    shown_at: float = 0.0
    rounds_left: int = 0
    score: int = 0
    reactions: list[int] = field(default_factory=list)
    # Баттл
    boss_hp: int = BATTLE_HP
    player_hp: int = BATTLE_HP
    task: asyncio.Task | None = None


minigames: dict[int, MiniGame] = {}


def stop_minigame(user_id: int) -> None:
    mg = minigames.pop(user_id, None)
    if mg and mg.task and not mg.task.done():
        mg.task.cancel()


async def mg_edit(mg: MiniGame, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    try:
        await mg.bot.edit_message_text(
            text, chat_id=mg.chat_id, message_id=mg.message_id, reply_markup=keyboard,
        )
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
    except TelegramBadRequest:
        pass


def games_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧠 Саймон говорит", callback_data="mg:simon")],
            [InlineKeyboardButton(text="⚡ Тайм-атака", callback_data="mg:ta")],
            [InlineKeyboardButton(text="🥊 Баттл с боссом", callback_data="mg:battle")],
        ]
    )


def arrows_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{LANE_COLORS[lane]} {arrow}", callback_data=f"{prefix}:{lane}"
                )
                for lane, arrow in enumerate(LANE_ARROWS)
            ],
            [InlineKeyboardButton(text="⏹ Выйти", callback_data="mg_quit")],
        ]
    )


def mg_over_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔁 Ещё раз", callback_data=f"mg:{kind}"),
                InlineKeyboardButton(text="🎮 Другие игры", callback_data="games"),
            ]
        ]
    )


@dp.callback_query(F.data == "games")
async def cb_games(query: CallbackQuery) -> None:
    stop_minigame(query.from_user.id)
    await query.message.answer("🎮 Выбирай игру:", reply_markup=games_menu_keyboard())
    await query.answer()


@dp.callback_query(F.data == "mg_quit")
async def cb_mg_quit(query: CallbackQuery) -> None:
    stop_minigame(query.from_user.id)
    await query.message.edit_text("🎮 Выбирай игру:", reply_markup=games_menu_keyboard())
    await query.answer()


async def start_minigame(query: CallbackQuery, kind: str) -> MiniGame:
    user_id = query.from_user.id
    stop_minigame(user_id)
    sent = await query.bot.send_message(query.message.chat.id, "…")
    mg = MiniGame(
        kind=kind, chat_id=query.message.chat.id, message_id=sent.message_id,
        bot=query.bot,
    )
    minigames[user_id] = mg
    return mg


# --- Саймон говорит ---

async def simon_show_round(mg: MiniGame) -> None:
    mg.accepting = False
    mg.input_pos = 0
    shown = "  ".join(LANE_ARROWS[i] for i in mg.seq)
    await mg_edit(
        mg,
        f"🧠 Раунд {mg.round + 1}\n\nЗапоминай:\n\n<b>{shown}</b>",
        None,
    )
    await asyncio.sleep(SIMON_SHOW_BASE_S + 0.4 * len(mg.seq))
    mg.accepting = True
    await mg_edit(
        mg,
        f"🧠 Раунд {mg.round + 1}\n\nПовтори последовательность "
        f"({len(mg.seq)} стрелки):",
        arrows_keyboard("sim"),
    )


@dp.callback_query(F.data == "mg:simon")
async def cb_mg_simon(query: CallbackQuery) -> None:
    mg = await start_minigame(query, "simon")
    mg.seq = [random.randrange(LANES) for _ in range(SIMON_START_LEN)]
    mg.task = asyncio.create_task(simon_show_round(mg))
    await query.answer()


@dp.callback_query(F.data.startswith("sim:"))
async def cb_simon_hit(query: CallbackQuery) -> None:
    mg = minigames.get(query.from_user.id)
    if mg is None or mg.kind != "simon" or not mg.accepting:
        await query.answer()
        return
    lane = int(query.data.split(":")[1])
    if lane != mg.seq[mg.input_pos]:
        stop_minigame(query.from_user.id)
        shown = "  ".join(LANE_ARROWS[i] for i in mg.seq)
        await mg_edit(
            mg,
            f"💥 Мимо! Было: <b>{shown}</b>\n\n"
            f"Пройдено раундов: {mg.round}",
            mg_over_keyboard("simon"),
        )
        await query.answer("💥")
        return
    mg.input_pos += 1
    if mg.input_pos == len(mg.seq):
        mg.round += 1
        mg.seq.append(random.randrange(LANES))
        mg.task = asyncio.create_task(simon_show_round(mg))
        await query.answer(f"✅ Раунд {mg.round} пройден!")
    else:
        await query.answer("✔")


# --- Тайм-атака ---

async def ta_next(mg: MiniGame) -> None:
    mg.target = -1
    await mg_edit(
        mg,
        f"⚡ Раунд {TA_ROUNDS - mg.rounds_left + 1}/{TA_ROUNDS}   Очки: {mg.score}\n\n"
        f"⏳ Жди стрелку…",
        arrows_keyboard("ta"),
    )
    await asyncio.sleep(random.uniform(1.2, 3.0))
    mg.target = random.randrange(LANES)
    mg.shown_at = time.monotonic()
    await mg_edit(
        mg,
        f"⚡ ЖМИ:   <b>{LANE_COLORS[mg.target]} {LANE_ARROWS[mg.target]}</b>",
        arrows_keyboard("ta"),
    )


async def ta_finish(user_id: int, mg: MiniGame) -> None:
    stop_minigame(user_id)
    avg = sum(mg.reactions) // len(mg.reactions) if mg.reactions else 0
    await mg_edit(
        mg,
        f"🏁 Тайм-атака окончена!\n\n"
        f"Очки: {mg.score}\n"
        f"Средняя реакция: {avg} мс\n"
        f"Попаданий: {len(mg.reactions)}/{TA_ROUNDS}",
        mg_over_keyboard("ta"),
    )


@dp.callback_query(F.data == "mg:ta")
async def cb_mg_ta(query: CallbackQuery) -> None:
    mg = await start_minigame(query, "ta")
    mg.rounds_left = TA_ROUNDS
    mg.task = asyncio.create_task(ta_next(mg))
    await query.answer()


@dp.callback_query(F.data.startswith("ta:"))
async def cb_ta_hit(query: CallbackQuery) -> None:
    user_id = query.from_user.id
    mg = minigames.get(user_id)
    if mg is None or mg.kind != "ta":
        await query.answer()
        return
    lane = int(query.data.split(":")[1])
    if mg.target < 0:
        await query.answer("⏳ Фальстарт! Жди стрелку")
        return
    mg.rounds_left -= 1
    if lane == mg.target:
        delta = int((time.monotonic() - mg.shown_at) * 1000)
        pts = max(1, (3000 - delta) // 100)
        mg.score += pts
        mg.reactions.append(delta)
        await query.answer(f"⚡ {delta} мс → +{pts}")
    else:
        await query.answer("💨 не та стрелка")
    if mg.rounds_left <= 0:
        await ta_finish(user_id, mg)
    else:
        mg.task = asyncio.create_task(ta_next(mg))


# --- Баттл с боссом ---

def battle_text(mg: MiniGame, log: str = "") -> str:
    return (
        f"🥊 Баттл с Daddy Dearest\n\n"
        f"Ты: {'💙' * mg.player_hp}{'🖤' * (BATTLE_HP - mg.player_hp)}   "
        f"Босс: {'💜' * mg.boss_hp}{'🖤' * (BATTLE_HP - mg.boss_hp)}\n\n"
        f"{log}\n"
        f"Правило: {BATTLE_RULE}\n\n"
        f"Твой ход:"
    )


@dp.callback_query(F.data == "mg:battle")
async def cb_mg_battle(query: CallbackQuery) -> None:
    mg = await start_minigame(query, "battle")
    await mg_edit(mg, battle_text(mg, "Босс готовит атаку…"), arrows_keyboard("bat"))
    await query.answer()


@dp.callback_query(F.data.startswith("bat:"))
async def cb_battle_hit(query: CallbackQuery) -> None:
    user_id = query.from_user.id
    mg = minigames.get(user_id)
    if mg is None or mg.kind != "battle":
        await query.answer()
        return
    player = int(query.data.split(":")[1])
    boss = random.randrange(LANES)
    p, b = LANE_ARROWS[player], LANE_ARROWS[boss]
    if player == boss:
        log = f"Ты {p} vs босс {b} — ничья! 🤝"
        await query.answer("🤝 Ничья")
    elif BEATS[player] == boss:
        mg.boss_hp -= 1
        log = f"Ты {p} vs босс {b} — твой удар прошёл! 💥"
        await query.answer("💥 Попадание!")
    else:
        mg.player_hp -= 1
        log = f"Ты {p} vs босс {b} — босс уколол тебя! 🩸"
        await query.answer("🩸 Пропустил удар")
    if mg.boss_hp <= 0 or mg.player_hp <= 0:
        won = mg.boss_hp <= 0
        stop_minigame(user_id)
        await mg_edit(
            mg,
            f"{'🏆 Победа! Daddy Dearest повержен!' if won else '💀 Поражение… Босс оказался сильнее.'}\n\n"
            f"{log}",
            mg_over_keyboard("battle"),
        )
        return
    await mg_edit(mg, battle_text(mg, log), arrows_keyboard("bat"))


# ============================ Хендлеры ============================


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Это FNF-бот: редактор чартов и мини-игры.\n"
        "Создай чарт, сыграй в него — или зацени мини-игры 🎮",
        reply_markup=start_keyboard(message.from_user.id),
    )


async def open_editor(bot: Bot, chat_id: int, user_id: int) -> None:
    cancel_game(user_id)
    stop_minigame(user_id)
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
    await query.message.answer(
        "На чём играешь?\n\n"
        "📱 Телефон — быстрый темп (пальцы на кнопках)\n"
        "🖥 Компьютер — спокойный темп (мышке нужно время)",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📱 Телефон", callback_data="speed:mobile"),
                    InlineKeyboardButton(text="🖥 Компьютер", callback_data="speed:desktop"),
                ]
            ]
        ),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("speed:"))
async def cb_speed(query: CallbackQuery) -> None:
    user_id = query.from_user.id
    chart = last_charts.get(user_id)
    if chart is None or chart.note_count() == 0:
        await query.answer("Сначала создай чарт 🎼", show_alert=True)
        return
    step_ms, lead_ms, perfect_ms, good_ms = PLAY_SPEEDS[query.data.split(":")[1]]
    cancel_game(user_id)
    await query.message.delete()
    sent = await query.bot.send_message(
        query.message.chat.id, "<pre>▶️ Приготовься…</pre>",
        reply_markup=play_keyboard(),
    )
    game = Game(
        chart=chart,
        notes=build_notes(chart, step_ms, lead_ms),
        chat_id=query.message.chat.id,
        message_id=sent.message_id,
        bot=query.bot,
        step_ms=step_ms,
        lead_ms=lead_ms,
        perfect_ms=perfect_ms,
        good_ms=good_ms,
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
            if diff <= game.good_ms and (best is None or diff < abs(elapsed - best.t_ms)):
                best = note
    if best is None:
        game.stray += 1
        await query.answer("💨 мимо")
        return
    # Аккорд ловится одним тапом: засчитываем все ноты той же строки
    # (одновременные тапы по нескольким inline-кнопкам Telegram не поддерживает)
    chord = [n for n in game.notes if n.row == best.row and not n.hit]
    perfect = abs(elapsed - best.t_ms) <= game.perfect_ms
    for note in chord:
        note.hit = True
    if perfect:
        game.perfect += len(chord)
        await query.answer(f"✨ идеально!{' x' + str(len(chord)) if len(chord) > 1 else ''}")
    else:
        game.good += len(chord)
        await query.answer(f"✅ хорошо{' x' + str(len(chord)) if len(chord) > 1 else ''}")


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
