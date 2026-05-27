"""SQL building blocks shared across services. Centralised so the
return-netting CASE clause stays byte-identical between
``data_import.core.queries.sales_recent`` and
``demand_forecasting_pipeline.services.reconciliation_refresh``.
"""
from __future__ import annotations


# Per-row net-sold CASE clause. Drops non-positive gross rows and clamps
# the net at 0 so an over-return can't push one line negative and mask
# other lines in the same GROUP BY. Fixed aliases: ``s`` outer invoice
# row, ``rj`` LEFT-JOINed returns subquery.
NET_SOLD_CASE_SQL = """\
CASE
    WHEN s.QuantityInPCs > 0 THEN
        CASE
            WHEN s.QuantityInPCs - COALESCE(rj.ret_qty, 0) > 0
            THEN s.QuantityInPCs - COALESCE(rj.ret_qty, 0)
            ELSE 0
        END
    ELSE 0
END\
"""


# Return-aggregation subquery skeleton. Callers fill in {view},
# {route_clause}, {date_clause}. Emits (InvoiceRef, ItemCode, ret_qty) where
# ret_qty is the positive sum of returns per (invoice, item). The IS NOT NULL
# filter drops orphan returns so the LEFT JOIN only subtracts attributable rows.
RETURNS_SUBQUERY_BODY_SQL = """\
SELECT
    r.ReturnItem_InvoiceRef AS InvoiceRef,
    r.ItemCode,
    SUM(-r.QuantityInPCs)   AS ret_qty
FROM {view} r WITH (NOLOCK)
WHERE r.TrxType IN (?, ?)
  AND r.ReturnItem_InvoiceRef IS NOT NULL
  {route_clause}
  {date_clause}
GROUP BY r.ReturnItem_InvoiceRef, r.ItemCode\
"""
