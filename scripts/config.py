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
ROOT_DIR = PROJECT_DIR.parent

PRODUCTION_BASE_URL = "https://api.crypto.com/exchange/v1"

# All amounts and prices are in USD; the bot trades <SYMBOL>_USD only.
QUOTE_CURRENCY = "USD"
SIDES = ("BUY", "SELL")

# Defaults reproduce the original USDU buyer. Override any of them in .env.
DEFAULTS = {
    "SYMBOL": "USDU",
    "SIDE": "BUY",
    "MIN_AMOUNT_USD": "2.00",
    "MAX_AMOUNT_USD": "20",
    "FULL_PRICE": "0.995",
    "NO_TRADE_PRICE": "1.000",
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class Strategy:
    """What to trade and how much.

    Each run trades a random USD amount between $0 and a price-scaled maximum
    (rounded to cents): the full max_amount_usd when the price is at or better
    than full_price, shrinking linearly to $0 at no_trade_price. "Better" is
    lower for BUY (the best ask) and higher for SELL (the best bid). Amounts
    below min_amount_usd are rerolled; if the maximum itself is below it,
    nothing is traded. Keep min_amount_usd above the exchange's $1 order minimum.
    The bot never substitutes another instrument if this one is unavailable.
    """

    symbol: str
    side: str
    min_amount_usd: Decimal
    max_amount_usd: Decimal
    full_price: Decimal
    no_trade_price: Decimal

    @property
    def instrument(self) -> str:
        return f"{self.symbol}_{QUOTE_CURRENCY}"

    @property
    def is_buy(self) -> bool:
        return self.side == "BUY"


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
    strategy: Strategy

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


def _load_strategy(env: Mapping[str, str]) -> Strategy:
    def get(name: str) -> str:
        return env.get(name, "").strip() or DEFAULTS[name]

    symbol = get("SYMBOL").upper()
    if not symbol.isalnum():
        raise ConfigError(f"SYMBOL must be a currency code like USDU or BTC, got {symbol!r}")
    side = get("SIDE").upper()
    if side not in SIDES:
        raise ConfigError(f"SIDE must be BUY or SELL, got {side!r}")

    min_amount = _parse_positive_decimal("MIN_AMOUNT_USD", env.get("MIN_AMOUNT_USD"), DEFAULTS["MIN_AMOUNT_USD"])
    max_amount = _parse_positive_decimal("MAX_AMOUNT_USD", env.get("MAX_AMOUNT_USD"), DEFAULTS["MAX_AMOUNT_USD"])
    if min_amount > max_amount:
        raise ConfigError(f"MIN_AMOUNT_USD ({min_amount}) must not exceed MAX_AMOUNT_USD ({max_amount})")

    full_price = _parse_positive_decimal("FULL_PRICE", env.get("FULL_PRICE"), DEFAULTS["FULL_PRICE"])
    no_trade_price = _parse_positive_decimal("NO_TRADE_PRICE", env.get("NO_TRADE_PRICE"), DEFAULTS["NO_TRADE_PRICE"])
    if side == "BUY" and not full_price < no_trade_price:
        raise ConfigError(
            f"For SIDE=BUY, FULL_PRICE ({full_price}) must be below NO_TRADE_PRICE ({no_trade_price}): "
            "the bot buys more the cheaper it gets."
        )
    if side == "SELL" and not full_price > no_trade_price:
        raise ConfigError(
            f"For SIDE=SELL, FULL_PRICE ({full_price}) must be above NO_TRADE_PRICE ({no_trade_price}): "
            "the bot sells more the higher it gets."
        )

    return Strategy(
        symbol=symbol,
        side=side,
        min_amount_usd=min_amount,
        max_amount_usd=max_amount,
        full_price=full_price,
        no_trade_price=no_trade_price,
    )


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build validated settings from `env` (defaults to `.env` + process env)."""
    if env is None:
        load_dotenv(ROOT_DIR / ".env")
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
        strategy=_load_strategy(env),
    )
