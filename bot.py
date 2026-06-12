"""Entry point. The watchdog and systemd service launch ``python bot.py``.

All logic lives in the :mod:`catabot` package; this shim only invokes it so the
historical launch path keeps working.
"""

from catabot.app import run

if __name__ == "__main__":
    run()
