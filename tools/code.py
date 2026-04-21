"""Python code execution tool."""

import asyncio
import os
import sys
import tempfile


async def run_python(code: str) -> str:
    """Execute Python code and return the output. Use for calculations, data analysis, plotting, or any computational task. Has access to standard library and installed packages. Timeout: 30s."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        fname = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, fname,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "Error: execution timed out (30s limit)"
        except asyncio.CancelledError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.communicate(), timeout=5)
            except asyncio.TimeoutError:
                pass
            raise
        output = stdout.decode().strip()
        if stderr.decode().strip():
            output = (output + "\n[stderr]\n" + stderr.decode().strip()).strip()
        return output or "(no output)"
    finally:
        os.unlink(fname)
