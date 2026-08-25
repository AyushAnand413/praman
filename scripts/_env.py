"""Load a local `.env` into the process environment.

Deliberately a *script* helper, not application code. `settings.py` reads
secrets from the environment and only from the environment; nothing under
`api/`, `kernel/`, `store/`, or `vyapaari/` may call this. Keeping the loader
here preserves that invariant: in deployment the environment is populated by
the platform, and this file is simply never imported.

Written by hand rather than pulling in python-dotenv, for the reason stated in
requirements.txt — one fewer dependency on the path that handles credentials.

Values are stripped. A trailing space on a pasted API key is invisible in an
editor and produces an authentication failure that looks like a wrong key, so
whitespace is removed rather than faithfully preserved.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


def parse_env_file(path: Path | str = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Parse `KEY=value` lines. Blank lines and `#` comments are skipped.

    Does not touch the environment — callers that only need to *read* the real
    credentials (a test that must compare against the true webhook secret, for
    instance) use this and leave the process environment alone.
    """
    path = Path(path)
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Allow quoted values so a secret containing spaces stays intact.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env_file(
    path: Path | str = DEFAULT_ENV_PATH, *, override: bool = False
) -> dict[str, str]:
    """Copy the parsed file into `os.environ` and return what was applied.

    An existing environment variable wins by default: a value exported in the
    shell for one command is the more specific instruction, and silently
    overwriting it makes a deliberate override look like it was ignored.
    """
    values = parse_env_file(path)
    applied: dict[str, str] = {}
    for key, value in values.items():
        if override or not os.environ.get(key):
            os.environ[key] = value
            applied[key] = value
    return applied
