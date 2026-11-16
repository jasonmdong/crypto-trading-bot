"""Configuration loading and validation.

All settings come from environment variables (optionally loaded from `.env`).
The bot is DRY_RUN by default; only the literal values "false"/"0"/"no"/"off"
disable it. Credentials are kept out of `repr()` so they never end up in logs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent

PRODUCTION_BASE_URL = "https://api.crypto.com/exchange/v1"

# The bot only ever trades this pair. There is deliberately no setting to change
# it, so a missing/halted USDU market can never fall back to another asset.
INSTRUMENT_NAME = "USDU_USD"
BASE_CURRENCY = "USDU"
QUOTE_CURRENCY = "USD"

# Purchase amount = random between $0 and max_amount (rounded to cents),
# where max_amount depends on the current best ask: the full MAX_AMOUNT_USD at or
# below FULL_BUY_PRICE, shrinking linearly to $0 at NO_BUY_PRICE.
# Amounts below MIN_AMOUNT_USD are rerolled; if max_amount itself is below it,
# nothing is bought. Keep MIN_AMOUNT_USD above the exchange's $1 order minimum.
MIN_AMOUNT_USD = Decimal("2.00")
MAX_AMOUNT_USD = Decimal("20")
FULL_BUY_PRICE = Decimal("0.995")
NO_BUY_PRICE = Decimal("1.000")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    dry_run: bool
    daily_limit_usd: Decimal
    base_url: str
    trades_csv: Path
    request_timeout: float
    order_status_timeout: float

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def state_dir(self) -> Path:
        """Directory holding the run lock and pending-order journal."""
        return self.trades_csv.parent


def _parse_bool(name: str, raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _parse_positive_decimal(name: str, raw: str | None, default: str) -> Decimal:
    text = (raw or "").strip() or default
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a number, got {text!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ConfigError(f"{name} must be greater than 0, got {text!r}")
    return value


def _parse_positive_float(name: str, raw: str | None, default: float) -> float:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {text!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0, got {text!r}")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build validated settings from `env` (defaults to `.env` + process env)."""
    if env is None:
        load_dotenv(PROJECT_DIR / ".env")
        env = os.environ

    api_key = env.get("CRYPTO_COM_API_KEY", "").strip()
    api_secret = env.get("CRYPTO_COM_API_SECRET", "").strip()
    dry_run = _parse_bool("DRY_RUN", env.get("DRY_RUN"), default=True)

    if bool(api_key) != bool(api_secret):
        raise ConfigError("Set both CRYPTO_COM_API_KEY and CRYPTO_COM_API_SECRET, or neither.")
    if not dry_run and not api_key:
        raise ConfigError("DRY_RUN=false requires CRYPTO_COM_API_KEY and CRYPTO_COM_API_SECRET.")

    base_url = (env.get("CRYPTO_COM_BASE_URL", "").strip() or PRODUCTION_BASE_URL).rstrip("/")
    if not base_url.startswith("https://"):
        raise ConfigError(f"CRYPTO_COM_BASE_URL must use https://, got {base_url!r}")

    trades_csv = Path(env.get("TRADES_CSV", "").strip() or "trades.csv").expanduser()
    if not trades_csv.is_absolute():
        trades_csv = PROJECT_DIR / trades_csv

    return Settings(
        api_key=api_key,
        api_secret=api_secret,
        dry_run=dry_run,
        daily_limit_usd=_parse_positive_decimal("DAILY_LIMIT_USD", env.get("DAILY_LIMIT_USD"), "50"),
        base_url=base_url,
        trades_csv=trades_csv,
        request_timeout=_parse_positive_float(
            "REQUEST_TIMEOUT_SECONDS", env.get("REQUEST_TIMEOUT_SECONDS"), 10.0
        ),
        order_status_timeout=_parse_positive_float(
            "ORDER_STATUS_TIMEOUT_SECONDS", env.get("ORDER_STATUS_TIMEOUT_SECONDS"), 30.0
        ),
    )
