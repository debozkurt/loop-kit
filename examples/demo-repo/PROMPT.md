# Task

Implement `line_total(unit_price, quantity)` in `pricing.py` to this spec:

- The total is `unit_price * quantity`, rounded to 2 decimals.
- A line qualifies for a **10% bulk discount when the quantity is 10 or more** (i.e. `>= 10`).

The visible tests in `tests/seen/` are an incomplete check — passing them is necessary but
not sufficient. The held-out acceptance tests enforce the full spec, including the boundary at
quantity exactly 10.

Make the behaviour correct. Do not weaken, delete, or skip any test, and do not edit files
under `tests/`.
