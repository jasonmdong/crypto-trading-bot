"""Minimal authenticated client for the Crypto.com Exchange REST API v1.

Reference: https://exchange-developer.crypto.com/exchange/v1/docs/api/rest-common-api-reference

Signing (Digital Signature section of the docs):
    sig = hex(HMAC_SHA256(secret, method + id + api_key + param_string + nonce))
where param_string concatenates the alphabetically sorted params as key+value.

Write calls (`private/create-order`) are never retried here: a lost response
does not mean the order was not placed. Duplicate-safe resubmission lives in
`main.submit_order`, which first looks the order up by `client_oid`.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import requests

from config import PRODUCTION_BASE_URL

# Mirrors MAX_LEVEL in the official Python signing example.
_MAX_PARAM_DEPTH = 3

# Code -> name, from "Response and Reason Codes" in the official docs (subset
# relevant to spot market orders; unknown codes are still reported verbatim).
ERROR_NAMES: dict[int, str] = {
    202: "ACCOUNT_IS_SUSPENDED",
    204: "DUPLICATE_CLORDID",
    206: "INSTRUMENT_EXPIRED",
    208: "INSTRUMENT_NOT_TRADABLE",
    209: "INVALID_INSTRUMENT",
    211: "INVALID_CURRENCY",
    213: "INVALID_ORDERQTY",
    218: "INVALID_ORDTYPE",
    224: "REJ_BY_MATCHING_ENGINE",
    227: "INVALID_SLIPPAGE",
    306: "INSUFFICIENT_AVAILABLE_BALANCE",
    308: "INVALID_PRICE",
    309: "MARKET_IS_NOT_OPEN",
    314: "EXCEEDS_MAX_ORDER_SIZE",
    318: "EXCEEDS_MAX_ALLOWED_ORDERS",
    321: "EXCEEDS_MAX_AVAILABLE_BALANCE",
    401: "ACCOUNT_DOES_NOT_EXIST",
    406: "ACCOUNT_IS_NOT_ACTIVE",
    410: "USER_IS_NOT_ACTIVE",
    415: "BELOW_MIN_ORDER_SIZE",
    40001: "BAD_REQUEST",
    40002: "METHOD_NOT_FOUND",
    40003: "INVALID_REQUEST",
    40004: "MISSING_OR_INVALID_ARGUMENT",
    40006: "DUPLICATE_REQUEST",
    40101: "UNAUTHORIZED",
    40102: "INVALID_NONCE",
    40103: "IP_ILLEGAL",
    40104: "USER_TIER_INVALID",
    40401: "NOT_FOUND",
    40801: "REQUEST_TIMEOUT",
    42901: "TOO_MANY_REQUESTS",
    50001: "ERR_INTERNAL",
    120009: "REJECTED",
}


class CryptoComError(Exception):
    """Base class for all client errors."""


class NetworkError(CryptoComError):
    """No HTTP response (timeout / connection failure). Outcome of a write is unknown."""


class HttpError(CryptoComError):
    """HTTP response without a parseable API envelope (e.g. gateway 502 HTML page)."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class ApiError(CryptoComError):
    """The exchange answered with a non-zero response code."""

    def __init__(self, method: str, code: int, message: str, http_status: int):
        self.method = method
        self.code = code
        self.name = ERROR_NAMES.get(code, "UNKNOWN")
        self.api_message = message
        self.http_status = http_status
        # `original` from the response is intentionally never stored: it echoes
        # the signed request including api_key.
        super().__init__(f"{method} failed: code {code} {self.name} (HTTP {http_status}) {message}".rstrip())


class AuthenticationError(ApiError):
    """Bad key/signature, IP not whitelisted, or tier not allowed."""


class InvalidNonceError(ApiError):
    """Request timestamp differs from server time by more than 60 seconds."""


class RateLimitError(ApiError):
    pass


class InsufficientBalanceError(ApiError):
    pass


class InvalidOrderError(ApiError):
    """Order rejected because of quantity / notional / price rules."""


class InstrumentUnavailableError(ApiError):
    pass


class DuplicateOrderError(ApiError):
    """client_oid or request already seen by the exchange."""


class NotFoundError(ApiError):
    pass


class TransientServerError(ApiError):
    """Server-side failure or timeout; the request outcome is unknown."""


_ERROR_CLASSES: dict[int, type[ApiError]] = {
    40101: AuthenticationError,
    40103: AuthenticationError,
    40104: AuthenticationError,
    40102: InvalidNonceError,
    42901: RateLimitError,
    306: InsufficientBalanceError,
    321: InsufficientBalanceError,
    213: InvalidOrderError,
    227: InvalidOrderError,
    308: InvalidOrderError,
    314: InvalidOrderError,
    415: InvalidOrderError,
    206: InstrumentUnavailableError,
    208: InstrumentUnavailableError,
    209: InstrumentUnavailableError,
    309: InstrumentUnavailableError,
    204: DuplicateOrderError,
    40006: DuplicateOrderError,
    40401: NotFoundError,
    40801: TransientServerError,
    50001: TransientServerError,
}


def make_api_error(method: str, code: int, message: str, http_status: int) -> ApiError:
    cls = _ERROR_CLASSES.get(code, ApiError)
    return cls(method, code, message, http_status)


def params_to_str(obj: Any, level: int = 0) -> str:
    """Flatten params into the signature string defined by the official docs.

    Keys are sorted ascending and concatenated as key+value with no delimiters;
    list elements are concatenated in order; None becomes "null".
    """
    if level >= _MAX_PARAM_DEPTH:
        return str(obj)
    if isinstance(obj, dict):
        parts: list[str] = []
        for key in sorted(obj):
            parts.append(str(key))
            parts.append(params_to_str(obj[key], level + 1) if isinstance(obj[key], (dict, list)) else _scalar(obj[key]))
        return "".join(parts)
    if isinstance(obj, list):
        return "".join(params_to_str(item, level) if isinstance(item, (dict, list)) else _scalar(item) for item in obj)
    return _scalar(obj)


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (bool, float)):
        # The API requires numbers as strings; bool/float renderings differ
        # between languages and would break signatures.
        raise TypeError(f"params must use strings for numbers/flags, got {type(value).__name__}: {value!r}")
    return str(value)


def sign(method: str, request_id: int, api_key: str, params: dict[str, Any], nonce: int, api_secret: str) -> str:
    payload = f"{method}{request_id}{api_key}{params_to_str(params)}{nonce}"
    return hmac.new(api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


class CryptoComClient:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        *,
        base_url: str = PRODUCTION_BASE_URL,
        timeout: float = 10.0,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        read_attempts: int = 3,
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._read_attempts = read_attempts
        self._ids = itertools.count(int(clock() * 1000))

    def __repr__(self) -> str:  # never expose credentials
        return f"CryptoComClient(base_url={self._base_url!r}, authenticated={bool(self._api_key)})"

    # ------------------------------------------------------------------ transport

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def build_private_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not (self._api_key and self._api_secret):
            raise CryptoComError(f"{method} requires API credentials")
        request_id = next(self._ids)
        nonce = self._now_ms()
        return {
            "id": request_id,
            "method": method,
            "api_key": self._api_key,
            "params": params,
            "nonce": nonce,
            "sig": sign(method, request_id, self._api_key, params, nonce, self._api_secret),
        }

    def _send(self, http_method: str, method: str, *, query: dict[str, Any] | None = None,
              body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url}/{method}"
        try:
            response = self._session.request(
                http_method, url, params=query, json=body,
                headers={"Content-Type": "application/json"}, timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise NetworkError(f"{method}: {type(exc).__name__} contacting {self._base_url}") from exc

        status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or "code" not in payload:
            if status == 429:
                raise RateLimitError(method, 42901, "rate limited (non-JSON response)", status)
            raise HttpError(f"{method}: unexpected HTTP {status} response without API envelope", status)
        try:
            code = int(payload["code"])
        except (TypeError, ValueError) as exc:
            raise HttpError(f"{method}: malformed response code {payload['code']!r}", status) from exc
        if code != 0:
            raise make_api_error(method, code, str(payload.get("message", "")), status)
        if status >= 400:
            raise HttpError(f"{method}: HTTP {status} with success code", status)
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _is_retryable(exc: CryptoComError) -> bool:
        if isinstance(exc, (NetworkError, RateLimitError, InvalidNonceError, TransientServerError)):
            return True
        return isinstance(exc, HttpError) and exc.status >= 500

    def _with_read_retries(self, call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Retry an idempotent read. Each attempt is re-signed with a fresh nonce."""
        for attempt in range(1, self._read_attempts + 1):
            try:
                return call()
            except CryptoComError as exc:
                if attempt == self._read_attempts or not self._is_retryable(exc):
                    raise
                self._sleep(0.5 * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    def public_get(self, method: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._with_read_retries(lambda: self._send("GET", method, query=query))

    def private_read(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._with_read_retries(
            lambda: self._send("POST", method, body=self.build_private_request(method, params))
        )

    # ------------------------------------------------------------- public data

    def get_instrument(self, symbol: str) -> dict[str, Any] | None:
        for item in self.public_get("public/get-instruments").get("data", []):
            if item.get("symbol") == symbol:
                return item
        return None

    def get_risk_limits(self, base_currency: str) -> dict[str, Any] | None:
        """Per-token spot limits (min/max order notional in USD) from public/get-risk-parameters."""
        result = self.public_get("public/get-risk-parameters")
        for item in result.get("base_currency_config", []):
            if item.get("instrument_name") == base_currency:
                return item
        return None

    def get_ticker(self, instrument_name: str) -> dict[str, Any] | None:
        data = self.public_get("public/get-tickers", {"instrument_name": instrument_name}).get("data", [])
        return data[0] if data else None

    # ------------------------------------------------------------ private reads

    def get_available_balance(self, currency: str) -> Decimal:
        accounts = self.private_read("private/user-balance", {}).get("data", [])
        for account in accounts:
            for position in account.get("position_balances", []):
                if position.get("instrument_name") != currency:
                    continue
                free = Decimal(position.get("quantity", "0")) - Decimal(position.get("reserved_qty", "0") or "0")
                withdrawable = position.get("max_withdrawal_balance")
                if withdrawable not in (None, ""):
                    free = min(free, Decimal(withdrawable))
                return max(free, Decimal("0"))
        return Decimal("0")

    def get_order_detail(self, *, order_id: str | None = None, client_oid: str | None = None) -> dict[str, Any]:
        if bool(order_id) == bool(client_oid):
            raise ValueError("pass exactly one of order_id or client_oid")
        params = {"order_id": str(order_id)} if order_id else {"client_oid": client_oid}
        return self.private_read("private/get-order-detail", params)

    def get_order_history(self, instrument_name: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        params = {
            "instrument_name": instrument_name,
            "start_time": str(start_ms),
            "end_time": str(end_ms),
            "limit": 100,
        }
        return self.private_read("private/get-order-history", params).get("data", [])

    # ------------------------------------------------------------------- write

    def create_order(self, params: dict[str, Any]) -> dict[str, Any]:
        """Submit `private/create-order` exactly once. Never retried here."""
        method = "private/create-order"
        return self._send("POST", method, body=self.build_private_request(method, params))
