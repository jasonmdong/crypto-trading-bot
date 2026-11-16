"""Test doubles shared by the test modules."""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

from config import Settings, Strategy

USDU_BUY = Strategy(symbol="USDU", side="BUY", min_amount_usd=Decimal("2.00"), max_amount_usd=Decimal("20"),
                    full_price=Decimal("0.995"), no_trade_price=Decimal("1.000"))
USDU_SELL = Strategy(symbol="USDU", side="SELL", min_amount_usd=Decimal("2.00"), max_amount_usd=Decimal("20"),
                     full_price=Decimal("1.005"), no_trade_price=Decimal("1.000"))

USDU_INSTRUMENT = {
    "symbol": "USDU_USD",
    "base_ccy": "USDU",
    "quote_ccy": "USD",
    "qty_tick_size": "0.1",
    "price_tick_size": "0.00001",
    "tradable": True,
}


class FakeResponse:
    def __init__(self, status: int, payload: Any = None):
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class FakeSession:
    """Replays queued responses (or raises queued exceptions) and records requests."""

    def __init__(self, *responses: Any):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": copy.deepcopy(json)})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Ticker:
    """Clock that advances 1 ms per call so nonces/ids differ between requests."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        self.now += 0.001
        return self.now


class FixedRandom:
    """Returns the given values in order, then repeats the last one."""

    def __init__(self, *values: float):
        self.values = list(values)

    def random(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class FakeExchange:
    """Stands in for CryptoComClient at the method level for orchestration tests."""

    def __init__(self, *, balance: str = "100", history: list[dict] | None = None, ask: str = "0.99852", bid: str = "0.99850",
                 create_effects: list[Any] | None = None, detail_effects: list[Any] | None = None):
        self.balance = Decimal(balance)
        self.ask = ask
        self.bid = bid
        self.history = history or []
        self.create_effects = list(create_effects or [])
        self.detail_effects = list(detail_effects or [])
        self.created: list[dict[str, str]] = []
        self.detail_queries: list[dict[str, Any]] = []

    def get_instrument(self, symbol):
        return dict(USDU_INSTRUMENT) if symbol == "USDU_USD" else None

    def get_ticker(self, instrument_name):
        return {"i": instrument_name, "k": self.ask, "b": self.bid}

    def get_risk_limits(self, base_currency):
        return {"instrument_name": "USDU", "min_order_notional_usd": "1.0", "max_order_notional_usd": "1000.0"}

    def get_available_balance(self, currency):
        return self.balance

    def get_order_history(self, instrument_name, start_ms, end_ms):
        return self.history

    def create_order(self, params):
        self.created.append(dict(params))
        effect = self.create_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect

    def get_order_detail(self, *, order_id=None, client_oid=None):
        self.detail_queries.append({"order_id": order_id, "client_oid": client_oid})
        effect = self.detail_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


def make_settings(tmp: Path, *, dry_run: bool, credentials: bool = True, daily_limit: str = "50",
                  strategy: Strategy = USDU_BUY) -> Settings:
    return Settings(
        api_key="test-key" if credentials else "",
        api_secret="test-secret" if credentials else "",
        dry_run=dry_run,
        daily_limit_usd=Decimal(daily_limit),
        base_url="https://api.crypto.com/exchange/v1",
        trades_csv=tmp / "trades.csv",
        request_timeout=5.0,
        order_status_timeout=1.0,
        strategy=strategy,
    )
