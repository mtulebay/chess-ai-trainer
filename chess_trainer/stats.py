"""Агрегированная статистика по моим ходам — основа профиля слабых мест."""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter, defaultdict

from .analyze import parse_time_control

ERR = ("mistake", "blunder")


def load_games(conn: sqlite3.Connection, since: int, until: int, exclude: set[str]) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM games WHERE analyzed = 1 AND end_time >= ? AND end_time < ? ORDER BY end_time",
        (since, until),
    ).fetchall()
    return [r for r in rows if (r["time_class"] or "").lower() not in exclude]


def load_moves(conn: sqlite3.Connection, game_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM moves WHERE game_id = ? ORDER BY ply", (game_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["motifs"] = json.loads(d["motifs"] or "[]")
        out.append(d)
    return out


def _motif_key(m: str) -> str:
    if m.startswith("hung_"):
        return "hung_piece"
    if m.startswith("double_attack"):
        return "double_attack"
    if m.startswith("allowed_mate"):
        return "allowed_mate"
    if m.startswith("missed_mate"):
        return "missed_mate"
    return m


def compute_stats(conn: sqlite3.Connection, since: int, until: int, exclude: set[str]) -> dict:
    games = load_games(conn, since, until, exclude)
    s: dict = {"games": len(games)}
    if not games:
        return s

    res = Counter(g["result"] for g in games)
    s["results"] = {"win": res["win"], "loss": res["loss"], "draw": res["draw"]}
    s["lost_on_time"] = sum(1 for g in games if g["result"] == "loss" and g["result_detail"] == "timeout")

    # Рейтинг по контролю: первый и последний за период.
    rating = {}
    for g in games:
        r = rating.setdefault(g["time_class"], {"start": g["my_rating"], "end": g["my_rating"], "games": 0})
        r["end"] = g["my_rating"]
        r["games"] += 1
    s["rating"] = rating

    by_color = defaultdict(Counter)
    for g in games:
        by_color[g["color"]][g["result"]] += 1
    s["by_color"] = {c: dict(v) for c, v in by_color.items()}

    phase_moves = Counter()
    phase_err = Counter()
    phase_loss = Counter()
    labels = Counter()
    motifs = Counter()
    low_clock_err = 0
    fast_err = 0
    err_total = 0
    first_err_moves = []
    blown_wins = []
    openings = defaultdict(lambda: {"games": 0, "points": 0.0, "opening_errors": 0})
    think_long = 0

    for g in games:
        moves = load_moves(conn, g["id"])
        mine = [m for m in moves if m["is_mine"]]
        base, _ = parse_time_control(g["time_control"])
        first_err = None
        best_eval = -10_000
        for m in mine:
            if m["decided"]:
                continue
            phase_moves[m["phase"]] += 1
            phase_loss[m["phase"]] += m["loss"]
            labels[m["label"]] += 1
            if m["label"] in ERR:
                err_total += 1
                phase_err[m["phase"]] += 1
                if m["low_clock"]:
                    low_clock_err += 1
                if m["time_spent"] is not None and m["time_spent"] < 3 and not m["low_clock"]:
                    fast_err += 1
                if first_err is None:
                    first_err = m["move_no"]
                for mt in m["motifs"]:
                    motifs[_motif_key(mt)] += 1
            if m["missed_punish"]:
                motifs["missed_punish"] += 1
            if base and m["time_spent"] and m["time_spent"] > 0.15 * base:
                think_long += 1
            best_eval = max(best_eval, m["eval_after"] if m["eval_after"] is not None else -10_000)
        if first_err is not None:
            first_err_moves.append(first_err)
        if best_eval >= 300 and g["result"] != "win":
            blown_wins.append({"url": g["url"], "opponent": g["opponent"], "result": g["result"],
                               "best_advantage_pawns": round(best_eval / 100, 1)})
        op = openings[g["opening"] or "?"]
        op["games"] += 1
        op["points"] += {"win": 1, "draw": 0.5}.get(g["result"], 0)
        op["opening_errors"] += sum(1 for m in mine if m["phase"] == "opening" and m["label"] in ERR)

    n = len(games)
    s["per_game"] = {k: round(labels[k] / n, 2) for k in ("blunder", "mistake", "inaccuracy")}
    s["phase"] = {
        ph: {
            "moves": phase_moves[ph],
            "errors": phase_err[ph],
            "errors_per_100_moves": round(100 * phase_err[ph] / phase_moves[ph], 1) if phase_moves[ph] else None,
            "avg_loss_pawns": round(phase_loss[ph] / phase_moves[ph] / 100, 2) if phase_moves[ph] else None,
        }
        for ph in ("opening", "middlegame", "endgame")
    }
    s["time"] = {
        "errors_total": err_total,
        "errors_on_low_clock": low_clock_err,
        "errors_played_fast_under_3s": fast_err,
        "very_long_thinks": think_long,
    }
    s["motifs"] = dict(motifs.most_common())
    s["median_first_error_move"] = statistics.median(first_err_moves) if first_err_moves else None
    s["blown_wins"] = blown_wins[:5]
    s["blown_wins_count"] = len(blown_wins)
    s["openings"] = sorted(
        ({"name": k, **v, "score_pct": round(100 * v["points"] / v["games"])} for k, v in openings.items()),
        key=lambda x: -x["games"],
    )[:6]
    return s
