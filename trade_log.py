"""Local persistence: trades CSV, pending-order journal, and single-run lock."""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

CSV_FIELDS = [
    "timestamp_utc",
    "mode",
    "instrument",
    "side",
    "order_type",
    "requested_usd",
    "status",
    "order_id",
    "client_oid",
    "avg_price",
    "quantity",
    "executed_value_usd",
    "fee",
    "fee_currency",
    "reason",
]

# Statuses whose executed value is final; anything else counts the full request.
_FINAL_STATUSES = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}


def append_trade(path: Path, row: dict[str, Any]) -> None:
    unknown = set(row) - set(CSV_FIELDS)
    if unknown:
        raise ValueError(f"unknown CSV fields: {sorted(unknown)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in CSV_FIELDS})
        handle.flush()
        os.fsync(handle.fileno())


def _decimal(text: str | None) -> Decimal:
    try:
        return Decimal(text or "0")
    except InvalidOperation:
        return Decimal("0")


def live_spend_on(path: Path, day: date) -> Decimal:
    """USD committed by LIVE orders logged on local calendar `day`.

    Final orders count their executed value; unverified orders count the full
    requested amount so an unknown outcome can never free up budget.
    """
    if not path.exists():
        return Decimal("0")
    total = Decimal("0")
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("mode") != "LIVE":
                continue
            try:
                logged = datetime.fromisoformat(row["timestamp_utc"]).astimezone()
            except (KeyError, ValueError):
                continue
            if logged.date() != day:
                continue
            if row.get("status") in _FINAL_STATUSES:
                total += _decimal(row.get("executed_value_usd"))
            else:
                total += _decimal(row.get("requested_usd"))
    return total


class PendingOrderJournal:
    """Records an order *before* submission so a crash can be reconciled next run."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class AlreadyRunningError(Exception):
    pass


class RunLock:
    """Exclusive non-blocking file lock so two runs can never overlap."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunningError(f"another run holds {self.path}") from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            if sys.platform == "win32":
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
