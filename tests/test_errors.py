import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests

import main
from config import ConfigError, load_settings
from crypto_client import (
    AuthenticationError,
    CryptoComClient,
    HttpError,
    InsufficientBalanceError,
    InvalidOrderError,
    NetworkError,
    NotFoundError,
    RateLimitError,
)
from trade_log import CSV_FIELDS, PendingOrderJournal, append_trade, live_spend_on
from tests.fakes import FakeExchange, FakeResponse, FakeSession, FixedRandom, Ticker, make_settings


def api_error(code: int, http: int = 400, message: str = "", original: str | None = None) -> FakeResponse:
    payload = {"id": 1, "method": "x", "code": code, "message": message}
    if original is not None:
        payload["original"] = original
    return FakeResponse(http, payload)


def ok(result: dict) -> FakeResponse:
    return FakeResponse(200, {"id": 1, "method": "x", "code": 0, "result": result})


def client_with(*responses) -> tuple[CryptoComClient, FakeSession]:
    session = FakeSession(*responses)
    client = CryptoComClient("key", "secret", session=session, clock=Ticker(), sleep=lambda _s: None)
    return client, session


ORDER = {"instrument_name": "USDU_USD", "side": "BUY", "type": "MARKET", "notional": "10.00",
         "client_oid": "oid-1", "spot_margin": "SPOT"}


class ClientErrorMappingTests(unittest.TestCase):
    def test_exchange_rejection_codes_map_to_specific_errors(self):
        cases = [
            (306, 500, InsufficientBalanceError),
            (213, 400, InvalidOrderError),
            (415, 500, InvalidOrderError),
            (40101, 401, AuthenticationError),
            (40103, 401, AuthenticationError),
        ]
        for code, http, cls in cases:
            with self.subTest(code=code):
                client, session = client_with(api_error(code, http))
                with self.assertRaises(cls) as ctx:
                    client.create_order(ORDER)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(len(session.calls), 1)

    def test_string_success_code_is_accepted(self):
        client, _ = client_with(FakeResponse(200, {"code": "0", "result": {"order_id": "42", "client_oid": "oid-1"}}))
        self.assertEqual(client.create_order(ORDER)["order_id"], "42")

    def test_error_text_never_includes_echoed_original_request(self):
        client, _ = client_with(api_error(40101, 401, "UNAUTHORIZED", original='{"api_key":"key","sig":"abc"}'))
        with self.assertRaises(AuthenticationError) as ctx:
            client.create_order(ORDER)
        self.assertNotIn("api_key", str(ctx.exception))

    def test_expired_nonce_on_read_is_retried_with_fresh_signature(self):
        client, session = client_with(api_error(40102), ok({"data": []}))
        self.assertEqual(client.get_available_balance("USD"), Decimal("0"))
        first, second = (call["json"] for call in session.calls)
        self.assertNotEqual(first["nonce"], second["nonce"])
        self.assertNotEqual(first["sig"], second["sig"])

    def test_auth_failure_on_read_is_not_retried(self):
        client, session = client_with(api_error(40101, 401))
        with self.assertRaises(AuthenticationError):
            client.get_available_balance("USD")
        self.assertEqual(len(session.calls), 1)

    def test_non_json_429_is_rate_limit(self):
        client, _ = client_with(FakeResponse(429), FakeResponse(429), FakeResponse(429))
        with self.assertRaises(RateLimitError):
            client.get_ticker("USDU_USD")

    def test_create_order_is_never_retried_by_the_client(self):
        for failure in (FakeResponse(502), requests.Timeout("read timed out"), api_error(50001, 500)):
            with self.subTest(failure=failure):
                client, session = client_with(failure, ok({"order_id": "1"}))
                with self.assertRaises((HttpError, NetworkError, main.TransientServerError)):
                    client.create_order(ORDER)
                self.assertEqual(len(session.calls), 1)

    def test_available_balance_excludes_reserved_funds(self):
        client, _ = client_with(ok({"data": [{"position_balances": [
            {"instrument_name": "USD", "quantity": "25.00", "reserved_qty": "5.00", "max_withdrawal_balance": "25.00"},
        ]}]}))
        self.assertEqual(client.get_available_balance("USD"), Decimal("20.00"))


class SubmitOrderIdempotencyTests(unittest.TestCase):
    def submit(self, exchange):
        return main.submit_order(exchange, dict(ORDER), sleep=lambda _s: None)

    def test_timeout_then_lookup_finds_order_without_resubmitting(self):
        exchange = FakeExchange(create_effects=[NetworkError("timeout")],
                                detail_effects=[{"order_id": "777", "status": "FILLED"}])
        self.assertEqual(self.submit(exchange), "777")
        self.assertEqual(len(exchange.created), 1)
        self.assertEqual(exchange.detail_queries[0]["client_oid"], "oid-1")

    def test_timeout_and_not_found_resubmits_with_same_client_oid(self):
        not_found = NotFoundError("private/get-order-detail", 40401, "NOT_FOUND", 200)
        exchange = FakeExchange(create_effects=[NetworkError("timeout"), {"order_id": "888"}],
                                detail_effects=[not_found, not_found, not_found])
        self.assertEqual(self.submit(exchange), "888")
        self.assertEqual([p["client_oid"] for p in exchange.created], ["oid-1", "oid-1"])

    def test_duplicate_client_oid_resolves_to_existing_order(self):
        duplicate = main.DuplicateOrderError("private/create-order", 204, "DUPLICATE_CLORDID", 400)
        exchange = FakeExchange(create_effects=[duplicate], detail_effects=[{"order_id": "999", "status": "FILLED"}])
        self.assertEqual(self.submit(exchange), "999")

    def test_definitive_rejection_is_not_retried(self):
        rejection = InsufficientBalanceError("private/create-order", 306, "", 500)
        exchange = FakeExchange(create_effects=[rejection])
        with self.assertRaises(InsufficientBalanceError):
            self.submit(exchange)
        self.assertEqual(len(exchange.created), 1)

    def test_lookup_failure_leaves_state_unknown_instead_of_resubmitting(self):
        exchange = FakeExchange(create_effects=[NetworkError("timeout")],
                                detail_effects=[AuthenticationError("private/get-order-detail", 40101, "", 401)])
        with self.assertRaises(main.OrderStateUnknownError):
            self.submit(exchange)
        self.assertEqual(len(exchange.created), 1)


class ConfigTests(unittest.TestCase):
    def test_dry_run_is_default(self):
        self.assertTrue(load_settings({}).dry_run)

    def test_unrecognised_dry_run_value_is_an_error_not_live(self):
        with self.assertRaises(ConfigError):
            load_settings({"DRY_RUN": "nope"})

    def test_live_mode_requires_credentials(self):
        with self.assertRaises(ConfigError):
            load_settings({"DRY_RUN": "false"})
        with self.assertRaises(ConfigError):
            load_settings({"DRY_RUN": "false", "CRYPTO_COM_API_KEY": "k"})

    def test_invalid_daily_limit_is_rejected(self):
        for value in ("0", "-5", "abc", "NaN"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_settings({"DAILY_LIMIT_USD": value})

    def test_repr_hides_credentials(self):
        settings = load_settings({"CRYPTO_COM_API_KEY": "visible-key", "CRYPTO_COM_API_SECRET": "hidden-secret"})
        self.assertNotIn("hidden-secret", repr(settings))
        self.assertNotIn("visible-key", repr(settings))


class DailySpendTests(unittest.TestCase):
    def test_counts_only_live_orders_from_the_given_local_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.csv"
            today = datetime.now().astimezone()
            iso = today.isoformat()
            rows = [
                {"timestamp_utc": iso, "mode": "LIVE", "status": "FILLED", "requested_usd": "5.00", "executed_value_usd": "4.99"},
                {"timestamp_utc": iso, "mode": "LIVE", "status": "REJECTED", "requested_usd": "9.00", "executed_value_usd": "0"},
                {"timestamp_utc": iso, "mode": "LIVE", "status": "ACTIVE", "requested_usd": "3.00", "executed_value_usd": ""},
                {"timestamp_utc": iso, "mode": "DRY_RUN", "status": "DRY_RUN", "requested_usd": "19.00"},
                {"timestamp_utc": "2020-01-01T12:00:00+00:00", "mode": "LIVE", "status": "FILLED",
                 "requested_usd": "8.00", "executed_value_usd": "8.00"},
            ]
            for row in rows:
                append_trade(path, row)
            self.assertEqual(live_spend_on(path, today.date()), Decimal("7.99"))
            self.assertEqual(live_spend_on(path, date(2000, 1, 1)), Decimal("0"))


class RunSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_bot(self, settings, exchange, rand=0.5):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main.run(settings, exchange, rng=FixedRandom(rand), sleep=lambda _s: None,
                            now=lambda: datetime.now(timezone.utc))
        return code, out.getvalue()

    def rows(self):
        with (self.tmp / "trades.csv").open(newline="") as handle:
            return list(csv.DictReader(handle))

    def test_dry_run_never_submits_and_logs_preview(self):
        exchange = FakeExchange()
        code, out = self.run_bot(make_settings(self.tmp, dry_run=True), exchange)
        self.assertEqual(code, main.EXIT_OK)
        self.assertEqual(exchange.created, [])
        self.assertIn("would buy $2.96", out)  # $20 * 0.296 (ask 0.99852) * 0.5
        self.assertNotIn("test-secret", out)
        self.assertNotIn("test-key", out)
        self.assertEqual([r["status"] for r in self.rows()], ["DRY_RUN"])
        self.assertEqual(list(self.rows()[0]), CSV_FIELDS)

    def test_daily_limit_blocks_order(self):
        exchange = FakeExchange(history=[{"side": "BUY", "cumulative_value": "48.00"}])
        code, out = self.run_bot(make_settings(self.tmp, dry_run=False), exchange)
        self.assertEqual(code, main.EXIT_NOT_PLACED)
        self.assertIn("daily limit", out)
        self.assertEqual(exchange.created, [])

    def test_below_exchange_minimum_is_not_submitted(self):
        exchange = FakeExchange()
        code, out = self.run_bot(make_settings(self.tmp, dry_run=False), exchange, rand=0.1)  # $5.92 * 0.1 = $0.59
        self.assertEqual(code, main.EXIT_NOT_PLACED)
        self.assertIn("below the exchange minimum", out)
        self.assertEqual(exchange.created, [])

    def test_price_at_or_above_one_dollar_buys_nothing(self):
        for ask in ("1.00000", "1.00010"):
            with self.subTest(ask=ask):
                exchange = FakeExchange(ask=ask)
                code, out = self.run_bot(make_settings(self.tmp, dry_run=False), exchange, rand=0.99)
                self.assertEqual(code, main.EXIT_NOT_PLACED)
                self.assertIn("at or above $1.000", out)
                self.assertEqual(exchange.created, [])

    def test_live_fill_is_verified_logged_and_journal_cleared(self):
        filled = {"order_id": "123", "status": "FILLED", "avg_price": "0.99852", "cumulative_quantity": "10.0",
                  "cumulative_value": "9.9852", "cumulative_fee": "0.005", "fee_instrument_name": "USDU"}
        exchange = FakeExchange(create_effects=[{"order_id": "123", "client_oid": "x"}],
                                detail_effects=[NotFoundError("d", 40401, "", 200), {"order_id": "123", "status": "ACTIVE"}, filled])
        code, out = self.run_bot(make_settings(self.tmp, dry_run=False), exchange)
        self.assertEqual(code, main.EXIT_OK)
        self.assertEqual(len(exchange.created), 1)
        row = self.rows()[0]
        self.assertEqual((row["mode"], row["status"], row["order_id"], row["quantity"]), ("LIVE", "FILLED", "123", "10.0"))
        self.assertEqual(out.strip(), "Bought 10.0 USDU for $9.99 at $0.99852 (fee 0.005 USDU)")
        self.assertIsNone(PendingOrderJournal(self.tmp / ".pending_order.json").load())

    def test_rejected_live_order_reports_failure(self):
        filled_nothing = {"order_id": "5", "status": "REJECTED", "cumulative_quantity": "0", "reason": "306"}
        exchange = FakeExchange(create_effects=[{"order_id": "5"}], detail_effects=[filled_nothing])
        code, out = self.run_bot(make_settings(self.tmp, dry_run=False), exchange)
        self.assertEqual(code, main.EXIT_FAILED)
        self.assertIn("nothing bought", out)
        self.assertEqual(self.rows()[0]["status"], "REJECTED")

    def test_unverified_order_blocks_next_live_run(self):
        journal = PendingOrderJournal(self.tmp / ".pending_order.json")
        journal.save({"client_oid": "old-oid", "amount": "4.00", "created_at": "2026-01-01T00:00:00+00:00"})
        exchange = FakeExchange(detail_effects=[{"order_id": "1", "status": "ACTIVE"}])
        code, out = self.run_bot(make_settings(self.tmp, dry_run=False), exchange)
        self.assertEqual(code, main.EXIT_FAILED)
        self.assertIn("Refusing to place a new order", out)
        self.assertEqual(exchange.created, [])
        self.assertIsNotNone(journal.load())

    def test_unavailable_market_never_falls_back(self):
        exchange = FakeExchange()
        exchange.get_instrument = lambda symbol: None
        with self.assertRaises(main.MarketUnavailableError), redirect_stdout(io.StringIO()):
            main.run(make_settings(self.tmp, dry_run=False), exchange, rng=FixedRandom(0.5), sleep=lambda _s: None)
        self.assertEqual(exchange.created, [])


if __name__ == "__main__":
    unittest.main()
