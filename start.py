#!/usr/bin/env python3
"""
Railway / PaaS entrypoint: run the trading bot + HTTP dashboard together.

Railway (and similar hosts) require a process listening on $PORT for the
public URL to work. The bot alone does not serve HTTP — this starts
``main.py`` in a child process and runs the Flask dashboard in the foreground
bound to ``PORT`` (fallback 5050 for local use).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def main() -> None:
    port = os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT") or "5050"
    os.environ["PORT"] = str(port)
    os.environ.setdefault("DASHBOARD_PORT", str(port))

    bot = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
    )

    def _shutdown(signum: int, _frame: object) -> None:
        try:
            bot.send_signal(signum)
        except OSError:
            pass
        # Give the bot a moment for clean SIGTERM handling, then exit.
        try:
            bot.wait(timeout=25)
        except subprocess.TimeoutExpired:
            bot.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Import after env is set so dashboard picks up PORT
    import dashboard  # noqa: WPS433

    # If bot dies early, exit so Railway restarts the whole service
    def _watch_bot() -> None:
        while True:
            code = bot.poll()
            if code is not None:
                print(f"Bot process exited with code {code}; shutting down", flush=True)
                os._exit(code or 1)
            time.sleep(2)

    import threading

    threading.Thread(target=_watch_bot, name="bot-watch", daemon=True).start()

    print(f"Starting dashboard on 0.0.0.0:{port} (bot pid={bot.pid})", flush=True)
    dashboard.app.run(host="0.0.0.0", port=int(port), debug=False)


if __name__ == "__main__":
    main()
