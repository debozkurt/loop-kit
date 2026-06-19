"""Line-item pricing with a bulk discount.

Spec: a line's total is `unit_price * quantity`, rounded to 2 decimals, and a line qualifies
for a 10% bulk discount when the quantity is **10 or more**.
"""


def line_total(unit_price: float, quantity: int) -> float:
    """Return the total for `quantity` units at `unit_price`, applying the bulk discount."""
    subtotal = unit_price * quantity
    if quantity > 10:          # BUG: the spec says "10 or more", so this should be >= 10
        subtotal *= 0.9
    return round(subtotal, 2)
