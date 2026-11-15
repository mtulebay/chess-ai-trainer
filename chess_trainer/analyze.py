"""Анализ партии движком: потеря на каждом ходу, лучший ход, фаза, часы, мотивы."""
from __future__ import annotations

import io
from dataclasses import dataclass

import chess
import chess.engine
import chess.pgn

MATE_CP = 10_000
CLIP_CP = 1_000          # оценки обрезаются до ±10 пешек: дальше разница уже не важна
LINE_PLIES = 6

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
PIECE_RU = {chess.PAWN: "пешка", chess.KNIGHT: "конь", chess.BISHOP: "слон",
            chess.ROOK: "ладья", chess.QUEEN: "ферзь", chess.KING: "король"}


@dataclass
class Thresholds:
    blunder: int = 200
    mistake: int = 100
    inaccuracy: int = 50
    decided: int = 500


# ---------- вспомогательное ----------

def material(board: chess.Board, color: chess.Color) -> int:
    return sum(VALUES[p.piece_type] for p in board.piece_map().values() if p.color == color)


def material_balance(board: chess.Board, color: chess.Color) -> int:
    return material(board, color) - material(board, not color)


def phase_of(board: chess.Board) -> str:
    """Фаза по материалу (без пешек и королей): старт = 62."""
    npm = sum(VALUES[p.piece_type] for p in board.piece_map().values()
              if p.piece_type not in (chess.PAWN, chess.KING))
    queens = len(board.pieces(chess.QUEEN, chess.WHITE)) + len(board.pieces(chess.QUEEN, chess.BLACK))
    if npm <= 26 or (queens == 0 and npm <= 32):
        return "endgame"
    if board.fullmove_number <= 12 and npm >= 54:
        return "opening"
    return "middlegame"


def parse_time_control(tc: str) -> tuple[float | None, float]:
    """'180+2' -> (180, 2); '600' -> (600, 0); '1/86400' (заочные) -> (None, 0)."""
    if not tc or "/" in tc:
        return None, 0.0
    base, _, inc = tc.partition("+")
    try:
        return float(base), float(inc or 0)
    except ValueError:
        return None, 0.0


def score_cp(info: chess.engine.InfoDict | None, color: chess.Color) -> int:
    score = info["score"].pov(color).score(mate_score=MATE_CP)
    return max(-CLIP_CP, min(CLIP_CP, score))


def mate_in(info: chess.engine.InfoDict, color: chess.Color) -> int | None:
    return info["score"].pov(color).mate()


def terminal_cp(board: chess.Board, color: chess.Color) -> int:
    if board.is_checkmate():
        return CLIP_CP if board.turn != color else -CLIP_CP
    return 0


def line_san(board: chess.Board, moves: list[chess.Move], plies: int = LINE_PLIES) -> str:
    b = board.copy(stack=False)
    try:
        return b.variation_san(moves[:plies])
    except (ValueError, AssertionError):
        return ""


def play_line(board: chess.Board, moves: list[chess.Move], plies: int) -> chess.Board:
    b = board.copy(stack=False)
    for mv in moves[:plies]:
        if mv not in b.legal_moves:
            break
        b.push(mv)
    return b


def hanging_squares(board: chess.Board, color: chess.Color) -> dict[chess.Square, int]:
    """Фигуры `color`, которые бьются невыгодно для него: не защищены или атакованы более дешёвой фигурой."""
    res = {}
    for sq, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        attackers = board.attackers(not color, sq)
        if not attackers:
            continue
        cheapest = min(VALUES[board.piece_type_at(a)] or 100 for a in attackers)
        if not board.attackers(color, sq) or cheapest < VALUES[piece.piece_type]:
            res[sq] = piece.piece_type
    return res


def fork_targets(board: chess.Board, from_sq: chess.Square, color: chess.Color) -> list[str]:
    """Что атакует фигура на from_sq (принадлежит `color`): ценные цели соперника."""
    targets = []
    for sq in board.attacks(from_sq):
        p = board.piece_at(sq)
        if p and p.color != color and (p.piece_type == chess.KING or VALUES[p.piece_type] >= 3):
            targets.append(f"{PIECE_RU[p.piece_type]} {chess.square_name(sq)}")
    return targets


def classify(loss: int, th: Thresholds) -> str:
    if loss >= th.blunder:
        return "blunder"
    if loss >= th.mistake:
        return "mistake"
    if loss >= th.inaccuracy:
        return "inaccuracy"
    return "ok"


# ---------- основной анализ ----------

def analyse_pgn(pgn_text: str, my_color: str, engine: chess.engine.SimpleEngine, depth: int,
                time_control: str = "", th: Thresholds | None = None) -> list[dict]:
    th = th or Thresholds()
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("Не удалось прочитать PGN")
    mine = chess.WHITE if my_color == "white" else chess.BLACK
    base, inc = parse_time_control(time_control or game.headers.get("TimeControl", ""))
    limit = chess.engine.Limit(depth=depth)

    board = game.board()
    nodes = list(game.mainline())
    # Оценка каждой позиции один раз: «после хода i» == «до хода i+1».
    infos: list[chess.engine.InfoDict | None] = []
    boards: list[chess.Board] = [board.copy()]
    infos.append(None if board.is_game_over() else engine.analyse(board, limit))
    for node in nodes:
        board.push(node.move)
        boards.append(board.copy())
        infos.append(None if board.is_game_over() else engine.analyse(board, limit))

    last_clock = {chess.WHITE: base, chess.BLACK: base}
    out: list[dict] = []
    for i, node in enumerate(nodes):
        before, after = boards[i], boards[i + 1]
        mover = before.turn
        info_b, info_a = infos[i], infos[i + 1]
        if info_b is None:
            break
        ev_b = score_cp(info_b, mover)
        ev_a = terminal_cp(after, mover) if info_a is None else score_cp(info_a, mover)
        pv_best = info_b.get("pv", [])
        best = pv_best[0] if pv_best else None
        played = node.move
        loss = 0 if played == best else max(0, ev_b - ev_a)
        decided = int((ev_b >= th.decided and ev_a >= th.decided) or (ev_b <= -th.decided and ev_a <= -th.decided))
        label = "ok" if decided else classify(loss, th)

        # Часы: %clk в PGN — остаток после хода.
        clock = node.clock()
        spent = None
        if clock is not None and last_clock[mover] is not None:
            spent = round(max(0.0, last_clock[mover] + inc - clock), 1)
        if clock is not None:
            last_clock[mover] = clock
        low = int(clock is not None and base is not None and clock < max(30.0, 0.1 * base))

        rec = {
            "ply": i + 1,
            "move_no": before.fullmove_number,
            "side": "white" if mover == chess.WHITE else "black",
            "is_mine": int(mover == mine),
            "san": before.san(played),
            "best_san": before.san(best) if best else None,
            "eval_before": ev_b,
            "eval_after": ev_a,
            "loss": loss,
            "label": label,
            "decided": decided,
            "phase": phase_of(before),
            "clock_left": clock,
            "time_spent": spent,
            "low_clock": low,
            "best_line": None,
            "reply_line": None,
            "material_lost": None,
            "motifs": [],
            "missed_punish": 0,
            "fen_before": before.fen(),
        }
        if label in ("mistake", "blunder", "inaccuracy"):
            _explain(rec, before, after, pv_best, info_b, info_a, mover)
        out.append(rec)

    # Не наказал ошибку соперника: соперник зевнул, а мой следующий ход упустил это.
    for prev, cur in zip(out, out[1:]):
        if cur["is_mine"] and not prev["is_mine"] and prev["label"] == "blunder" and cur["loss"] >= th.mistake:
            cur["missed_punish"] = 1
            if "missed_punish" not in cur["motifs"]:
                cur["motifs"].append("missed_punish")
    return out


def _explain(rec: dict, before: chess.Board, after: chess.Board, pv_best: list[chess.Move],
             info_b, info_a, mover: chess.Color) -> None:
    """Добавить проверяемые факты, чтобы разбор не выдумывал тактику."""
    rec["best_line"] = line_san(before, pv_best)
    motifs: list[str] = []
    pv_reply = (info_a or {}).get("pv", []) if info_a else []
    rec["reply_line"] = line_san(after, pv_reply) if pv_reply else None

    # Материал: где оказываемся через несколько полуходов после лучшего хода и после сыгранного + ответа.
    after_best = play_line(before, pv_best, LINE_PLIES)
    after_played = play_line(after, pv_reply, LINE_PLIES - 1)
    diff = material_balance(after_best, mover) - material_balance(after_played, mover)
    rec["material_lost"] = float(diff) if diff > 0 else 0.0

    # Мат: пропустил свой или разрешил соперника.
    m_before = mate_in(info_b, mover)
    m_after = mate_in(info_a, mover) if info_a is not None else None
    if m_before is not None and m_before > 0 and (m_after is None or m_after <= 0):
        motifs.append(f"missed_mate_in_{m_before}")
    if m_after is not None and m_after < 0:
        motifs.append(f"allowed_mate_in_{-m_after}")

    # Подвисшая фигура: ответ соперника бьёт фигуру, которая после хода осталась без нормальной защиты.
    hang = hanging_squares(after, mover)
    if pv_reply:
        reply = pv_reply[0]
        if reply.to_square in hang and after.is_capture(reply):
            pt = hang[reply.to_square]
            motifs.append(f"hung_{PIECE_RU[pt]}_{chess.square_name(reply.to_square)}")
        # Вилка/двойной удар: ответ соперника нападает на 2+ ценные цели.
        b2 = after.copy(stack=False)
        if reply in b2.legal_moves:
            b2.push(reply)
            targets = fork_targets(b2, reply.to_square, not mover)
            if len(targets) >= 2:
                motifs.append("double_attack:" + ", ".join(targets))
            if b2.is_check():
                motifs.append("opponent_check_reply")
    # Лучший ход выигрывал материал, а сыгранный — нет.
    if pv_best and (material_balance(after_best, mover) - material_balance(before, mover)) >= 2 and diff >= 2:
        motifs.append("missed_material_win")
    rec["motifs"] = motifs
