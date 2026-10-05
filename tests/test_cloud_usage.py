import unittest

from niji.cloud_usage import (
    MAX_COST_MICROS,
    cost_micros,
    current_month_start,
    parse_price_micros_per_million,
    parse_usd_micros,
    pricing_from_environment,
    seconds_until_month_end,
    validate_cost_micros,
)


class CloudUsageTests(unittest.TestCase):
    def test_usd_and_price_parsing_are_bounded_and_conservative(self):
        self.assertEqual(parse_usd_micros("50000", "spend"), 50_000_000_000)
        self.assertEqual(parse_price_micros_per_million("0.0000001", "price"), 1)
        self.assertEqual(cost_micros(1, 1, 1, 1), 1)
        for raw in ("0", "-1", "NaN", "Infinity", "1000000001"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_usd_micros(raw, "spend")
        for raw in ("-0.1", "NaN", "Infinity", "100001"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_price_micros_per_million(raw, "price")

    def test_partial_pricing_environment_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "requires its limit"):
            pricing_from_environment({"NIJI_CLOUD_MAX_MONTHLY_SPEND_USD": "10"})
        self.assertEqual(pricing_from_environment({}), (None, 0, 0))

    def test_cost_micro_bounds_are_independent_of_token_budgets(self):
        self.assertEqual(validate_cost_micros(50_000_000_000, "cost"), 50_000_000_000)
        self.assertEqual(validate_cost_micros(MAX_COST_MICROS, "cost"), MAX_COST_MICROS)
        for value in (-1, MAX_COST_MICROS + 1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_cost_micros(value, "cost")

    def test_monthly_periods_follow_utc_calendar_boundaries(self):
        february = 1_709_251_200  # 2024-03-01T00:00:00Z
        self.assertEqual(current_month_start(february - 1), 1_706_745_600)
        self.assertEqual(seconds_until_month_end(february), 31 * 24 * 60 * 60)
        self.assertEqual(seconds_until_month_end(february + 1), 31 * 24 * 60 * 60 - 1)


if __name__ == "__main__":
    unittest.main()
