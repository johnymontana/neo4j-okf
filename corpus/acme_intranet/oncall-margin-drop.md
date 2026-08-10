# Runbook: sudden gross margin drop

**Owner:** analytics guild
**Author:** human:kliu@acme
**Last reviewed:** 2026-03-12

Use this when the weekly margin number moves more than 3 points week over week
and nobody has announced a pricing change.

## Trigger

The Margin Daily dashboard flags a week-over-week move greater than 3 points.

## Steps

1. Check whether the definition changed. The FY2026 Cost Allocation Memo moved
   the formula from product-cost-only to full COGS on 2026-02-01; a one-time
   step change around that date is expected, not an incident.
2. Check freshness of the Customer Orders table. A stalled hourly load shows up
   as a margin move because the denominator is short.
3. Check `acme.finance.fx_daily_rates` for gaps. A missing rate silently drops
   non-USD orders from the numerator.
4. Check for a large returns batch. Returns land in the same table with
   `order_status = 'returned'`.
5. If none of the above, escalate per the escalation matrix.

## What not to do

Do not "fix" the number by reverting to the pre-2026 formula. That produces a
figure that reconciles to nothing and has been the cause of two prior incidents.
