"""Bounded usage-budget helpers for hosted cloud runs.

Token quotas are enforced independently of provider pricing. Optional USD budgets
are enabled only when operators configure an explicit monthly limit and both input
and output prices; prices are operator-supplied estimates, not provider-verified.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

DEFAULT_MONTHLY_PROMPT_TOKENS = 2_000_000
DEFAULT_MONTHLY_COMPLETION_TOKENS = 160_000
DEFAULT_RUN_PROMPT_TOKENS = 200_000
DEFAULT_RUN_COMPLETION_TOKENS = 16_384
MAX_TOKEN_BUDGET = 10_000_000_000
MAX_COST_MICROS = 1_000_000_000_000_000  # USD 1,000,000,000 in micro-USD.


def validate_cost_micros(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COST_MICROS:
        raise ValueError(f"{name} must be an integer between 0 and {MAX_COST_MICROS}")
    return value


def validate_token_count(value: int, name: str, *, maximum: int = MAX_TOKEN_BUDGET) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 0 and {maximum}")
    return value


def current_month_start(timestamp: float | None = None) -> int:
    now = time.time() if timestamp is None else timestamp
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    return int(datetime(dt.year, dt.month, 1, tzinfo=timezone.utc).timestamp())


def seconds_until_month_end(timestamp: float | None = None) -> int:
    now = time.time() if timestamp is None else timestamp
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    if dt.month == 12:
        next_month = datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(dt.year, dt.month + 1, 1, tzinfo=timezone.utc)
    return max(1, math.ceil(next_month.timestamp() - now))


def parse_usd_micros(value: str, name: str) -> int:
    try:
        amount = Decimal(value.strip())
    except (AttributeError, InvalidOperation) as exc:
        raise ValueError(f"{name} must be a positive USD amount") from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000000"):
        raise ValueError(f"{name} must be a positive USD amount no greater than 1000000000")
    return int((amount * Decimal(1_000_000)).to_integral_value(rounding=ROUND_FLOOR))


def parse_price_micros_per_million(value: str, name: str) -> int:
    """Convert USD per million tokens to micro-USD per million tokens."""
    try:
        amount = Decimal(value.strip())
    except (AttributeError, InvalidOperation) as exc:
        raise ValueError(f"{name} must be a non-negative USD-per-million price") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal("100000"):
        raise ValueError(f"{name} must be between 0 and 100000 USD per million tokens")
    return int((amount * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))


def pricing_from_environment(environ=None) -> tuple[int | None, int, int]:
    import os
    values = environ if environ is not None else os.environ
    spend = values.get("NIJI_CLOUD_MAX_MONTHLY_SPEND_USD", "").strip()
    input_price = values.get("NIJI_CLOUD_INPUT_PRICE_USD_PER_MILLION_TOKENS", "").strip()
    output_price = values.get("NIJI_CLOUD_OUTPUT_PRICE_USD_PER_MILLION_TOKENS", "").strip()
    if any((spend, input_price, output_price)) and not all((spend, input_price, output_price)):
        raise ValueError("Monthly USD spend budget requires its limit and both explicit token prices")
    if not spend:
        return None, 0, 0
    return (
        parse_usd_micros(spend, "NIJI_CLOUD_MAX_MONTHLY_SPEND_USD"),
        parse_price_micros_per_million(
            input_price, "NIJI_CLOUD_INPUT_PRICE_USD_PER_MILLION_TOKENS"),
        parse_price_micros_per_million(
            output_price, "NIJI_CLOUD_OUTPUT_PRICE_USD_PER_MILLION_TOKENS"),
    )


def cost_micros(prompt_tokens: int, completion_tokens: int,
                prompt_price_micros_per_million: int,
                completion_price_micros_per_million: int) -> int:
    """Conservative ceiling to whole micro-USD using configured per-million prices."""
    p = (Decimal(prompt_tokens) * Decimal(prompt_price_micros_per_million) / Decimal(1_000_000))
    c = (Decimal(completion_tokens) * Decimal(completion_price_micros_per_million) / Decimal(1_000_000))
    return int((p + c).to_integral_value(rounding=ROUND_CEILING))
