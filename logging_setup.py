"""Logging configuration: console + rotating file (`bot.log`)."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_FILE = "bot.log"


class _ConsoleFormatter(logging.Formatter):
    """Colours the level name when the stream supports ANSI escapes."""

    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    RESET = "\033[0m"

    def __init__(self, fmt: str, datefmt: str, use_color: bool = True) -> None:
        super().__init__(fmt, datefmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not self.use_color:
            return message
        color = self.COLORS.get(record.levelname)
        return f"{color}{message}{self.RESET}" if color else message


def _known_level(level: str) -> int:
    name = (level or "INFO").strip().upper()
    numeric = logging.getLevelName(name)
    return numeric if isinstance(numeric, int) else logging.INFO


def setup_logging(
    log_file: Optional[str | Path] = DEFAULT_LOG_FILE,
    level: str = "INFO",
    *,
    quiet: bool = False,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Logger:
    """Configure the root logger once and return it."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - defensive
            pass

    numeric_level = _known_level(level)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(numeric_level)
    stream = getattr(console, "stream", None)
    use_color = bool(getattr(stream, "isatty", lambda: False)())
    console.setFormatter(_ConsoleFormatter(LOG_FORMAT, DATE_FORMAT, use_color))
    if not quiet:
        root.addHandler(console)

    if log_file:
        path = Path(log_file).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=max(int(max_bytes), 10_000),
                backupCount=max(int(backup_count), 1),
                encoding="utf-8",
            )
            file_handler.setLevel(numeric_level)
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
            root.addHandler(file_handler)
        except OSError as exc:  # read-only FS, permission denied, ...
            root.warning("File logging disabled (%s): %s", path, exc)

    # Third-party noise control.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)