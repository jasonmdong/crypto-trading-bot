"""Buy or sell a random, price-scaled USD amount of one coin on the Crypto.com Exchange.

What to trade comes from .env (SYMBOL, SIDE, MIN/MAX_AMOUNT_USD, FULL_PRICE,
NO_TRADE_PRICE); see config.Strategy for how the amount is chosen.

Usage:  python main.py

DRY_RUN is on by default. With DRY_RUN=false it trades without asking.
See README.md.
"""

from __future__ import annotations

import secrets
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from random import Random
from typing import Any

from config import QUOTE_CURRENCY, ConfigError, Settings, Strategy, load_settings
from crypto_client import (
    ApiError,
    CryptoComClient,
    CryptoComError,
    DuplicateOrderError,
    HttpError,
    InvalidNonceError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    TransientServerError,
)
from trade_log import AlreadyRunningError, PendingOrderJournal, RunLock, append_trade, live_spend_on

CENT = Decimal("0.01")
TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_NOT_PLACED = 3  # validation stopped the order (below minimum, limit reached, ...)


class AmountError(Exception):
    """The generated amount cannot be submitted."""


class MarketUnavailableError(Exception):
    """The configured instrument is missing or not tradable. The bot never substitutes another asset."""


class OrderStateUnknownError(Exception):
    """The order may or may not exist; it is left in the pending journal."""


@dataclass(frozen=True)
class MarketRules:
    instrument: str
    qty_tick_size: Decimal
    min_notional: Decimal | None
    max_notional: Decimal | None
    price: Decimal  # best ask for BUY, best bid for SELL


# --------------------------------------------------------------------- amount


def max_amount_for_price(price: Decimal, strategy: Strategy) -> Decimal:
    """Full max_amount_usd at or past full_price, falling linearly to $0 at no_trade_price."""
    scale = (strategy.no_trade_price - price) / (strategy.no_trade_price - strategy.full_price)
    return strategy.max_amount_usd * min(max(scale, Decimal("0")), Decimal("1"))


def generate_amount(price: Decimal, strategy: Strategy, rng: Random | None = None) -> Decimal:
    """Random amount from $0 to max_amount_for_price, in cents; reroll while below min_amount_usd.

    Callers must first check max_amount_for_price(...) >= min_amount_usd, or this never ends.
    """
    rng = rng or secrets.SystemRandom()
    max_amount = max_amount_for_price(price, strategy)
    while True:
        amount = (max_amount * Decimal(str(rng.random()))).quantize(CENT, rounding=ROUND_HALF_UP)
        if amount >= strategy.min_amount_usd:
            return amount


def estimate_quantity(amount: Decimal, rules: MarketRules) -> Decimal:
    raw = amount / rules.price
    return (raw // rules.qty_tick_size) * rules.qty_tick_size


def validate_amount(
    amount: Decimal,
    rules: MarketRules,
    strategy: Strategy,
    remaining_daily_usd: Decimal,
    available: Decimal | None = None,
) -> Decimal:
    """Return the (estimated) coin quantity, or raise AmountError explaining why not.

    `available` is the free USD balance for a BUY, or the free coin balance for a SELL.
    """
    symbol = strategy.symbol
    if not amount.is_finite() or amount <= 0:
        raise AmountError(f"amount must be positive, got ${amount}")
    if amount != amount.quantize(CENT):
        raise AmountError(f"amount must have at most 2 decimal places, got {amount}")
    if rules.min_notional is not None and amount < rules.min_notional:
        raise AmountError(f"${amount} is below the exchange minimum order of ${rules.min_notional} for {symbol}")
    if rules.max_notional is not None and amount > rules.max_notional:
        raise AmountError(f"${amount} exceeds the exchange maximum order of ${rules.max_notional} for {symbol}")
    if amount > remaining_daily_usd:
        raise AmountError(
            f"${amount} exceeds the remaining daily limit of ${max(remaining_daily_usd, Decimal('0')):.2f}"
        )
    if strategy.is_buy and available is not None and amount > available:
        raise AmountError(f"${amount} exceeds the available Exchange {QUOTE_CURRENCY} balance of ${available}")
    quantity = estimate_quantity(amount, rules)
    if quantity < rules.qty_tick_size:
        raise AmountError(
            f"${amount} is ~{amount / rules.price:.4f} {symbol}, below the {rules.qty_tick_size} quantity tick"
        )
    if not strategy.is_buy and available is not None and quantity > available:
        raise AmountError(f"selling {quantity} {symbol} exceeds the available Exchange {symbol} balance of {available}")
    return quantity


# --------------------------------------------------------------------- market


def _decimal_field(data: dict[str, Any], key: str) -> Decimal | None:
    value = data.get(key)
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def load_market_rules(client: CryptoComClient, strategy: Strategy) -> MarketRules:
    name = strategy.instrument
    instrument = client.get_instrument(name)
    if instrument is None:
        raise MarketUnavailableError(f"{name} is not listed on the Crypto.com Exchange. No order placed.")
    if instrument.get("base_ccy") != strategy.symbol or instrument.get("quote_ccy") != QUOTE_CURRENCY:
        raise MarketUnavailableError(f"{name} reports unexpected currencies: {instrument}")
    if instrument.get("tradable") is not True:
        raise MarketUnavailableError(f"{name} is listed but not tradable right now. No order placed.")
    qty_tick = _decimal_field(instrument, "qty_tick_size")
    if qty_tick is None or qty_tick <= 0:
        raise MarketUnavailableError(f"{name} has no usable qty_tick_size: {instrument.get('qty_tick_size')!r}")

    # A market BUY fills against the asks, a market SELL against the bids.
    ticker = client.get_ticker(name) or {}
    price = _decimal_field(ticker, "k" if strategy.is_buy else "b")
    if price is None or price <= 0:
        book_side = "ask" if strategy.is_buy else "bid"
        raise MarketUnavailableError(f"{name} has no {book_side} price (empty order book). No order placed.")

    limits = client.get_risk_limits(strategy.symbol) or {}
    return MarketRules(
        instrument=name,
        qty_tick_size=qty_tick,
        min_notional=_decimal_field(limits, "min_order_notional_usd"),
        max_notional=_decimal_field(limits, "max_order_notional_usd"),
        price=price,
    )


def build_order_params(strategy: Strategy, amount: Decimal, quantity: Decimal, client_oid: str) -> dict[str, str]:
    """Market BUY spending exactly `amount` USD (`notional`), or market SELL of `quantity` coins.

    The exchange only accepts `notional` on market buys, so sells are sized in coins.
    """
    params = {
        "instrument_name": strategy.instrument,
        "side": strategy.side,
        "type": "MARKET",
        "client_oid": client_oid,
        "spot_margin": "SPOT",  # never borrow
    }
    if strategy.is_buy:
        params["notional"] = str(amount.quantize(CENT))
    else:
        params["quantity"] = format(quantity, "f")
    return params


def spent_today(settings: Settings, client: CryptoComClient, now: datetime) -> Decimal:
    """Max of locally logged and exchange-reported USD traded today on this instrument and side."""
    strategy = settings.strategy
    local_now = now.astimezone()
    local = live_spend_on(settings.trades_csv, local_now.date(), strategy.instrument, strategy.side)
    if not settings.has_credentials:
        return local
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    orders = client.get_order_history(
        strategy.instrument, int(midnight.timestamp() * 1000), int(local_now.timestamp() * 1000)
    )
    exchange = sum(
        (
            _decimal_field(order, "cumulative_value") or Decimal("0")
            for order in orders
            if order.get("side") == strategy.side
        ),
        Decimal("0"),
    )
    return max(local, exchange)


# ---------------------------------------------------------------------- orders


def _find_order(client: CryptoComClient, client_oid: str, attempts: int, sleep: Callable[[float], None]) -> dict | None:
    """Look an order up by client_oid. None only if repeatedly NOT_FOUND."""
    for attempt in range(attempts):
        try:
            return client.get_order_detail(client_oid=client_oid)
        except NotFoundError:
            if attempt < attempts - 1:
                sleep(1.0)
        except CryptoComError as exc:
            raise OrderStateUnknownError(f"could not look up client_oid {client_oid}: {exc}") from exc
    return None


def submit_order(
    client: CryptoComClient,
    params: dict[str, str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    submit_attempts: int = 3,
    lookup_attempts: int = 3,
) -> str:
    """Submit once; on an ambiguous failure, look up by client_oid before resending.

    Resends always reuse the same client_oid, so the exchange can reject a true
    duplicate (DUPLICATE_CLORDID) even if our lookup raced the matching engine.
    Definitive rejections (ApiError subclasses not handled below) propagate.
    """
    client_oid = params["client_oid"]
    last_error: Exception | None = None
    for attempt in range(1, submit_attempts + 1):
        try:
            return str(client.create_order(params)["order_id"])
        except DuplicateOrderError as exc:
            existing = _find_order(client, client_oid, lookup_attempts, sleep)
            if existing is None:
                raise OrderStateUnknownError(f"exchange reports duplicate but {client_oid} not found") from exc
            return str(existing["order_id"])
        except (InvalidNonceError, RateLimitError) as exc:
            # Rejected before reaching the matching engine: resending is safe.
            last_error = exc
            sleep(1.0 * attempt)
        except (NetworkError, TransientServerError, HttpError) as exc:
            # Outcome unknown: the order may exist. Never resend before checking.
            last_error = exc
            sleep(1.0)
            existing = _find_order(client, client_oid, lookup_attempts, sleep)
            if existing is not None:
                return str(existing["order_id"])
    raise OrderStateUnknownError(f"order {client_oid} not confirmed after {submit_attempts} attempts: {last_error}")


def wait_for_final_status(
    client: CryptoComClient,
    order_id: str,
    timeout: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """Poll get-order-detail until a terminal status or timeout. Returns last detail seen."""
    deadline = clock() + timeout
    last: dict[str, Any] | None = None
    while True:
        try:
            last = client.get_order_detail(order_id=order_id)
            if last.get("status") in TERMINAL_STATUSES:
                return last
        except NotFoundError:
            pass  # create-order is asynchronous; detail may lag briefly
        except CryptoComError as exc:
            print(f"  status check failed ({exc}); retrying")
        if clock() >= deadline:
            return last
        sleep(0.5)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_from_detail(strategy: Strategy, amount: Decimal, client_oid: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp_utc": _utc_now(),
        "mode": "LIVE",
        # From the order itself, so a leftover order is logged correctly even if .env changed since.
        "instrument": detail.get("instrument_name") or strategy.instrument,
        "side": detail.get("side") or strategy.side,
        "order_type": "MARKET",
        "requested_usd": str(amount),
        "status": detail.get("status", ""),
        "order_id": str(detail.get("order_id", "")),
        "client_oid": client_oid,
        "avg_price": detail.get("avg_price", ""),
        "quantity": detail.get("cumulative_quantity", ""),
        "executed_value_usd": detail.get("cumulative_value", ""),
        "fee": detail.get("cumulative_fee", ""),
        "fee_currency": detail.get("fee_instrument_name", ""),
        "reason": detail.get("reject_reason") or detail.get("reason") or "",
    }


def print_result(record: dict[str, Any]) -> None:
    bought = record["side"] == "BUY"
    qty = _decimal_field(record, "quantity") or Decimal("0")
    if qty <= 0:
        reason = f" (reason {record['reason']})" if record["reason"] else ""
        print(f"Order {record['status']}: nothing {'bought' if bought else 'sold'}{reason}.")
        return
    value = (_decimal_field(record, "executed_value_usd") or Decimal("0")).quantize(CENT)
    symbol = record["instrument"].split("_")[0]
    fee = f" (fee {record['fee']} {record['fee_currency']})" if record["fee"] else ""
    print(f"{'Bought' if bought else 'Sold'} {qty} {symbol} for ${value} at ${record['avg_price']}{fee}")


def filled_quantity(record: dict[str, Any]) -> Decimal:
    return _decimal_field(record, "quantity") or Decimal("0")


def reconcile_pending(client: CryptoComClient, settings: Settings, journal: PendingOrderJournal,
                      sleep: Callable[[float], None]) -> bool:
    """Resolve an order left over from an interrupted run. True if cleared."""
    pending = journal.load()
    if pending is None:
        return True
    client_oid = pending["client_oid"]
    print(f"Checking unfinished order from last run (${pending['amount']})...")
    if not settings.has_credentials:
        print("  Cannot check it without API credentials.")
        return False
    try:
        detail = _find_order(client, client_oid, attempts=2, sleep=sleep)
    except OrderStateUnknownError as exc:
        print(f"  {exc}")
        return False
    if detail is None:
        print("  The exchange has no order with that client_oid: it was never placed. Clearing journal.")
        journal.clear()
        return True
    if detail.get("status") not in TERMINAL_STATUSES:
        print(f"  Still {detail.get('status')} on the exchange. Re-run later.")
        return False
    record = record_from_detail(settings.strategy, Decimal(pending["amount"]), client_oid, detail)
    append_trade(settings.trades_csv, record)
    journal.clear()
    print_result(record)
    return True


# ------------------------------------------------------------------------ run


def run(
    settings: Settings,
    client: CryptoComClient,
    *,
    rng: Random | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    lock = RunLock(settings.state_dir / ".bot.lock")
    try:
        lock.acquire()
    except AlreadyRunningError:
        print("Already running in another window.")
        return EXIT_FAILED
    try:
        journal = PendingOrderJournal(settings.state_dir / ".pending_order.json")
        if not reconcile_pending(client, settings, journal, sleep) and not settings.dry_run:
            print("Refusing to place a new order until the previous one is resolved.")
            return EXIT_FAILED

        strategy = settings.strategy
        verb = "buy" if strategy.is_buy else "sell"
        rules = load_market_rules(client, strategy)

        max_amount = max_amount_for_price(rules.price, strategy)
        if max_amount < strategy.min_amount_usd:
            print(
                f"No trade: {strategy.symbol} at ${rules.price} is too close to ${strategy.no_trade_price} "
                f"(max {verb} ${max_amount:.2f} < ${strategy.min_amount_usd})."
            )
            return EXIT_NOT_PLACED
        amount = generate_amount(rules.price, strategy, rng)

        available: Decimal | None = None
        if settings.has_credentials:
            available = client.get_available_balance(QUOTE_CURRENCY if strategy.is_buy else strategy.symbol)
        spent = spent_today(settings, client, now())
        remaining = settings.daily_limit_usd - spent

        try:
            est_qty = validate_amount(amount, rules, strategy, remaining, available)
        except AmountError as exc:
            print(f"No trade: {exc}")
            return EXIT_NOT_PLACED

        client_oid = str(uuid.uuid4())
        params = build_order_params(strategy, amount, est_qty, client_oid)

        if settings.dry_run:
            append_trade(settings.trades_csv, {
                "timestamp_utc": _utc_now(), "mode": "DRY_RUN", "instrument": strategy.instrument,
                "side": strategy.side, "order_type": "MARKET", "requested_usd": str(amount),
                "status": "DRY_RUN", "client_oid": client_oid, "avg_price": str(rules.price),
                "quantity": str(est_qty), "reason": "preview only; no order submitted",
            })
            print(f"Dry run: would {verb} ${amount} (~{est_qty} {strategy.symbol} at ${rules.price}). No order sent.")
            return EXIT_OK

        journal.save({"client_oid": client_oid, "amount": str(amount), "created_at": _utc_now()})
        try:
            order_id = submit_order(client, params, sleep=sleep)
        except OrderStateUnknownError as exc:
            print(f"Order status unknown: {exc}. The next run will check it before trading again.")
            return EXIT_FAILED
        except ApiError as exc:
            journal.clear()
            append_trade(settings.trades_csv, {
                "timestamp_utc": _utc_now(), "mode": "LIVE", "instrument": strategy.instrument,
                "side": strategy.side, "order_type": "MARKET", "requested_usd": str(amount), "status": "REJECTED",
                "client_oid": client_oid, "executed_value_usd": "0", "reason": f"{exc.code} {exc.name}",
            })
            print(f"Order rejected: {exc.code} {exc.name}")
            return EXIT_FAILED

        journal.save({"client_oid": client_oid, "amount": str(amount), "order_id": order_id, "created_at": _utc_now()})
        detail = wait_for_final_status(client, order_id, settings.order_status_timeout, sleep=sleep)
        if detail is None or detail.get("status") not in TERMINAL_STATUSES:
            status = detail.get("status") if detail else "unknown"
            print(f"Order {order_id} still {status}; the next run will check it.")
            return EXIT_FAILED

        record = record_from_detail(strategy, amount, client_oid, detail)
        append_trade(settings.trades_csv, record)
        journal.clear()
        print_result(record)
        if filled_quantity(record) <= 0:
            return EXIT_FAILED
        return EXIT_OK
    finally:
        lock.release()


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return EXIT_CONFIG
    client = CryptoComClient(
        settings.api_key,
        settings.api_secret,
        base_url=settings.base_url,
        timeout=settings.request_timeout,
    )
    try:
        return run(settings, client)
    except MarketUnavailableError as exc:
        print(f"NOT PLACED: {exc}")
        return EXIT_NOT_PLACED
    except CryptoComError as exc:
        print(f"ERROR: {exc}")
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
