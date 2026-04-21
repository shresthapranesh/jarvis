"""File management tools: read, write, and list files on disk."""

import asyncio
import pathlib


async def write_file(filepath: str, content: str) -> str:
    """Write text content to a file, creating parent directories as needed."""
    def _sync() -> str:
        path = pathlib.Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Report saved to {path}"
    return await asyncio.to_thread(_sync)


async def read_file(filepath: str) -> str:
    """Read text content from a file."""
    def _sync() -> str:
        path = pathlib.Path(filepath)
        if not path.exists():
            return f"File not found: {filepath}"
        return path.read_text(encoding="utf-8")
    return await asyncio.to_thread(_sync)


async def list_files(directory: str) -> str:
    """List files in a directory."""
    def _sync() -> str:
        path = pathlib.Path(directory)
        if not path.exists():
            return f"Directory not found: {directory}"
        files = sorted(p for p in path.iterdir() if p.is_file())
        if not files:
            return f"No files found in {directory}"
        return "\n".join(str(f) for f in files)
    return await asyncio.to_thread(_sync)
