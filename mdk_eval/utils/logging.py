"""Rich-based logger + global verbosity flag."""
from __future__ import annotations

import logging
import os

from rich.console import Console
from rich.logging import RichHandler

# Single shared stderr console for log output and Live regions.
_console = Console(stderr=True)

# Verbosity is a process-global. Set by the CLI top-level callback. Modules read it
# to decide whether to emit live panels, judge rationales, etc.
#   "quiet"   -> final score only; no preflight; no live panel
#   "normal"  -> preflight + live panel + inline failures + next-steps panel (default)
#   "verbose" -> normal + judge rationales inline + per-scenario timing
_VERBOSITY = os.getenv("MDK_EVAL_VERBOSITY", "normal").lower()


def set_verbosity(level: str) -> None:
    global _VERBOSITY
    if level not in ("quiet", "normal", "verbose"):
        raise ValueError(f"verbosity must be quiet|normal|verbose, got {level}")
    _VERBOSITY = level
    logger = logging.getLogger("mdk_eval")
    logger.setLevel({"quiet": logging.WARNING, "normal": logging.INFO, "verbose": logging.DEBUG}[level])


def verbosity() -> str:
    return _VERBOSITY


def is_quiet() -> bool:
    return _VERBOSITY == "quiet"


def is_verbose() -> bool:
    return _VERBOSITY == "verbose"


def get_logger(name: str = "mdk_eval") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = RichHandler(console=_console, rich_tracebacks=True, show_path=False, markup=True)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def console() -> Console:
    return _console
