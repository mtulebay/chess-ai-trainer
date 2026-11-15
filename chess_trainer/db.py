"""SQLite-хранилище: партии, ходы с анализом, состояние, снимки профиля."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

ANALYSIS_VERSION = 2  # синхронно с analyze.ANALYSIS_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id            TEXT PRIMARY KEY,
    url           TEXT,
    end_time      INTEGER NOT NULL,
    time_class    TEXT,
    time_control  TEXT,
    rated         INTEGER,
    color         TEXT,          -- 'white' | 'black' (мой цвет)
    opponent      TEXT,
    my_rating     INTEGER,
    opp_rating    INTEGER,
    result        TEXT,          -- 'win' | 'loss' | 'draw'
    result_detail TEXT,          -- resigned, timeout, checkmated, ...
    opening       TEXT,
    pgn           TEXT NOT NULL,
    analyzed      INTEGER NOT NULL DEFAULT 0,
    analysis_depth INTEGER,
    analysis_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_games_end ON games(end_time);

CREATE TABLE IF NOT EXISTS moves (
    game_id      TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply          INTEGER NOT NULL,      -- 1 = первый ход белых
    move_no      INTEGER NOT NULL,      -- номер хода в нотации
    side         TEXT NOT NULL,         -- 'white' | 'black'
    is_mine      INTEGER NOT NULL,
    san          TEXT NOT NULL,
    best_san     TEXT,
    eval_before  INTEGER,               -- оценка с точки зрения ходящего, сантипешки (обрезана)
    eval_after   INTEGER,
    loss         INTEGER NOT NULL,
    label        TEXT NOT NULL,         -- ok | inaccuracy | mistake | blunder
    decided      INTEGER NOT NULL DEFAULT 0,
    phase        TEXT NOT NULL,         -- opening | middlegame | endgame
    clock_left   REAL,
    time_spent   REAL,
    low_clock    INTEGER NOT NULL DEFAULT 0,
    best_line    TEXT,
    reply_line   TEXT,                  -- как соперник наказывает сыгранный ход
    material_lost REAL,                 -- материал, который форсированно теряется по сравнению с лучшим
    motifs       TEXT,                  -- JSON-список: hung_piece, fork, allowed_mate, missed_mate, ...
    missed_punish INTEGER NOT NULL DEFAULT 0,
    fen_before   TEXT,
    PRIMARY KEY (game_id, ply)
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS profile_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    stats_json TEXT NOT NULL,
    narrative  TEXT
);
"""


MIGRATIONS = [
    ("moves", "uci", "TEXT"),
    ("moves", "best_uci", "TEXT"),
    ("moves", "second_eval", "INTEGER"),
    ("games", "analysis_version", "INTEGER"),
]

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS game_notes (
    game_id  TEXT NOT NULL,
    kind     TEXT NOT NULL,         -- 'report' | 'explain:<ply>'
    text     TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (game_id, kind)
);
CREATE TABLE IF NOT EXISTS openings (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL,
    color  TEXT NOT NULL,           -- 'white' | 'black'
    match  TEXT,                    -- подстрока для поиска в названии дебюта Chess.com
    notes  TEXT
);
CREATE TABLE IF NOT EXISTS opening_resources (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    opening_id INTEGER NOT NULL REFERENCES openings(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    url        TEXT NOT NULL,
    kind       TEXT,                -- video | article | course
    lang       TEXT,
    why        TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (opening_id, url)
);
"""


def connect(path: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.executescript(EXTRA_SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def upsert_game(conn: sqlite3.Connection, g: dict) -> bool:
    """Вставить партию, если её ещё нет. True — если новая."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO games
        (id, url, end_time, time_class, time_control, rated, color, opponent,
         my_rating, opp_rating, result, result_detail, opening, pgn)
        VALUES (:id, :url, :end_time, :time_class, :time_control, :rated, :color, :opponent,
                :my_rating, :opp_rating, :result, :result_detail, :opening, :pgn)""",
        g,
    )
    return cur.rowcount > 0


def save_analysis(conn: sqlite3.Connection, game_id: str, depth: int, moves: list[dict]) -> None:
    conn.execute("DELETE FROM moves WHERE game_id = ?", (game_id,))
    for m in moves:
        row = dict(m)
        row["game_id"] = game_id
        row["motifs"] = json.dumps(row.get("motifs") or [], ensure_ascii=False)
        conn.execute(
            """INSERT INTO moves (game_id, ply, move_no, side, is_mine, san, best_san, eval_before,
               eval_after, loss, label, decided, phase, clock_left, time_spent, low_clock, best_line,
               reply_line, material_lost, motifs, missed_punish, fen_before, uci, best_uci, second_eval)
               VALUES (:game_id, :ply, :move_no, :side, :is_mine, :san, :best_san, :eval_before,
               :eval_after, :loss, :label, :decided, :phase, :clock_left, :time_spent, :low_clock,
               :best_line, :reply_line, :material_lost, :motifs, :missed_punish, :fen_before,
               :uci, :best_uci, :second_eval)""",
            {"uci": None, "best_uci": None, "second_eval": None, **row},
        )
    conn.execute(
        "UPDATE games SET analyzed = 1, analysis_depth = ?, analysis_error = NULL, analysis_version = ? "
        "WHERE id = ?",
        (depth, ANALYSIS_VERSION, game_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, game_id: str, error: str) -> None:
    conn.execute("UPDATE games SET analyzed = -1, analysis_error = ? WHERE id = ?", (error[:500], game_id))
    conn.commit()
