"""The visible (in-sample) tests — what the loop optimizes against each tick.

Note: these pass even with the seeded bug, because they don't probe the boundary at
quantity == 10. That gap is the whole point — a green here is necessary, not sufficient.
"""
from pricing import line_total


def test_small_order_has_no_discount():
    assert line_total(2.00, 3) == 6.00


def test_large_order_is_discounted():
    assert line_total(2.00, 20) == 36.00     # 40.00 * 0.9
