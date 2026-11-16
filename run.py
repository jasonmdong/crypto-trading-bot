"""Run main.py once a day at a random time. Stop with Ctrl-C.

Usage:  python run.py

The first run is at a random time in the rest of today, then once per day at a
random time. Each run is a fresh `python scripts/main.py`, so .env changes apply.
"""

from __future__ import annotations

import random
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

MAIN = Path(__file__).resolve().parent / "scripts" / "main.py"


def next_midnight(moment: datetime) -> datetime:
    return datetime.combine(moment.date() + timedelta(days=1), datetime.min.time())


def random_time_before_midnight(start: datetime, rng: random.Random) -> datetime:
    """Uniformly random moment between `start` and the following midnight."""
    return start + (next_midnight(start) - start) * rng.random()


def wait_until(when: datetime) -> None:
    # Short sleeps against the wall clock, so a PC that slept still runs on time.
    while (remaining := (when - datetime.now()).total_seconds()) > 0:
        time.sleep(min(remaining, 60))


def main() -> int:
    rng = random.SystemRandom()
    start = datetime.now()
    try:
        while True:
            when = random_time_before_midnight(start, rng)
            print(f"Next run: {when:%a %b %d %H:%M}. Press Ctrl-C to stop.", flush=True)
            wait_until(when)
            print(f"[{datetime.now():%Y-%m-%d %H:%M}] ", end="", flush=True)
            subprocess.run([sys.executable, str(MAIN)], cwd=MAIN.parent)
            # Next run is tomorrow; if the PC slept past midnight, use the rest of today.
            start = max(next_midnight(when), datetime.now())
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
