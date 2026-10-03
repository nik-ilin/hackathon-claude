"""Structured logging with colors and context — one glance tells you what happened."""
import logging
import sys
from datetime import datetime

# ANSI colors
_COLORS = {
    "DEAL":    "\033[92m",  # green
    "DUEL":    "\033[96m",  # cyan
    "TRADE":   "\033[93m",  # yellow
    "BROKER":  "\033[95m",  # magenta
    "VENUE":   "\033[94m",  # blue
    "WARN":    "\033[91m",  # red
    "RESET":   "\033[0m",
}


class BazaarFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now().strftime("%H:%M:%S")
        tag = getattr(record, "tag", "AGENT")
        color = _COLORS.get(tag, "")
        reset = _COLORS["RESET"] if color else ""
        return f"{ts} {color}[{tag:>6}]{reset} {record.getMessage()}"


def setup_logger(name: str = "bazaar", level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(BazaarFormatter())
        logger.addHandler(h)
    return logger


log = setup_logger()


def deal(msg: str, **kw):   log.info(msg, extra={"tag": "DEAL", **kw})
def duel(msg: str, **kw):   log.info(msg, extra={"tag": "DUEL", **kw})
def trade(msg: str, **kw):  log.info(msg, extra={"tag": "TRADE", **kw})
def broker(msg: str, **kw): log.info(msg, extra={"tag": "BROKER", **kw})
def venue(msg: str, **kw):  log.info(msg, extra={"tag": "VENUE", **kw})
def warn(msg: str, **kw):   log.warning(msg, extra={"tag": "WARN", **kw})
def info(msg: str, **kw):   log.info(msg, extra={"tag": "AGENT", **kw})
