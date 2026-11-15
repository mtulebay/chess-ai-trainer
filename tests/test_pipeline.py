import json
import time

import chess
import pytest

from chess_trainer import chesscom, db, reports, telegram
from chess_trainer.analyze import Thresholds, analyse_pgn, parse_time_control, phase_of
from chess_trainer.config import load_config
from chess_trainer.engine import find_stockfish, open_engine

SCHOLAR = """[Event "Live Chess"]
[White "opp"]
[Black "me"]
[Result "1-0"]
[TimeControl "180+2"]

1. e4 {[%clk 0:03:01]} 1... e5 {[%clk 0:03:00]} 2. Bc4 {[%clk 0:02:59]} 2... Nc6 {[%clk 0:02:58]}
3. Qh5 {[%clk 0:02:57]} 3... Nf6 {[%clk 0:02:59]} 4. Qxf7# {[%clk 0:02:55]} 1-0
"""

HANG_QUEEN = """[Event "Live Chess"]
[White "me"]
[Black "opp"]
[Result "0-1"]
[TimeControl "600"]

1. e4 {[%clk 0:09:58]} 1... e5 {[%clk 0:09:57]} 2. Qh5 {[%clk 0:09:50]} 2... Nc6 {[%clk 0:09:40]}
3. Qxe5+ {[%clk 0:09:49]} 3... Nxe5 {[%clk 0:09:30]} 0-1
"""


@pytest.fixture(scope="module")
def cfg(tmp_path_factory, monkeypatch_module):
    monkeypatch_module.setenv("DATA_DIR", str(tmp_path_factory.mktemp("data")))
    monkeypatch_module.setenv("LLM_BACKEND", "none")
    monkeypatch_module.setenv("CHESSCOM_USERNAME", "me")
    monkeypatch_module.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch_module.setenv("ENGINE_DEPTH", "10")
    monkeypatch_module.setenv("ENGINE_THREADS", "1")  # детерминированный поиск
    return load_config()


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def engine(cfg):
    try:
        find_stockfish(cfg)
    except FileNotFoundError:
        pytest.skip("stockfish не установлен")
    with open_engine(cfg) as e:
        yield e


def test_time_control():
    assert parse_time_control("180+2") == (180, 2)
    assert parse_time_control("600") == (600, 0)
    assert parse_time_control("1/86400") == (None, 0)


def test_phase():
    assert phase_of(chess.Board()) == "opening"
    assert phase_of(chess.Board("8/5k2/8/8/8/8/2R5/4K3 w - - 0 50")) == "endgame"


def test_scholar_blunder(engine):
    moves = analyse_pgn(SCHOLAR, "black", engine, 10, "180+2")
    nf6 = next(m for m in moves if m["san"] == "Nf6")
    assert nf6["is_mine"] and nf6["label"] == "blunder"
    assert any(x.startswith("allowed_mate") for x in nf6["motifs"])
    assert nf6["time_spent"] == pytest.approx(1.0)   # 178 + 2 - 179
    assert nf6["phase"] == "opening"


def test_hung_queen(engine):
    moves = analyse_pgn(HANG_QUEEN, "white", engine, 10, "600", Thresholds())
    qxe5 = next(m for m in moves if m["san"] == "Qxe5+")
    assert qxe5["label"] == "blunder"
    assert qxe5["material_lost"] >= 7
    assert any(x.startswith("hung_ферзь") for x in qxe5["motifs"])


def test_normalize():
    raw = {"uuid": "u1", "url": "https://x", "end_time": 1, "time_class": "blitz",
           "time_control": "180+2", "rated": True, "rules": "chess", "pgn": "1. e4 *",
           "eco": "https://www.chess.com/openings/Italian-Game",
           "white": {"username": "Me", "rating": 1500, "result": "timeout"},
           "black": {"username": "opp", "rating": 1510, "result": "win"}}
    g = chesscom.normalize(raw, "me")
    assert g["color"] == "white" and g["result"] == "loss" and g["result_detail"] == "timeout"
    assert g["opening"] == "Italian Game"
    assert chesscom.normalize({**raw, "rules": "chess960"}, "me") is None


def test_split():
    parts = telegram.split("a" * 9000)
    assert len(parts) == 3 and all(len(p) <= 4000 for p in parts)


def test_daily_and_weekly_end_to_end(cfg, engine):
    conn = db.connect(cfg.db_path)
    now = int(time.time())
    for i, (pgn, color, tc, tcl) in enumerate([(SCHOLAR, "black", "180+2", "blitz"),
                                              (HANG_QUEEN, "white", "600", "rapid"),
                                              (SCHOLAR, "black", "60", "bullet")]):
        db.upsert_game(conn, {"id": f"g{i}", "url": f"https://chess.com/g{i}", "end_time": now - 100 - i,
                              "time_class": tcl, "time_control": tc, "rated": 1, "color": color,
                              "opponent": "opp", "my_rating": 1500 + i, "opp_rating": 1500,
                              "result": "loss", "result_detail": "checkmated", "opening": "Test", "pgn": pgn})
        db.save_analysis(conn, f"g{i}", 10, analyse_pgn(pgn, color, engine, 10, tc))
    reports.update_profile(conn, cfg, with_narrative=False, now=now)
    data = reports.build_daily(conn, cfg, now - 86400, now)
    assert len(data["партии_сегодня"]) == 2           # пуля исключена
    json.dumps(data, ensure_ascii=False)               # сериализуется для LLM
    text = reports.run_daily(conn, cfg, now=now)
    assert "Nf6" in text and "Qxe5+" in text
    assert reports.run_weekly(conn, cfg, now=now)


def test_pick_asset_macos(monkeypatch):
    from chess_trainer import engine as eng
    assets = [{"name": n} for n in ["stockfish-ubuntu-x86-64-avx2.tar", "stockfish-macos-m1-apple-silicon.tar",
                                    "stockfish-macos-x86-64-avx2.tar", "stockfish-windows-x86-64.zip"]]
    monkeypatch.setattr(eng.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(eng.platform, "machine", lambda: "arm64")
    assert eng._pick_asset(assets)["name"] == "stockfish-macos-m1-apple-silicon.tar"
    monkeypatch.setattr(eng.platform, "machine", lambda: "x86_64")
    assert eng._pick_asset(assets)["name"] == "stockfish-macos-x86-64-avx2.tar"
    # Новые релизы: одна универсальная сборка для обеих архитектур.
    new = [{"name": "stockfish-macos-universal.tar.gz"}, {"name": "stockfish-ubuntu-x86-64-avx2.tar"}]
    for m in ("arm64", "x86_64"):
        monkeypatch.setattr(eng.platform, "machine", lambda m=m: m)
        assert eng._pick_asset(new)["name"] == "stockfish-macos-universal.tar.gz"


def test_broken_binary_skipped(cfg, tmp_path, monkeypatch):
    from chess_trainer import engine as eng
    bad = tmp_path / "stockfish"
    bad.write_bytes(b"\x7fELFgarbage")
    bad.chmod(0o755)
    assert not eng._runs(str(bad))
