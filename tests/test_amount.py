import unittest
from decimal import Decimal

from main import AmountError, MarketRules, build_order_params, generate_amount, validate_amount
from tests.fakes import FixedRandom

RULES = MarketRules(
    instrument="USDU_USD",
    qty_tick_size=Decimal("0.1"),
    min_notional=Decimal("1.0"),
    max_notional=Decimal("1000.0"),
    best_ask=Decimal("0.99852"),
)
NO_LIMIT = Decimal("1000000")


class GenerateAmountTests(unittest.TestCase):
    def test_formula_is_twenty_times_random_rounded_to_cents(self):
        cases = {0.0: "0.00", 0.5: "10.00", 0.12345: "2.47", 0.61701: "12.34", 0.9999999: "20.00"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                amount = generate_amount(FixedRandom(value))
                self.assertEqual(amount, Decimal(expected))
                self.assertEqual(amount.as_tuple().exponent, -2)

    def test_default_generator_stays_within_zero_to_twenty(self):
        for _ in range(2000):
            amount = generate_amount()
            self.assertTrue(Decimal("0") <= amount <= Decimal("20"))
            self.assertEqual(amount, amount.quantize(Decimal("0.01")))


class ValidateAmountTests(unittest.TestCase):
    def test_valid_amount_returns_quantity_rounded_down_to_tick(self):
        # 12.34 / 0.99852 = 12.358... -> 12.3 at a 0.1 tick
        self.assertEqual(validate_amount(Decimal("12.34"), RULES, NO_LIMIT, Decimal("100")), Decimal("12.3"))

    def test_rejects_zero(self):
        with self.assertRaisesRegex(AmountError, "positive"):
            validate_amount(Decimal("0.00"), RULES, NO_LIMIT)

    def test_rejects_sub_cent_precision(self):
        with self.assertRaisesRegex(AmountError, "2 decimal"):
            validate_amount(Decimal("5.005"), RULES, NO_LIMIT)

    def test_minimum_notional_boundary(self):
        with self.assertRaisesRegex(AmountError, "below the exchange minimum"):
            validate_amount(Decimal("0.99"), RULES, NO_LIMIT)
        self.assertEqual(validate_amount(Decimal("1.00"), RULES, NO_LIMIT), Decimal("1.0"))

    def test_rejects_above_exchange_maximum(self):
        with self.assertRaisesRegex(AmountError, "maximum"):
            validate_amount(Decimal("1000.01"), RULES, NO_LIMIT)

    def test_daily_limit_boundary(self):
        validate_amount(Decimal("7.50"), RULES, Decimal("7.50"))
        with self.assertRaisesRegex(AmountError, "daily limit"):
            validate_amount(Decimal("7.51"), RULES, Decimal("7.50"))

    def test_exhausted_daily_limit_reports_zero_remaining(self):
        with self.assertRaisesRegex(AmountError, r"remaining daily limit of \$0.00"):
            validate_amount(Decimal("5.00"), RULES, Decimal("-3"))

    def test_rejects_amount_above_available_balance(self):
        with self.assertRaisesRegex(AmountError, "available Exchange USD balance"):
            validate_amount(Decimal("12.34"), RULES, NO_LIMIT, Decimal("12.33"))

    def test_rejects_amount_too_small_for_quantity_tick_when_no_minimum_published(self):
        rules = MarketRules("USDU_USD", Decimal("0.1"), None, None, Decimal("0.99852"))
        with self.assertRaisesRegex(AmountError, "quantity tick"):
            validate_amount(Decimal("0.05"), rules, NO_LIMIT)


class OrderParamsTests(unittest.TestCase):
    def test_market_buy_by_notional_with_string_values(self):
        params = build_order_params(Decimal("5"), "oid-1")
        self.assertEqual(params["notional"], "5.00")
        self.assertEqual((params["instrument_name"], params["side"], params["type"]), ("USDU_USD", "BUY", "MARKET"))
        self.assertNotIn("quantity", params)
        self.assertTrue(all(isinstance(value, str) for value in params.values()))


if __name__ == "__main__":
    unittest.main()
