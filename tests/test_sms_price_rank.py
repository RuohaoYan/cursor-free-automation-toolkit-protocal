from __future__ import annotations

from modules.hero_sms_provider import (
    OperatorQuote,
    PhoneCountry,
    pick_country_by_price_rank,
    pick_operator_quote_by_price_rank,
)


def test_pick_country_by_price_rank_second_cheapest() -> None:
    rows = [
        PhoneCountry("A", "1", "A", 1, (), price=0.05, count=10),
        PhoneCountry("B", "2", "B", 2, (), price=0.08, count=20),
        PhoneCountry("C", "3", "C", 3, (), price=0.12, count=30),
    ]
    picked = pick_country_by_price_rank(rows, rank=1)
    assert picked.iso_code == "B"


def test_pick_operator_quote_by_price_rank_second_cheapest() -> None:
    quotes = [
        OperatorQuote("op1", "op1", 0.05, 5),
        OperatorQuote("op2", "op2", 0.08, 8),
        OperatorQuote("op3", "op3", 0.12, 12),
    ]
    picked = pick_operator_quote_by_price_rank(quotes, rank=1, max_price=0.1)
    assert picked is not None
    assert picked.operator == "op2"
