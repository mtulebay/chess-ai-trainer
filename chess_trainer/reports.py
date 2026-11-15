"""Сборка фактов для разборов: день, неделя, профиль слабых мест."""
from __future__ import annotations

import json
import re
import sqlite3
import time

from . import coach, db
from .config import Config
from .stats import compute_stats, load_games, load_moves

DAY = 86_400
PHASE_RU = {"opening": "дебют", "middlegame": "миттельшпиль", "endgame": "эндшпиль"}
RESULT_RU = {"win": "победа", "loss": "поражение", "draw": "ничья"}
LABEL_RU = {"blunder": "зевок", "mistake": "ошибка", "inaccuracy": "неточность"}


def situation(cp: int | None) -> str:
    """Оценка позиции словами — чтобы LLM не тащил цифры в текст."""
    if cp is None:
        return "?"
    if cp >= 500:
        return "выигрываете"
    if cp >= 150:
        return "заметно лучше"
    if cp >= 50:
        return "чуть лучше"
    if cp > -50:
        return "примерно равно"
    if cp > -150:
        return "чуть хуже"
    if cp > -500:
        return "заметно хуже"
    return "проигрываете"


def _fmt_clock(sec: float | None) -> str | None:
    if sec is None:
        return None
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def key_moments(moves: list[dict], per_game: int = 3) -> list[dict]:
    mine = [m for m in moves if m["is_mine"] and (m["label"] in ("blunder", "mistake") or m["missed_punish"])]
    mine.sort(key=lambda m: -m["loss"])
    out = []
    for m in sorted(mine[:per_game], key=lambda m: m["ply"]):
        out.append({
            "ход": f'{m["move_no"]}{"." if m["side"] == "white" else "..."} {m["san"]}',
            "тип": LABEL_RU.get(m["label"], m["label"]),
            "фаза": PHASE_RU[m["phase"]],
            "было_до_хода": situation(m["eval_before"]),
            "стало_после": situation(m["eval_after"]),
            "потеря_в_пешках": round(m["loss"] / 100, 1),
            "лучше_было": m["best_san"],
            "продолжение_после_лучшего": m["best_line"],
            "как_соперник_наказывает": m["reply_line"],
            "материал_теряется": m["material_lost"],
            "мотивы": m["motifs"],
            "на_часах": _fmt_clock(m["clock_left"]),
            "потрачено_секунд": m["time_spent"],
            "мало_времени": bool(m["low_clock"]),
        })
    return out


def game_summary(g: sqlite3.Row, moves: list[dict]) -> dict:
    mine = [m for m in moves if m["is_mine"] and not m["decided"]]
    return {
        "соперник": f'{g["opponent"]} ({g["opp_rating"]})',
        "мой_рейтинг": g["my_rating"],
        "цвет": "белые" if g["color"] == "white" else "чёрные",
        "контроль": f'{g["time_class"]} {g["time_control"]}',
        "дебют": g["opening"],
        "результат": RESULT_RU[g["result"]],
        "как_закончилась": g["result_detail"],
        "ходов": max((m["move_no"] for m in moves), default=0),
        "зевков": sum(m["label"] == "blunder" for m in mine),
        "ошибок": sum(m["label"] == "mistake" for m in mine),
        "неточностей": sum(m["label"] == "inaccuracy" for m in mine),
        "ключевые_моменты": key_moments(moves),
        "ссылка": g["url"],
    }


def latest_profile(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT narrative FROM profile_snapshots WHERE narrative IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["narrative"] if row else None


def extract_recommendation(text: str, marker: str) -> str | None:
    m = re.search(rf"{marker}\s*(.+)", text)
    return m.group(1).strip() if m else None


# ---------- день ----------

def build_daily(conn: sqlite3.Connection, cfg: Config, since: int, until: int) -> dict | None:
    games = load_games(conn, since, until, cfg.exclude_time_classes)
    if not games:
        return None
    summaries = [game_summary(g, load_moves(conn, g["id"])) for g in games]
    # Ограничиваем объём: больше 8 историй всё равно не прочитают.
    total = 0
    for s in summaries:
        room = max(0, 8 - total)
        s["ключевые_моменты"] = s["ключевые_моменты"][:room]
        total += len(s["ключевые_моменты"])
    return {
        "партии_сегодня": summaries,
        "статистика_дня": compute_stats(conn, since, until, cfg.exclude_time_classes),
        "профиль_слабых_мест": latest_profile(conn),
        "вчерашняя_рекомендация": db.get_state(conn, "last_recommendation"),
    }


def fallback_daily(data: dict) -> str:
    lines = []
    for s in data["партии_сегодня"]:
        lines.append(f'{s["результат"]} vs {s["соперник"]} ({s["цвет"]}, {s["контроль"]}): '
                     f'зевков {s["зевков"]}, ошибок {s["ошибок"]}')
        for k in s["ключевые_моменты"]:
            lines.append(f'  {k["ход"]} — {k["тип"]}, лучше {k["лучше_было"]}; на часах {k["на_часах"]}')
    return "Партии за день:\n" + "\n".join(lines)


def run_daily(conn: sqlite3.Connection, cfg: Config, now: int | None = None) -> str | None:
    now = now or int(time.time())
    since = int(db.get_state(conn, "last_daily_until") or now - DAY)
    data = build_daily(conn, cfg, since, now)
    if data is None:
        db.set_state(conn, "last_daily_until", now)
        return None
    text = coach.generate(cfg, coach.SYSTEM_PROMPT, data, fallback_daily(data))
    rec = extract_recommendation(text, "На следующую партию:")
    if rec:
        db.set_state(conn, "last_recommendation", rec)
    db.set_state(conn, "last_daily_until", now)
    return text


# ---------- профиль ----------

def update_profile(conn: sqlite3.Connection, cfg: Config, with_narrative: bool, now: int | None = None) -> dict:
    now = now or int(time.time())
    stats = compute_stats(conn, now - cfg.profile_window_days * DAY, now, cfg.exclude_time_classes)
    narrative = None
    if with_narrative and stats.get("games"):
        prev = latest_profile(conn)
        narrative = coach.generate(
            cfg, coach.PROFILE_PROMPT,
            {"статистика_за_окно_дней": cfg.profile_window_days, "статистика": stats, "прошлый_профиль": prev},
            fallback=prev or "",
        ) or None
        if narrative:
            cfg.profile_md.write_text(narrative + "\n", encoding="utf-8")
    conn.execute("INSERT INTO profile_snapshots(created_at, stats_json, narrative) VALUES (?, ?, ?)",
                 (now, json.dumps(stats, ensure_ascii=False), narrative))
    conn.commit()
    return stats


# ---------- неделя ----------

def biggest_moment(conn: sqlite3.Connection, cfg: Config, since: int, until: int) -> dict | None:
    best = None
    for g in load_games(conn, since, until, cfg.exclude_time_classes):
        for m in load_moves(conn, g["id"]):
            if m["is_mine"] and not m["decided"] and (best is None or m["loss"] > best[1]["loss"]):
                best = (g, m)
    if not best or best[1]["label"] not in ("blunder", "mistake"):
        return None
    g, m = best
    return {"соперник": f'{g["opponent"]} ({g["opp_rating"]})', "ссылка": g["url"], **key_moments([m], 1)[0]}


def run_weekly(conn: sqlite3.Connection, cfg: Config, now: int | None = None) -> str | None:
    now = now or int(time.time())
    week = compute_stats(conn, now - 7 * DAY, now, cfg.exclude_time_classes)
    if not week.get("games"):
        return None
    prev = compute_stats(conn, now - 14 * DAY, now - 7 * DAY, cfg.exclude_time_classes)
    update_profile(conn, cfg, with_narrative=True, now=now)
    data = {
        "эта_неделя": week,
        "прошлая_неделя": prev,
        "самый_дорогой_момент": biggest_moment(conn, cfg, now - 7 * DAY, now),
        "профиль_слабых_мест": latest_profile(conn),
    }
    fallback = (f'Неделя: {week["games"]} партий, +{week["results"]["win"]} -{week["results"]["loss"]} '
                f'={week["results"]["draw"]}. Зевков за партию: {week["per_game"]["blunder"]}.')
    text = coach.generate(cfg, coach.WEEKLY_PROMPT, data, fallback)
    profile = latest_profile(conn)
    if profile:
        text += "\n\nПрофиль слабых мест (обновлён):\n" + profile
    return text
