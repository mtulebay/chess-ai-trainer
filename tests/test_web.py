import os
import time

import pytest

from chess_trainer import db
from chess_trainer.analyze import analyse_pgn
from chess_trainer.classify import book_plies, classify_moves, win_prob
from chess_trainer.config import load_config
from chess_trainer.engine import find_stockfish, open_engine
from test_pipeline import HANG_QUEEN

OPERA = """[White "me"]
[Black "duke"]
[Result "1-0"]
[TimeControl "600"]

1. e4 e5 2. Nf3 d6 3. d4 Bg4 4. dxe5 Bxf3 5. Qxf3 dxe5 6. Bc4 Nf6 7. Qb3 Qe7 8. Nc3 c6 9. Bg5 b5
10. Nxb5 cxb5 11. Bxb5+ Nbd7 12. O-O-O Rd8 13. Rxd7 Rxd7 14. Rd1 Qe6 15. Bxd7+ Nxd7 16. Qb8+ Nxb8 17. Rd8# 1-0
"""


def test_book_plies():
    assert book_plies("Caro Kann Defense Advance Variation 3...c5") == 6
    assert book_plies("Italian Game 4.d3") == 7
    assert book_plies("Kings Pawn Opening") == 0


def test_win_prob_symmetric():
    assert win_prob(0) == pytest.approx(0.5)
    assert win_prob(300) == pytest.approx(1 - win_prob(-300))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("DATA_DIR", str(tmp_path_factory.mktemp("web")))
    mp.setenv("LLM_BACKEND", "none")
    mp.setenv("CHESSCOM_USERNAME", "")
    mp.setenv("ENGINE_DEPTH", "10")
    mp.setenv("ENGINE_THREADS", "1")
    cfg = load_config()
    try:
        find_stockfish(cfg)
    except FileNotFoundError:
        pytest.skip("stockfish не установлен")
    conn = db.connect(cfg.db_path)
    now = int(time.time())
    for gid, pgn, color, op in [("opera", OPERA, "white", "Philidor Defense 2...d6"),
                                ("hang", HANG_QUEEN, "white", "")]:
        db.upsert_game(conn, dict(id=gid, url="", end_time=now, time_class="rapid", time_control="600", rated=1,
                                  color=color, opponent="x", my_rating=1500, opp_rating=1490, result="win",
                                  result_detail="", opening=op, pgn=pgn))
    conn.commit()
    from chess_trainer.web import create_app
    app = create_app(cfg)
    yield app.test_client()
    app.config["engine"].close()
    mp.undo()


def test_analyze_and_classify(client):
    assert client.get("/api/game/opera").get_json()["analyzed"] is False
    assert client.post("/api/game/opera/analyze").get_json()["ok"]
    g = client.get("/api/game/opera").get_json()
    cls = {m["san"]: m["cls"] for m in g["moves"]}
    assert g["moves"][0]["cls"] == "book"
    assert cls["Nxb5"] == "brilliant"          # жертва коня
    assert g["moves"][-1]["cls"] == "best"      # мат


def test_blunder_and_report(client):
    client.post("/api/game/hang/analyze")
    g = client.get("/api/game/hang").get_json()
    q = next(m for m in g["moves"] if m["san"] == "Qxe5+")
    assert q["cls"] == "blunder" and "под боем" in q["explain"]
    r = client.get("/api/game/hang/report").get_json()
    assert r["counts"]["me"]["blunder"] == 1 and r["accuracy"]["me"] < 80


def test_board_moves_lines(client):
    start = client.get("/api/game/hang").get_json()["start_fen"]
    assert client.get("/api/board.svg", query_string={"fen": start, "arrows": "e2e4"}).status_code == 200
    assert not client.post("/api/move", json={"fen": start, "from": "e2", "to": "e5"}).get_json()["legal"]
    assert client.post("/api/move", json={"fen": start, "from": "e2", "to": "e4"}).get_json()["san"] == "e4"
    ls = client.post("/api/lines", json={"fen": start, "multipv": 2}).get_json()
    assert len(ls) == 2 and ls[0]["eval"].startswith(("+", "-"))


def test_openings_crud(client):
    assert client.post("/api/openings", json={"name": "Филидор", "color": "white", "match": "Philidor"}).status_code == 200
    o = client.get("/api/openings").get_json()[0]
    assert o["games"] == 1 and o["win"] == 1
    # без LLM поиск должен вернуть понятную ошибку, а не упасть
    r = client.post(f"/api/openings/{o['id']}/find")
    assert r.status_code == 500 and "LLM не настроен" in r.get_json()["error"]
    assert client.delete(f"/api/openings/{o['id']}").status_code == 200
