# PyInstaller spec for Jarvis — single-folder bundle (frontend + core agent).
# Run from repo root: `uv run pyinstaller jarvis.spec`
# Output: dist/jarvis/jarvis (or jarvis.exe)

from PyInstaller.utils.hooks import collect_submodules


def _safe_submodules(name: str) -> list[str]:
    try:
        return collect_submodules(name)
    except Exception:
        return []


hidden = (
    _safe_submodules("langchain")
    + _safe_submodules("langchain_core")
    + _safe_submodules("langchain_anthropic")
    + _safe_submodules("langchain_aws")
    + _safe_submodules("langchain_google_genai")
    + _safe_submodules("langchain_ollama")
    + _safe_submodules("langchain_openai")
    + _safe_submodules("langgraph")
    + _safe_submodules("langgraph_checkpoint_sqlite")
    + _safe_submodules("langgraph.store.sqlite")
    + _safe_submodules("apscheduler")
    + _safe_submodules("uvicorn")
    + [
        "aiosqlite",
        "sse_starlette",
        "httptools",
        "websockets",
        "websockets.legacy",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ]
)

datas = [("static/dist", "static/dist")]

# Heavy / native / optional deps. The Python code has been guarded to degrade
# gracefully when these are absent (see tools/finance.py, tools/browser_agent.py,
# server/routes_media.py).
excludes = [
    # Whisper / TTS
    "mlx", "mlx_whisper", "mlx_lm",
    "faster_whisper", "ctranslate2",
    "piper", "piper_phonemize",
    # Browser automation
    "browser_use", "playwright",
    # Optional bots
    "telegram", "discord",
    # Finance stack (huge transitive: pandas, numpy chunks)
    "yfinance", "pandas", "matplotlib",
    # GUI toolkits we don't use
    "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
    # Test frameworks
    "test", "tests", "pytest",
]

a = Analysis(
    ["bin/jarvis_entry.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    excludes=excludes,
    hookspath=[],
    runtime_hooks=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jarvis",
    console=True,
    debug=False,
    strip=False,
    upx=False,
    bootloader_ignore_signals=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="jarvis",
    strip=False,
    upx=False,
)
