ACME DATA WAREHOUSE - README
============================

Maintained by: process:platform-nightly
Last touched: 2026-01-20

Overview
--------

The warehouse is BigQuery. Three datasets matter for commerce reporting:

  acme.sales      orders, order_lines
  acme.catalog    products
  acme.finance    fx_daily_rates, payment_fees
  acme.logistics  fulfillment_cost, shipment_cost

Customer Orders
---------------

acme.sales.orders is one row per completed customer order across web, mobile
and marketplace channels. Loaded hourly. The order_status column moves through
placed -> shipped -> delivered -> returned. Only 'delivered' rows are eligible
for revenue recognition.

Amounts are stored in the transaction currency in net_amount, with the currency
in the currency column. Convert with acme.finance.fx_daily_rates joined on
currency and DATE(order_ts). There is no pre-converted USD column; anyone who
tells you otherwise is looking at a deprecated view.

Order Lines
-----------

acme.sales.order_lines is one row per product per order. Join to products on
product_id to pick up cost. Note that cost is the current catalog cost, not the
cost at time of order - a known limitation the platform team has not fixed.

Freshness and gotchas
---------------------

- Marketplace channel orders were backfilled in March 2024.
- The 30-day returns window means recent orders are provisional. Most analyses
  exclude orders newer than 30 days.
- fx_daily_rates has gaps on non-trading days; use the most recent prior rate.
