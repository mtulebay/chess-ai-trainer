"""Поиск Stockfish и скачивание готовой сборки с GitHub (если нет прав на установку)."""
from __future__ import annotations

import os
import platform
import shutil
import stat
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
    candidates = [cfg.stockfish_path, shutil.which("stockfish") or "", "/usr/games/stockfish",
                  "/usr/local/bin/stockfish"]
    candidates += [str(p) for p in sorted(BIN_DIR.glob("stockfish*")) if p.is_file()]
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    raise FileNotFoundError(
        "Stockfish не найден. Поставьте его (apt install stockfish), укажите STOCKFISH_PATH "
        "или выполните: python -m chess_trainer setup-engine"
    )


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
    machine = platform.machine().lower()
    names = {a["name"]: a for a in assets}
    if machine in ("aarch64", "arm64"):
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
    for p in prefs:
        if p in names:
            return names[p]
    linux = [a for a in assets if "ubuntu" in a["name"] or "linux" in a["name"]]
    if not linux:
        raise RuntimeError("В релизе нет сборки под Linux: " + ", ".join(names))
    return linux[0]


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
        bins = [p for p in extract.rglob("stockfish*") if p.is_file() and p.suffix not in (".nnue", ".txt", ".md")]
        bins.sort(key=lambda p: p.stat().st_size, reverse=True)
        if not bins:
            raise RuntimeError("В архиве не найден бинарник stockfish")
        target = BIN_DIR / "stockfish"
        shutil.copy2(bins[0], target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target
