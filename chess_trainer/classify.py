"""Оценки ходов в стиле Chess.com и точность партии.

Chess.com не публикует точный алгоритм, это приближение по их открытому описанию:
ходы оцениваются по потере «ожидаемых очков» (шанс на победу от 0 до 1), а не по пешкам.
  Лучший       — ход движка
  Превосходный — потеря 0–0.02
  Хороший      — 0.02–0.05
  Неточность   — 0.05–0.10
  Ошибка       — 0.10–0.20
  Зевок        — ≥ 0.20
  Отличный (great) — единственный хороший ход: второй вариант заметно хуже
  Блестящий    — лучший/почти лучший ход с жертвой материала, позиция не была уже выиграна
  Промах (miss) — соперник ошибся, а ты не воспользовался
  Теория       — ход из дебютной теории (по названию дебюта в PGN Chess.com)
"""
from __future__ import annotations

import math
import re

import chess

from .analyze import VALUES, hanging_squares

CLASSES = {
    "brilliant": ("Блестящий", "!!"),
    "great": ("Отличный", "!"),
    "best": ("Лучший", "★"),
    "excellent": ("Превосходный", "👍"),
    "good": ("Хороший", "✓"),
    "book": ("Теория", "📖"),
    "inaccuracy": ("Неточность", "?!"),
    "mistake": ("Ошибка", "?"),
    "miss": ("Промах", "✗"),
    "blunder": ("Зевок", "??"),
}
BAD = ("inaccuracy", "mistake", "miss", "blunder")


def win_prob(cp: float | None) -> float:
    """Шанс на победу (0..1) из оценки в сантипешках — формула lichess."""
    if cp is None:
        return 0.5
    return 1 / (1 + math.exp(-0.00368208 * cp))


def move_accuracy(wp_before: float, wp_after: float) -> float:
    """Точность хода (0..100), формула lichess."""
    drop = max(0.0, (wp_before - wp_after) * 100)
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * drop) - 3.1669))


def book_plies(opening: str | None) -> int:
    """Chess.com кладёт в ECOUrl последний «теоретический» ход: '...-Variation-3...c5' -> 6 полуходов."""
    if not opening:
        return 0
    m = re.search(r"(\d+)\.(\.\.)?\s*[A-Za-z][\w+#=]*\s*$", opening.strip())
    if not m:
        return 0
    n = int(m.group(1))
    return 2 * n if m.group(2) else 2 * n - 1


def _is_sacrifice(fen_before: str, san: str) -> bool:
    """Ход отдаёт материал: сходившая фигура (не пешка) осталась под невыгодным боем,
    либо взятие защищённой фигуры более дорогой фигурой."""
    board = chess.Board(fen_before)
    try:
        move = board.parse_san(san)
    except ValueError:
        return False
    mover = board.turn
    piece = board.piece_at(move.from_square)
    if piece is None or piece.piece_type in (chess.PAWN, chess.KING):
        return False
    captured = board.piece_at(move.to_square)
    gain = VALUES[captured.piece_type] if captured else 0
    board.push(move)
    hang = hanging_squares(board, mover)
    if move.to_square in hang and VALUES[piece.piece_type] - gain >= 2:
        return True
    # Оставил под боем другую ценную фигуру (например, ферзя), сходив этой.
    return any(VALUES[pt] >= 3 and sq != move.to_square for sq, pt in hang.items()) and \
        not board.is_check()


def classify_moves(moves: list[dict], opening: str | None = None) -> list[dict]:
    """Добавить каждому ходу 'cls', 'ep_loss', 'accuracy', 'wp_after_white', 'explain'.

    `moves` — строки таблицы moves (eval_before/eval_after с точки зрения ходящего).
    """
    book = book_plies(opening)
    prev_bad = False
    for m in moves:
        ev_b, ev_a = m["eval_before"], m["eval_after"]
        wp_b, wp_a = win_prob(ev_b), win_prob(ev_a)
        ep = max(0.0, wp_b - wp_a)
        is_best = m["best_san"] is not None and m["san"] == m["best_san"]
        if is_best:
            ep = 0.0
        second = m.get("second_eval")

        if m["ply"] <= book and ep < 0.05:
            cls = "book"
        elif ep >= 0.20:
            cls = "blunder"
        elif ep >= 0.10:
            cls = "mistake"
        elif ep >= 0.05:
            cls = "inaccuracy"
        elif is_best:
            cls = "best"
        elif ep <= 0.02:
            cls = "excellent"
        else:
            cls = "good"

        # Промах: соперник только что ошибся, а мы упустили наказание.
        if prev_bad and cls in ("mistake", "inaccuracy", "blunder") and wp_b >= 0.6:
            cls = "miss"

        # Отличный / блестящий — только для лучших и почти лучших ходов.
        if cls in ("best", "excellent") and m.get("fen_before"):
            only_move = second is not None and (wp_b - win_prob(second)) >= 0.15
            not_already_won = wp_b < 0.9
            if not_already_won and wp_a >= 0.45 and _is_sacrifice(m["fen_before"], m["san"]):
                cls = "brilliant"
            elif only_move and not_already_won and is_best:
                cls = "great"

        m["cls"] = cls
        m["cls_name"], m["cls_icon"] = CLASSES[cls]
        m["ep_loss"] = round(ep, 3)
        m["accuracy"] = round(move_accuracy(wp_b, wp_a), 1)
        white_moved = m["side"] == "white"
        m["wp_after_white"] = round(wp_a if white_moved else 1 - wp_a, 3)
        m["eval_after_white"] = ev_a if white_moved else (-ev_a if ev_a is not None else None)
        m["explain"] = explain(m)
        prev_bad = cls in ("mistake", "blunder", "miss")
    return moves


def _piece_ru(code: str) -> str:
    return code.replace("_", " на ")


def explain(m: dict) -> str:
    """Короткое объяснение хода без жаргона — по проверенным фактам анализа."""
    cls = m["cls"]
    best, line, reply = m.get("best_san"), m.get("best_line"), m.get("reply_line")
    motifs = m.get("motifs") or []
    parts: list[str] = []
    if cls == "book":
        return "Ход из дебютной теории."
    if cls == "brilliant":
        return "Жертва материала, которая работает: позиция после неё не хуже, а соперник не может спокойно взять."
    if cls == "great":
        return "Единственный ход, который сохраняет позицию: остальные заметно хуже."
    if cls == "best":
        return "Лучший ход в позиции."
    if cls in ("excellent", "good"):
        s = "Хороший ход." if cls == "good" else "Почти так же хорошо, как лучший ход."
        return s + (f" Сильнейшим было {best}." if best else "")

    for mt in motifs:
        if mt.startswith("allowed_mate_in_"):
            parts.append(f"Разрешает мат в {mt.rsplit('_', 1)[1]}.")
        elif mt.startswith("missed_mate_in_"):
            parts.append(f"Был мат в {mt.rsplit('_', 1)[1]}, но ход его упускает.")
        elif mt.startswith("hung_"):
            _, piece, sq = mt.split("_", 2)
            parts.append(f"{piece.capitalize()} на {sq} остаётся под боем.")
        elif mt.startswith("double_attack:"):
            parts.append(f"Соперник получает двойной удар: {mt.split(':', 1)[1]}.")
        elif mt == "missed_material_win":
            parts.append("Можно было выиграть материал.")
        elif mt == "missed_punish":
            parts.append("Соперник перед этим ошибся, а ход не использует это.")
    if m.get("material_lost"):
        parts.append(f"Теряется около {m['material_lost']:g} очк. материала.")
    if reply:
        parts.append(f"Соперник отвечает: {reply}.")
    if best:
        parts.append(f"Лучше было {best}" + (f" ({line})." if line else "."))
    if m.get("low_clock"):
        parts.append("Ход сделан при нехватке времени.")
    elif m.get("time_spent") is not None and m["time_spent"] < 3 and cls in ("mistake", "blunder", "miss"):
        parts.append(f"На ход ушло {m['time_spent']:g} с — спешка.")
    head = {"inaccuracy": "Неточность.", "mistake": "Ошибка.", "miss": "Упущенная возможность.",
            "blunder": "Зевок."}[cls]
    return " ".join([head, *parts])


def game_accuracy(moves: list[dict], side: str) -> float | None:
    acc = [m["accuracy"] for m in moves if m["side"] == side and "accuracy" in m]
    if not acc:
        return None
    # Среднее арифметическое и гармоническое: гармоническое сильнее наказывает за зевки (как у lichess).
    mean = sum(acc) / len(acc)
    harm = len(acc) / sum(1 / max(a, 1) for a in acc)
    return round((mean + harm) / 2, 1)
