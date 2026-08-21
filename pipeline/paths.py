"""One place that knows where things live, so systemd's ReadWritePaths
allowlist and the code cannot drift apart."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VPS_ROOT = Path("/opt/screener")


def _default_root() -> Path:
    r"""Where data lives when SCREENER_DATA is not set.

    `/opt/screener` is the deployed layout and matches the systemd
    ReadWritePaths allowlist. It is meaningless on a developer machine -- on
    Windows it resolves to `\opt\screener` on the current drive.

    The check is `os.name == "posix"` AND the directory existing, not just the
    latter. An earlier version tested existence alone, which failed in a way
    worth remembering: a previous run on Windows had already *created*
    `C:\opt\screener`, so the existence test then found it and kept using it.
    The heuristic was defeated by the very mess it was meant to clean up.

    Set SCREENER_DATA explicitly to override either.
    """
    if os.name == "posix" and VPS_ROOT.is_dir():
        return VPS_ROOT
    return REPO_ROOT


_ROOT = _default_root()

DATA_DIR = Path(os.environ.get("SCREENER_DATA", _ROOT / "data"))
JOURNAL_DIR = Path(os.environ.get("SCREENER_JOURNAL", _ROOT / "journal"))
LOG_DIR = Path(os.environ.get("SCREENER_LOGS", _ROOT / "logs"))

CANDIDATES = DATA_DIR / "candidates.json"
BARS_DB = DATA_DIR / "bars.sqlite"
HEARTBEAT = DATA_DIR / "heartbeat.json"


def ensure_dirs() -> None:
    for d in (DATA_DIR, JOURNAL_DIR, LOG_DIR, DATA_DIR / "universe",
              DATA_DIR / "tokenwise"):
        d.mkdir(parents=True, exist_ok=True)


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env reader. systemd already injects EnvironmentFile, so this is
    only for running stages by hand from a shell."""
    env = path or Path(__file__).resolve().parent.parent / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
