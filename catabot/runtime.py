"""Process-wide runtime concerns: filesystem paths, timezone, and logging.

Importing this module has no side effects beyond defining paths. Call
:func:`setup_logging` once from the entry point to install handlers.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import queue
from datetime import datetime
from zoneinfo import ZoneInfo

# Australian Eastern time — all human-facing timestamps and the scheduled
# restart window are expressed in this zone.
AEST = ZoneInfo("Australia/Sydney")

# Project root is the parent of this package directory.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

CONFIG_FILE = os.path.join(BASE_DIR, "config.toml")
LOG_FILE = os.path.join(LOG_DIR, "monitor.log")
PID_FILE = os.path.join(DATA_DIR, "bot.pid")

STATE_FILE = os.path.join(DATA_DIR, "stock_state.json")
BOT_STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")
PRODUCTS_CACHE_FILE = os.path.join(DATA_DIR, "products_cache.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# True when started with BOT_ENV=dev — used to tag the login line.
IS_DEV = os.environ.get("BOT_ENV", "").lower() == "dev"

# True when run under the watchdog/systemd supervisor. A supervised process
# exits on restart and lets the supervisor respawn it; an unsupervised one
# re-spawns itself. See catabot.app._respawn_and_exit.
SUPERVISED = os.environ.get("CATABOT_SUPERVISED") == "1"

_log_listener: logging.handlers.QueueListener | None = None


def setup_logging() -> logging.Logger:
    """Install a non-blocking, rotating logging configuration.

    Log records are pushed onto an in-memory queue and flushed by a background
    listener thread, so disk I/O never blocks the event loop. Timestamps render
    in AEST. Safe to call once at startup; clears any pre-existing handlers so
    the configuration is not duplicated.
    """
    global _log_listener

    logging.Formatter.converter = lambda *args: datetime.now(AEST).timetuple()
    fmt = logging.Formatter("%(asctime)s AEST [%(levelname)s] %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    stream_handler = logging.StreamHandler()
    file_handler.setFormatter(fmt)
    stream_handler.setFormatter(fmt)

    log_queue: queue.SimpleQueue = queue.SimpleQueue()
    queue_handler = logging.handlers.QueueHandler(log_queue)

    root = logging.root
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(queue_handler)

    _log_listener = logging.handlers.QueueListener(
        log_queue, file_handler, stream_handler, respect_handler_level=True
    )
    _log_listener.start()

    return logging.getLogger("catabot")
