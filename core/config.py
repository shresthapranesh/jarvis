"""App-level configuration — loaded from environment variables once per process.

Priority: CLI args (configure()) > env vars > .env file > hardcoded defaults
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_overrides: dict[str, Any] = {}


def configure(**kwargs: Any) -> None:
    """Pre-set config overrides from CLI args before the first get_config() call."""
    global _overrides
    _overrides = {k: v for k, v in kwargs.items() if v is not None}
    get_config.cache_clear()


@dataclass(frozen=True)
class AppConfig:
    work_dir: Path
    database_url: str
    checkpoints_db: str
    memory_file: str
    conversation_history_dir: str
    artifacts_dir: Path
    piper_voice: str
    whisper_model: str

    @classmethod
    def from_env(cls, overrides: dict[str, Any]) -> AppConfig:
        work_dir = Path(
            overrides.get("work_dir") or os.environ.get("WORK_DIR", str(Path.home() / ".jarvis"))
        ).resolve()
        artifacts_dir = Path(
            overrides.get("artifacts_dir")
            or os.environ.get("ARTIFACTS_DIR", str(work_dir / "artifacts"))
        ).resolve()
        return cls(
            work_dir=work_dir,
            database_url=overrides.get("database_url")
                or os.environ.get("DATABASE_URL", f"sqlite+aiosqlite:///{work_dir / 'database.db'}"),
            checkpoints_db=overrides.get("checkpoints_db")
                or os.environ.get("CHECKPOINTS_DB", str(work_dir / "checkpoints.db")),
            # Kept as relative strings — LocalShellBackend resolves these relative to root_dir="."
            memory_file=overrides.get("memory_file")
                or os.environ.get("MEMORY_FILE", "memory/AGENTS.md"),
            conversation_history_dir=overrides.get("conversation_history_dir")
                or os.environ.get("CONVERSATION_HISTORY_DIR", "conversation_history"),
            artifacts_dir=artifacts_dir,
            piper_voice=overrides.get("piper_voice")
                or os.environ.get("PIPER_VOICE", "voices/en_US-hfc_female-medium.onnx"),
            whisper_model=overrides.get("whisper_model")
                or os.environ.get("WHISPER_MODEL", "base"),
        )


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Process-wide AppConfig singleton. Loads .env, then env vars; CLI overrides win."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    return AppConfig.from_env(_overrides)
