"""Настройки из окружения / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


@dataclass
class Config:
    username: str
    tg_token: str
    tg_chat_id: str
    llm_backend: str
    anthropic_model: str
    anthropic_effort: str
    stockfish_path: str
    depth: int
    threads: int
    hash_mb: int
    blunder_cp: int
    mistake_cp: int
    inaccuracy_cp: int
    decided_cp: int
    exclude_time_classes: set[str] = field(default_factory=set)
    profile_window_days: int = 30
    data_dir: Path = ROOT / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "trainer.sqlite3"

    @property
    def profile_md(self) -> Path:
        return self.data_dir / "profile.md"


def load_config() -> Config:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")
    data_dir = Path(_str("DATA_DIR", str(ROOT / "data")))
    if not data_dir.is_absolute():
        data_dir = (ROOT / data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    excl = {s.strip().lower() for s in _str("EXCLUDE_TIME_CLASSES", "bullet").split(",") if s.strip()}
    return Config(
        username=_str("CHESSCOM_USERNAME").lower(),
        tg_token=_str("TELEGRAM_BOT_TOKEN"),
        tg_chat_id=_str("TELEGRAM_CHAT_ID"),
        llm_backend=_str("LLM_BACKEND", "auto").lower(),
        anthropic_model=_str("ANTHROPIC_MODEL", "claude-opus-5-5"),
        anthropic_effort=_str("ANTHROPIC_EFFORT", "medium"),
        stockfish_path=_str("STOCKFISH_PATH"),
        depth=_int("ENGINE_DEPTH", 16),
        threads=_int("ENGINE_THREADS", 2),
        hash_mb=_int("ENGINE_HASH_MB", 256),
        blunder_cp=_int("BLUNDER_CP", 200),
        mistake_cp=_int("MISTAKE_CP", 100),
        inaccuracy_cp=_int("INACCURACY_CP", 50),
        decided_cp=_int("DECIDED_CP", 500),
        exclude_time_classes=excl,
        profile_window_days=_int("PROFILE_WINDOW_DAYS", 30),
        data_dir=data_dir,
    )
