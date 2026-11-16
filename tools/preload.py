"""What every kernel preloads (`edge/src/kernels/mod.rs: bootstrap`), as
`from tools.preload import *`; the same line restores it after `%reset -f`.

Only what is cheap: the web tools and the SDK, and modules they import
anyway. pandas, numpy and yfinance are installed but not preloaded — this
runs on low-RAM hardware, and a kernel that never uses them shouldn't hold
them. (Bound lazily they don't stay lazy: the first traceback's
`inspect.getmodule` reads `__file__` off every module in `sys.modules`.)
A piece that fails to import is left out; the kernel starts regardless.
"""

from __future__ import annotations

import datetime
import subprocess

__all__: list[str] = ["datetime", "subprocess"]

try:
    from tools.research import read, read_many, search

    __all__ += ["search", "read", "read_many"]
except Exception:
    pass
try:
    import tools.sdk as jarvis

    __all__ += ["jarvis"]
except Exception:
    pass
try:
    import httpx

    __all__ += ["httpx"]
except Exception:
    pass
