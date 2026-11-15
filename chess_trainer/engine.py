"""Поиск Stockfish и скачивание готовой сборки с GitHub (если нет прав на установку)."""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

import chess.engine
import requests

from .config import ROOT, Config

BIN_DIR = ROOT / "bin"
RELEASES = "https://api.github.com/repos/official-stockfish/Stockfish/releases/latest"


def find_stockfish(cfg: Config) -> str:
    candidates = [cfg.stockfish_path, shutil.which("stockfish") or "", "/opt/homebrew/bin/stockfish",
                  "/usr/local/bin/stockfish", "/usr/games/stockfish"]
    candidates += [str(p) for p in sorted(BIN_DIR.glob("stockfish*")) if p.is_file()]
    broken = []
    for c in dict.fromkeys(c for c in candidates if c):
        if Path(c).is_file() and os.access(c, os.X_OK):
            if _runs(c):
                return c
            broken.append(c)
    hint = (f" Найдены, но не запускаются (не та платформа?): {', '.join(broken)}." if broken else "")
    raise FileNotFoundError(
        "Stockfish не найден." + hint + " macOS: brew install stockfish; Linux: apt install stockfish; "
        "или укажите STOCKFISH_PATH, или выполните: python -m chess_trainer setup-engine"
    )


def _runs(path: str) -> bool:
    """Бинарник реально запускается на этой машине (а не собран под другую ОС/процессор)."""
    try:
        out = subprocess.run([path], input="uci\nquit\n", capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "uciok" in out.stdout


def open_engine(cfg: Config) -> chess.engine.SimpleEngine:
    eng = chess.engine.SimpleEngine.popen_uci(find_stockfish(cfg))
    eng.configure({"Threads": cfg.threads, "Hash": cfg.hash_mb})
    return eng


def _cpu_flags() -> set[str]:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("flags"):
                return set(line.split(":", 1)[1].split())
    except OSError:
        pass
    return set()


def _pick_asset(assets: list[dict]) -> dict:
    system = platform.system()
    machine = platform.machine().lower()
    names = {a["name"]: a for a in assets}
    arm = machine in ("aarch64", "arm64")
    if system == "Darwin":
        os_key = "macos"
        arch_keys = ("apple-silicon", "m1", "arm") if arm else ("x86-64-bmi2", "x86-64-avx2", "x86-64")
        mac = [n for n in names if "macos" in n]
        # Новые релизы — одна универсальная сборка (stockfish-macos-universal.tar.gz), старые — по архитектурам.
        prefs = [n for n in mac if "universal" in n]
        prefs += [n for k in arch_keys for n in mac if k in n and n not in prefs]
    elif system == "Linux":
        os_key = "ubuntu"
        if arm:
            prefs = ["stockfish-android-armv8.tar", "stockfish-ubuntu-armv8.tar"]
        else:
            flags = _cpu_flags()
            prefs = []
            if {"avx2", "bmi2"} <= flags:
                prefs.append("stockfish-ubuntu-x86-64-bmi2.tar")
            if "avx2" in flags:
                prefs.append("stockfish-ubuntu-x86-64-avx2.tar")
            if "sse4_1" in flags and "popcnt" in flags:
                prefs.append("stockfish-ubuntu-x86-64-sse41-popcnt.tar")
            prefs.append("stockfish-ubuntu-x86-64.tar")
    else:
        raise RuntimeError(f"Автоскачивание не поддерживает {system}; поставьте Stockfish вручную")
    for p in prefs:
        if p in names:
            return names[p]
    raise RuntimeError(f"В релизе нет подходящей сборки для {system}/{machine}: "
                       + ", ".join(n for n in names if os_key in n))


def download_stockfish() -> Path:
    """Скачать свежий релиз Stockfish в ./bin и вернуть путь к бинарнику."""
    rel = requests.get(RELEASES, timeout=30, headers={"Accept": "application/vnd.github+json"})
    rel.raise_for_status()
    asset = _pick_asset(rel.json()["assets"])
    BIN_DIR.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset["name"]
        with requests.get(asset["browser_download_url"], stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(archive, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        extract = Path(tmp) / "x"
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(extract)
        else:
            with tarfile.open(archive) as t:
                t.extractall(extract, filter="data")
        bins = [p for p in extract.rglob("stockfish*") if p.is_file()
                and p.suffix.lower() not in (".nnue", ".txt", ".md", ".h", ".cpp", ".py", ".sh")]
        bins.sort(key=lambda p: p.stat().st_size, reverse=True)
        target = BIN_DIR / "stockfish"
        for cand in bins:
            shutil.copy2(cand, target)
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            if _runs(str(target)):
                return target
        if target.exists():
            target.unlink()
    raise RuntimeError(f"В {asset['name']} не нашлось бинарника, который запускается на этой машине "
                       f"(кандидаты: {', '.join(p.name for p in bins) or 'нет'})")
