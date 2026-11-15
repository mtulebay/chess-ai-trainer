"""Загрузка партий из публичного API Chess.com (без ключа)."""
from __future__ import annotations

import time
from typing import Iterable

import requests

API = "https://api.chess.com/pub/player/{user}/games/archives"
# Chess.com просит осмысленный User-Agent; контакт можно добавить через CHESSCOM_USER_AGENT.
HEADERS = {"User-Agent": "chess-ai-trainer/0.1 (personal training bot)"}

DRAW_CODES = {"agreed", "repetition", "stalemate", "insufficient", "50move",
              "timevsinsufficient", "draw"}


def _get(url: str, session: requests.Session, retries: int = 4) -> requests.Response:
    delay = 2.0
    for attempt in range(retries):
        r = session.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 429 and attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("unreachable")


def archive_urls(user: str, session: requests.Session) -> list[str]:
    return _get(API.format(user=user), session).json().get("archives", [])


def normalize(raw: dict, user: str) -> dict | None:
    """Привести запись Chess.com к строке таблицы games. None — если партию пропускаем."""
    if raw.get("rules", "chess") != "chess" or not raw.get("pgn"):
        return None  # варианты (960, bughouse...) не анализируем
    white, black = raw.get("white", {}), raw.get("black", {})
    if white.get("username", "").lower() == user:
        me, opp, color = white, black, "white"
    elif black.get("username", "").lower() == user:
        me, opp, color = black, white, "black"
    else:
        return None
    code = me.get("result", "")
    if code == "win":
        result, detail = "win", opp.get("result", "")
    elif code in DRAW_CODES:
        result, detail = "draw", code
    else:
        result, detail = "loss", code
    opening = ""
    eco_url = raw.get("eco") or ""
    if "/openings/" in eco_url:
        opening = eco_url.rsplit("/openings/", 1)[1].replace("-", " ")
    return {
        "id": raw.get("uuid") or raw.get("url"),
        "url": raw.get("url"),
        "end_time": int(raw.get("end_time", 0)),
        "time_class": raw.get("time_class", ""),
        "time_control": raw.get("time_control", ""),
        "rated": int(bool(raw.get("rated"))),
        "color": color,
        "opponent": opp.get("username", ""),
        "my_rating": me.get("rating"),
        "opp_rating": opp.get("rating"),
        "result": result,
        "result_detail": detail,
        "opening": opening,
        "pgn": raw["pgn"],
    }


def fetch_games(user: str, months: int = 2, session: requests.Session | None = None) -> Iterable[dict]:
    """Партии за последние `months` месячных архивов (0 = все)."""
    session = session or requests.Session()
    urls = archive_urls(user, session)
    if months > 0:
        urls = urls[-months:]
    for url in urls:
        for raw in _get(url, session).json().get("games", []):
            g = normalize(raw, user)
            if g:
                yield g


STATS = "https://api.chess.com/pub/player/{user}/stats"


def fetch_stats(user: str, session: requests.Session | None = None) -> dict:
    """Текущий/лучший рейтинг и счёт по контролям: {'rapid': {'rating':.., 'best':.., 'win':..}, ...}."""
    session = session or requests.Session()
    raw = _get(STATS.format(user=user), session).json()
    out = {}
    for key, val in raw.items():
        if not key.startswith("chess_"):
            continue
        tc = key.removeprefix("chess_")
        rec = val.get("record", {})
        out[tc] = {
            "rating": val.get("last", {}).get("rating"),
            "best": val.get("best", {}).get("rating"),
            "win": rec.get("win", 0), "loss": rec.get("loss", 0), "draw": rec.get("draw", 0),
        }
    return out
