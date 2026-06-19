"""The held-out acceptance tests — the loop never optimizes against these (Ch 9).

They enforce the part of the spec the visible tests miss: the boundary at quantity == 10.
With the seeded bug (`quantity > 10`) the first test fails; only a correct fix (`>= 10`)
passes both gates.
"""
from pricing import line_total


def test_discount_applies_at_the_boundary():
    assert line_total(2.00, 10) == 18.00     # "10 or more" qualifies: 20.00 * 0.9


def test_just_below_boundary_is_not_discounted():
    assert line_total(2.00, 9) == 18.00      # no discount
