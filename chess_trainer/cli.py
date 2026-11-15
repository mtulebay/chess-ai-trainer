"""Командная строка: python -m chess_trainer <команда>."""
from __future__ import annotations

import argparse
import sys
import time

from . import chesscom, db, reports, telegram
from .analyze import Thresholds, analyse_pgn
from .config import Config, load_config
from .engine import download_stockfish, find_stockfish, open_engine


def cmd_fetch(cfg: Config, conn, months: int) -> int:
    if not cfg.username:
        sys.exit("CHESSCOM_USERNAME не задан")
    new = sum(db.upsert_game(conn, g) for g in chesscom.fetch_games(cfg.username, months))
    conn.commit()
    print(f"[fetch] новых партий: {new}")
    return new


def cmd_analyze(cfg: Config, conn, limit: int | None = None, retry_failed: bool = False,
                days: int | None = None) -> int:
    states = "(0, -1)" if retry_failed else "(0)"
    excl = tuple(cfg.exclude_time_classes) or ("",)
    q = (f"SELECT id, pgn, color, time_control FROM games WHERE analyzed IN {states} "
         f"AND time_class NOT IN ({','.join('?' * len(excl))}) AND end_time >= ? ORDER BY end_time DESC")
    since = int(time.time()) - days * 86400 if days else 0
    rows = conn.execute(q, (*excl, since)).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        print("[analyze] нечего анализировать")
        return 0
    th = Thresholds(cfg.blunder_cp, cfg.mistake_cp, cfg.inaccuracy_cp, cfg.decided_cp)
    done = 0
    with open_engine(cfg) as eng:
        for r in rows:
            t0 = time.time()
            try:
                moves = analyse_pgn(r["pgn"], r["color"], eng, cfg.depth, r["time_control"], th)
                db.save_analysis(conn, r["id"], cfg.depth, moves)
                done += 1
                print(f"[analyze] {r['id']}: {len(moves)} полуходов за {time.time() - t0:.0f}с")
            except Exception as e:  # noqa: BLE001
                db.mark_failed(conn, r["id"], f"{type(e).__name__}: {e}")
                print(f"[analyze] {r['id']}: ошибка {e}", file=sys.stderr)
    return done


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="chess_trainer")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup-engine", help="скачать Stockfish с GitHub в ./bin")
    sub.add_parser("check", help="проверить окружение")
    f = sub.add_parser("fetch", help="загрузить партии с Chess.com")
    f.add_argument("--months", type=int, default=2, help="сколько последних месячных архивов (0 = все)")
    a = sub.add_parser("analyze", help="проанализировать новые партии")
    a.add_argument("--limit", type=int)
    a.add_argument("--retry-failed", action="store_true")
    a.add_argument("--days", type=int, help="только партии за последние N дней (по умолчанию все)")
    d = sub.add_parser("daily", help="загрузить, проанализировать, прислать вечерний разбор")
    d.add_argument("--dry-run", action="store_true", help="не отправлять в Telegram, напечатать")
    w = sub.add_parser("weekly", help="недельная сводка + обновление профиля")
    w.add_argument("--dry-run", action="store_true")
    pr = sub.add_parser("profile", help="пересчитать профиль слабых мест")
    pr.add_argument("--narrative", action="store_true", help="переписать текст профиля через LLM")
    pr.add_argument("--send", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "setup-engine":
        print(f"Stockfish: {download_stockfish()}")
        return

    cfg = load_config()
    conn = db.connect(cfg.db_path)

    if args.cmd == "check":
        try:
            sf = find_stockfish(cfg)
        except FileNotFoundError as e:
            sf = f"НЕТ — {e}"
        print(f"ник: {cfg.username or '—'}\nstockfish: {sf}\n"
              f"telegram: {'да' if cfg.tg_token and cfg.tg_chat_id else 'нет'}\n"
              f"LLM: {reports.coach._resolve_backend(cfg)}\nБД: {cfg.db_path}")
        pending = conn.execute("SELECT time_class, COUNT(*) n FROM games WHERE analyzed = 0 GROUP BY 1").fetchall()
        if pending:
            print("не проанализировано: " + ", ".join(f"{r['time_class']} {r['n']}" for r in pending))
    elif args.cmd == "fetch":
        cmd_fetch(cfg, conn, args.months)
    elif args.cmd == "analyze":
        cmd_analyze(cfg, conn, args.limit, args.retry_failed, args.days)
    elif args.cmd == "daily":
        cmd_fetch(cfg, conn, 2)
        cmd_analyze(cfg, conn, days=cfg.profile_window_days)
        reports.update_profile(conn, cfg, with_narrative=False)  # цифры профиля — каждый день
        text = reports.run_daily(conn, cfg)
        if text is None:
            print("[daily] сегодня партий (без пули) не было")
        elif args.dry_run:
            print(text)
        else:
            telegram.send(cfg.tg_token, cfg.tg_chat_id, text)
    elif args.cmd == "weekly":
        cmd_fetch(cfg, conn, 2)
        cmd_analyze(cfg, conn, days=cfg.profile_window_days)
        text = reports.run_weekly(conn, cfg)
        if text is None:
            print("[weekly] за неделю партий не было")
        elif args.dry_run:
            print(text)
        else:
            telegram.send(cfg.tg_token, cfg.tg_chat_id, text)
    elif args.cmd == "profile":
        reports.update_profile(conn, cfg, with_narrative=args.narrative)
        text = reports.latest_profile(conn) or "Профиль пока без текста (запустите с --narrative)."
        if args.send:
            telegram.send(cfg.tg_token, cfg.tg_chat_id, text)
        else:
            print(text)


if __name__ == "__main__":
    main()
