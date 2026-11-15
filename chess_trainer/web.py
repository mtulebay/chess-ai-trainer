"""Локальный веб-интерфейс: рейтинг, партии по контролям, доска с разбором ходов, отчёт, дебюты."""
from __future__ import annotations

import atexit
import io
import threading
import time
import webbrowser

import chess
import chess.engine
import chess.pgn
import chess.svg
import requests
from flask import Flask, abort, jsonify, request, send_from_directory

from . import chesscom, coach, db, telegram
from .analyze import ANALYSIS_VERSION, Thresholds, analyse_pgn, parse_time_control
from .classify import BAD, CLASSES, classify_moves, game_accuracy
from .config import ROOT, Config, load_config
from .engine import open_engine
from .reports import latest_profile
from .stats import load_moves

STATIC = ROOT / "chess_trainer" / "static"
TIME_CLASSES = ["rapid", "blitz", "bullet", "daily"]
PHASE_RU = {"opening": "Дебют", "middlegame": "Миттельшпиль", "endgame": "Эндшпиль"}


class Engine:
    """Один процесс Stockfish на весь сервер, доступ — под замком."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self._eng: chess.engine.SimpleEngine | None = None

    def get(self) -> chess.engine.SimpleEngine:
        if self._eng is None:
            self._eng = open_engine(self.cfg)
            atexit.register(self.close)
        return self._eng

    def close(self) -> None:
        if self._eng is not None:
            try:
                self._eng.quit()
            except Exception:  # noqa: BLE001
                pass
            self._eng = None

    def lines(self, fen: str, depth: int, multipv: int = 3) -> list[dict]:
        board = chess.Board(fen)
        if board.is_game_over():
            return []
        with self.lock:
            try:
                res = self.get().analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
            except chess.engine.EngineTerminatedError:
                self._eng = None
                res = self.get().analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
        out = []
        for info in res:
            pv = info.get("pv", [])[:10]
            if not pv:
                continue
            score = info["score"].white()
            out.append({
                "uci": [m.uci() for m in pv],
                "san": board.variation_san(pv),
                "first_san": board.san(pv[0]),
                "eval": score_text(score),
                "cp_white": score.score(mate_score=10_000),
            })
        return out

    def analyse_game(self, pgn: str, color: str, tc: str) -> list[dict]:
        th = Thresholds(self.cfg.blunder_cp, self.cfg.mistake_cp, self.cfg.inaccuracy_cp, self.cfg.decided_cp)
        with self.lock:
            return analyse_pgn(pgn, color, self.get(), self.cfg.depth, tc, th)


def score_text(score: chess.engine.Score) -> str:
    """Оценка для шкалы: '+1.4', '-0.3', '#3' — это единственное место, где показываем цифры."""
    if score.is_mate():
        m = score.mate()
        return f"#{m}" if m > 0 else f"-#{-m}"
    return f"{score.score() / 100:+.1f}"


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__, static_folder=None)
    engine = Engine(cfg)
    app.config["engine"] = engine
    stats_cache: dict = {"t": 0, "data": None}

    def conn():
        return db.connect(cfg.db_path, check_same_thread=False)

    # ---------- статика ----------
    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_file(name):
        return send_from_directory(STATIC, name)

    @app.errorhandler(Exception)
    def on_error(e):
        code = getattr(e, "code", 500)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), code if isinstance(code, int) else 500

    # ---------- главная ----------
    @app.get("/api/overview")
    def overview():
        c = conn()
        live, err = None, None
        if cfg.username and time.time() - stats_cache["t"] > 600:
            try:
                stats_cache["data"] = chesscom.fetch_stats(cfg.username)
                stats_cache["t"] = time.time()
            except Exception as e:  # noqa: BLE001 — без сети покажем рейтинг из базы
                err = str(e)
        live = stats_cache["data"]
        modes = []
        for tc in TIME_CLASSES:
            rows = c.execute(
                "SELECT my_rating, result, end_time, analyzed FROM games WHERE time_class = ? ORDER BY end_time",
                (tc,)).fetchall()
            if not rows and not (live and tc in live):
                continue
            last30 = [r for r in rows if r["end_time"] >= time.time() - 30 * 86400]
            modes.append({
                "time_class": tc,
                "rating": (live or {}).get(tc, {}).get("rating") or (rows[-1]["my_rating"] if rows else None),
                "best": (live or {}).get(tc, {}).get("best"),
                "games": len(rows),
                "analyzed": sum(1 for r in rows if r["analyzed"] == 1),
                "last30": {"win": sum(r["result"] == "win" for r in last30),
                           "loss": sum(r["result"] == "loss" for r in last30),
                           "draw": sum(r["result"] == "draw" for r in last30)},
                "history": [r["my_rating"] for r in rows[-60:] if r["my_rating"]],
            })
        return jsonify({"username": cfg.username, "modes": modes, "live": bool(live), "live_error": err,
                        "profile": latest_profile(c)})

    @app.post("/api/sync")
    def sync():
        c = conn()
        new = sum(db.upsert_game(c, g) for g in chesscom.fetch_games(cfg.username, 2))
        c.commit()
        stats_cache["t"] = 0
        return jsonify({"new": new})

    # ---------- список партий ----------
    @app.get("/api/games")
    def games():
        tc = request.args.get("time_class", "rapid")
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = int(request.args.get("offset", 0))
        result = request.args.get("result")
        q = "SELECT * FROM games WHERE time_class = ?"
        args: list = [tc]
        if result in ("win", "loss", "draw"):
            q += " AND result = ?"
            args.append(result)
        q += " ORDER BY end_time DESC LIMIT ? OFFSET ?"
        rows = conn().execute(q, (*args, limit, offset)).fetchall()
        return jsonify([{
            "id": r["id"], "end_time": r["end_time"], "color": r["color"], "opponent": r["opponent"],
            "opp_rating": r["opp_rating"], "my_rating": r["my_rating"], "result": r["result"],
            "result_detail": r["result_detail"], "opening": opening_title(r["opening"]),
            "time_control": r["time_control"], "analyzed": r["analyzed"] == 1,
        } for r in rows])

    # ---------- партия ----------
    def game_row(gid: str):
        r = conn().execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
        if r is None:
            abort(404)
        return r

    def game_payload(g) -> dict:
        game = chess.pgn.read_game(io.StringIO(g["pgn"]))
        board = game.board()
        start_fen = board.fen()
        moves_db = {m["ply"]: m for m in load_moves(conn(), g["id"])} if g["analyzed"] == 1 else {}
        classified = classify_moves(list(moves_db.values()), g["opening"]) if moves_db else []
        by_ply = {m["ply"]: m for m in classified}
        moves = []
        for i, node in enumerate(game.mainline(), start=1):
            san = board.san(node.move)
            board.push(node.move)
            m = by_ply.get(i, {})
            moves.append({
                "ply": i, "san": san, "uci": node.move.uci(), "fen": board.fen(),
                "clock": node.clock(),
                "cls": m.get("cls"), "cls_name": m.get("cls_name"), "cls_icon": m.get("cls_icon"),
                "explain": m.get("explain"), "best_san": m.get("best_san"), "best_uci": m.get("best_uci"),
                "best_line": m.get("best_line"), "reply_line": m.get("reply_line"),
                "eval_white": m.get("eval_after_white"), "wp_white": m.get("wp_after_white"),
                "phase": m.get("phase"), "time_spent": m.get("time_spent"), "is_mine": m.get("is_mine"),
            })
        h = game.headers
        return {
            "id": g["id"], "url": g["url"], "color": g["color"], "result": g["result"],
            "result_detail": g["result_detail"], "time_class": g["time_class"],
            "time_control": g["time_control"], "end_time": g["end_time"],
            "opening": opening_title(g["opening"]),
            "white": {"name": h.get("White"),
                      "rating": h.get("WhiteElo") or (g["my_rating"] if g["color"] == "white" else g["opp_rating"])},
            "black": {"name": h.get("Black"),
                      "rating": h.get("BlackElo") or (g["my_rating"] if g["color"] == "black" else g["opp_rating"])},
            "start_fen": start_fen, "moves": moves,
            "analyzed": g["analyzed"] == 1,
            "outdated": g["analyzed"] == 1 and (g["analysis_version"] or 1) < ANALYSIS_VERSION,
            "classes": {k: {"name": v[0], "icon": v[1]} for k, v in CLASSES.items()},
            "_classified": classified,
        }

    @app.get("/api/game/<gid>")
    def game(gid):
        p = game_payload(game_row(gid))
        p.pop("_classified")
        return jsonify(p)

    @app.post("/api/game/<gid>/analyze")
    def analyze_game(gid):
        g = game_row(gid)
        moves = engine.analyse_game(g["pgn"], g["color"], g["time_control"])
        db.save_analysis(conn(), gid, cfg.depth, moves)
        return jsonify({"ok": True})

    @app.get("/api/game/<gid>/report")
    def report(gid):
        g = game_row(gid)
        if g["analyzed"] != 1:
            abort(409, "Партия ещё не проанализирована")
        p = game_payload(g)
        cl = p["_classified"]
        my = g["color"]
        opp = "black" if my == "white" else "white"
        def counts(side):
            return {k: sum(1 for m in cl if m["side"] == side and m["cls"] == k) for k in CLASSES}
        phases = {}
        for ph in ("opening", "middlegame", "endgame"):
            acc = [m["accuracy"] for m in cl if m["side"] == my and m["phase"] == ph]
            if acc:
                phases[PHASE_RU[ph]] = round(sum(acc) / len(acc), 1)
        key = sorted((m for m in cl if m["side"] == my and m["cls"] in BAD + ("brilliant", "great")),
                     key=lambda m: (m["cls"] not in ("brilliant", "great"), -m["ep_loss"]))[:8]
        base, _ = parse_time_control(g["time_control"])
        mine = [m for m in cl if m["side"] == my]
        spent = [m["time_spent"] for m in mine if m["time_spent"] is not None]
        note = conn().execute("SELECT text FROM game_notes WHERE game_id = ? AND kind = 'report'",
                              (gid,)).fetchone()
        return jsonify({
            "accuracy": {"me": game_accuracy(cl, my), "opponent": game_accuracy(cl, opp)},
            "counts": {"me": counts(my), "opponent": counts(opp)},
            "phases": phases,
            "key_moments": [{"ply": m["ply"], "move": f'{m["move_no"]}{"." if m["side"] == "white" else "..."} {m["san"]}',
                             "cls": m["cls"], "cls_name": m["cls_name"], "cls_icon": m["cls_icon"],
                             "explain": m["explain"]} for m in sorted(key, key=lambda m: m["ply"])],
            "time": {"base": base, "avg_spent": round(sum(spent) / len(spent), 1) if spent else None,
                     "fast_errors": sum(1 for m in mine if m["cls"] in ("mistake", "blunder", "miss")
                                        and m["time_spent"] is not None and m["time_spent"] < 3)},
            "narrative": note["text"] if note else None,
        })

    @app.post("/api/game/<gid>/report/narrative")
    def report_narrative(gid):
        g = game_row(gid)
        p = game_payload(g)
        cl = p["_classified"]
        facts = {
            "партия": {k: p[k] for k in ("opening", "result", "result_detail", "time_control", "color")},
            "белые": p["white"], "чёрные": p["black"],
            "точность": {"моя": game_accuracy(cl, g["color"]),
                         "соперника": game_accuracy(cl, "black" if g["color"] == "white" else "white")},
            "ходы": [{"ход": f'{m["move_no"]}{"." if m["side"] == "white" else "..."} {m["san"]}',
                      "чей": "мой" if m["is_mine"] else "соперника", "оценка": m["cls_name"],
                      "лучше": m["best_san"], "вариант": m["best_line"], "ответ_соперника": m["reply_line"],
                      "мотивы": m["motifs"], "фаза": m["phase"], "секунд": m["time_spent"]}
                     for m in cl if m["cls"] not in ("book", "best", "excellent", "good")],
            "профиль_слабых_мест": latest_profile(conn()),
        }
        text = coach.ask(cfg, coach.GAME_REPORT_PROMPT, facts)
        c = conn()
        c.execute("INSERT OR REPLACE INTO game_notes VALUES (?, 'report', ?, ?)", (gid, text, int(time.time())))
        c.commit()
        return jsonify({"text": text})

    @app.post("/api/game/<gid>/explain/<int:ply>")
    def explain_move(gid, ply):
        kind = f"explain:{ply}"
        c = conn()
        cached = c.execute("SELECT text FROM game_notes WHERE game_id = ? AND kind = ?", (gid, kind)).fetchone()
        if cached and not request.args.get("refresh"):
            return jsonify({"text": cached["text"]})
        g = game_row(gid)
        cl = {m["ply"]: m for m in game_payload(g)["_classified"]}
        m = cl.get(ply)
        if not m:
            abort(404)
        lines = engine.lines(m["fen_before"], cfg.depth, 3)
        facts = {
            "позиция_до_хода_FEN": m["fen_before"], "ходят": "белые" if m["side"] == "white" else "чёрные",
            "чей_ход": "мой" if m["is_mine"] else "соперника",
            "сыграно": m["san"], "оценка_хода": m["cls_name"], "лучший_ход": m["best_san"],
            "сильнейшие_варианты": [l["san"] for l in lines],
            "ответ_соперника_на_сыгранный": m["reply_line"], "мотивы": m["motifs"],
            "материал_теряется": m["material_lost"], "фаза": m["phase"],
            "секунд_на_ход": m["time_spent"], "краткое_объяснение": m["explain"],
        }
        text = coach.ask(cfg, coach.MOVE_PROMPT, facts)
        c.execute("INSERT OR REPLACE INTO game_notes VALUES (?, ?, ?, ?)", (gid, kind, text, int(time.time())))
        c.commit()
        return jsonify({"text": text})

    # ---------- доска и движок ----------
    @app.get("/api/board.svg")
    def board_svg():
        fen = request.args.get("fen") or chess.STARTING_FEN
        board = chess.Board(fen)
        flip = request.args.get("flip") == "1"
        last = request.args.get("last")
        arrows = []
        for i, a in enumerate(filter(None, (request.args.get("arrows") or "").split(","))):
            try:
                mv = chess.Move.from_uci(a)
                arrows.append(chess.svg.Arrow(mv.from_square, mv.to_square,
                                              color="#15781bcc" if i == 0 else "#2b6cb088"))
            except ValueError:
                pass
        fill = {}
        sel = request.args.get("sel")
        if sel:
            sq = chess.parse_square(sel)
            fill[sq] = "#f6f66988"
            for mv in board.legal_moves:
                if mv.from_square == sq:
                    fill[mv.to_square] = "#9ad1ff88"
        svg = chess.svg.board(
            board, orientation=chess.BLACK if flip else chess.WHITE,
            lastmove=chess.Move.from_uci(last) if last else None,
            check=board.king(board.turn) if board.is_check() else None,
            arrows=arrows, fill=fill, size=560,
            colors={"square light": "#eeeed2", "square dark": "#769656",
                    "square light lastmove": "#f6f669", "square dark lastmove": "#baca2b"},
        )
        return app.response_class(svg, mimetype="image/svg+xml")

    @app.post("/api/lines")
    def lines():
        body = request.get_json(force=True)
        depth = min(int(body.get("depth") or cfg.depth), 22)
        return jsonify(engine.lines(body["fen"], depth, int(body.get("multipv", 3))))

    @app.post("/api/move")
    def move():
        body = request.get_json(force=True)
        board = chess.Board(body["fen"])
        frm, to = chess.parse_square(body["from"]), chess.parse_square(body["to"])
        mv = chess.Move(frm, to)
        piece = board.piece_at(frm)
        if piece and piece.piece_type == chess.PAWN and chess.square_rank(to) in (0, 7):
            mv = chess.Move(frm, to, promotion=chess.QUEEN)
        if mv not in board.legal_moves:
            return jsonify({"legal": False})
        san = board.san(mv)
        board.push(mv)
        return jsonify({"legal": True, "san": san, "uci": mv.uci(), "fen": board.fen()})

    @app.post("/api/play")
    def play():
        """Сыграть последовательность uci-ходов из позиции (для просмотра варианта движка)."""
        body = request.get_json(force=True)
        board = chess.Board(body["fen"])
        out = []
        for u in body["uci"]:
            mv = chess.Move.from_uci(u)
            if mv not in board.legal_moves:
                break
            san = board.san(mv)
            board.push(mv)
            out.append({"san": san, "uci": u, "fen": board.fen()})
        return jsonify(out)

    # ---------- дебюты ----------
    @app.get("/api/openings")
    def openings():
        c = conn()
        rows = c.execute("SELECT * FROM openings ORDER BY color DESC, id").fetchall()
        out = []
        for r in rows:
            match = (r["match"] or r["name"]).lower()
            games = [g for g in c.execute("SELECT id, opening, result, color, end_time FROM games "
                                          "WHERE color = ? ORDER BY end_time DESC", (r["color"],))
                     if match in (g["opening"] or "").lower()]
            res = [g["result"] for g in games]
            out.append({
                "id": r["id"], "name": r["name"], "color": r["color"], "match": r["match"], "notes": r["notes"],
                "games": len(games), "win": res.count("win"), "loss": res.count("loss"), "draw": res.count("draw"),
                "recent_games": [{"id": g["id"], "result": g["result"], "end_time": g["end_time"],
                                  "opening": opening_title(g["opening"])} for g in games[:10]],
                "resources": [dict(x) for x in c.execute(
                    "SELECT id, title, url, kind, lang, why FROM opening_resources WHERE opening_id = ? "
                    "ORDER BY id", (r["id"],))],
            })
        return jsonify(out)

    @app.post("/api/openings")
    def add_opening():
        b = request.get_json(force=True)
        if not b.get("name") or b.get("color") not in ("white", "black"):
            abort(400, "Нужны название и цвет")
        c = conn()
        c.execute("INSERT INTO openings(name, color, match, notes) VALUES (?, ?, ?, ?)",
                  (b["name"].strip(), b["color"], (b.get("match") or "").strip() or None, b.get("notes")))
        c.commit()
        return jsonify({"ok": True})

    @app.delete("/api/openings/<int:oid>")
    def del_opening(oid):
        c = conn()
        c.execute("DELETE FROM openings WHERE id = ?", (oid,))
        c.commit()
        return jsonify({"ok": True})

    @app.post("/api/openings/<int:oid>/find")
    def find_resources(oid):
        c = conn()
        o = c.execute("SELECT * FROM openings WHERE id = ?", (oid,)).fetchone()
        if o is None:
            abort(404)
        rating = c.execute("SELECT my_rating FROM games WHERE time_class IN ('rapid','blitz') "
                           "ORDER BY end_time DESC LIMIT 1").fetchone()
        items = coach.find_resources(cfg, o["name"], o["color"], rating["my_rating"] if rating else None)
        saved, dropped = 0, []
        for it in items:
            if not link_alive(it["url"]):
                dropped.append(it["url"])
                continue
            cur = c.execute("INSERT OR IGNORE INTO opening_resources(opening_id, title, url, kind, lang, why, "
                            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (oid, it.get("title") or it["url"], it["url"], it.get("kind"), it.get("lang"),
                             it.get("why"), int(time.time())))
            saved += cur.rowcount
        c.commit()
        return jsonify({"saved": saved, "dropped": dropped})

    @app.delete("/api/resources/<int:rid>")
    def del_resource(rid):
        c = conn()
        c.execute("DELETE FROM opening_resources WHERE id = ?", (rid,))
        c.commit()
        return jsonify({"ok": True})

    @app.post("/api/openings/<int:oid>/send")
    def send_resources(oid):
        c = conn()
        o = c.execute("SELECT * FROM openings WHERE id = ?", (oid,)).fetchone()
        res = c.execute("SELECT * FROM opening_resources WHERE opening_id = ? ORDER BY id", (oid,)).fetchall()
        if o is None or not res:
            abort(400, "Нет материалов — сначала нажмите «Найти материалы»")
        icon = {"video": "🎬", "article": "📄", "course": "🎓"}
        lines = [f'Дебют: {o["name"]} ({"белыми" if o["color"] == "white" else "чёрными"})', ""]
        for r in res:
            lines.append(f'{icon.get(r["kind"], "•")} {r["title"]}\n{r["url"]}' + (f'\n{r["why"]}' if r["why"] else ""))
            lines.append("")
        telegram.send(cfg.tg_token, cfg.tg_chat_id, "\n".join(lines).strip())
        return jsonify({"ok": True, "sent": len(res)})

    return app


def opening_title(op: str | None) -> str:
    """'Caro Kann Defense Advance Variation 3...c5' -> 'Caro Kann Defense Advance Variation'."""
    if not op:
        return ""
    import re
    return re.sub(r"\s*\d+\.(\.\.)?\s*\S+\s*$", "", op).strip() or op


def link_alive(url: str) -> bool:
    """Отсеять выдуманные ссылки: YouTube проверяем через oEmbed, остальное — GET."""
    try:
        if "youtube.com" in url or "youtu.be" in url:
            r = requests.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=10)
            return r.status_code == 200
        r = requests.get(url, timeout=10, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (chess-ai-trainer link check)"})
        return r.status_code < 400
    except requests.RequestException:
        return False


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    app = create_app()
    url = f"http://{host}:{port}"
    print(f"Интерфейс: {url}  (Ctrl+C — остановить)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, threaded=True, debug=False)
