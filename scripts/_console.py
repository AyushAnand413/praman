"""Make script output safe on a Windows console.

The demo scripts print box-drawing characters, em dashes, the rupee sign, and
arrows. Windows defaults stdout to cp1252, which cannot encode any of them:
depending on whether stdout is a console or a pipe, Python either raises
UnicodeEncodeError mid-print or substitutes `?`. A demo script that dies while
printing a captured payment ID is a bad demo.

`use_utf8_stdout()` switches the stream to UTF-8 and, as a belt-and-braces
measure, replaces anything the terminal still cannot render instead of raising.
Import it before the first print in any script under scripts/.
"""

from __future__ import annotations

import sys


def use_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # stream was replaced by something without that API
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Detached or already-closed stream: printing is the caller's
            # problem now, and it is not worth failing the script over.
            pass
