"""PyInstaller entrypoint — boots the typer CLI from `main:app`.

The frozen binary is invoked like the source CLI: `./jarvis start`,
`./jarvis run "query"`, `./jarvis config list`, etc.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap_paths() -> None:
    if getattr(sys, "frozen", False):
        root = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        os.environ.setdefault("JARVIS_FROZEN", "1")


def main() -> None:
    _bootstrap_paths()
    from main import app  # imported after sys.path setup so frozen bundle resolves
    app()


if __name__ == "__main__":
    main()
