"""YaumiLive transaction-type vocabulary.

Single source of truth for the TrxType values that VW_GET_SALES_DETAILS
emits. Used by:
  * data_import.core.queries — to filter sales vs returns at the SQL layer
  * demand_forecasting_pipeline.services.reconciliation_refresh — to net
    returns against sales when computing actual_sold

If a YaumiLive vocab change ships, updating it here propagates to every
consumer in one commit. Previously these strings were duplicated in two
services' Settings classes with the same defaults but no compile-time
guard against drift.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SalesVocab:
    """Canonical TrxType vocabulary from VW_GET_SALES_DETAILS."""
    sales_invoice_trx_type: str = "SalesInvoice"
    bad_return_trx_type:    str = "Bad Return"
    good_return_trx_type:   str = "Good Return"
    sales_item_type:        str = "OrderItem"


# Module-level singleton; consumers import this rather than reconstructing.
SALES_VOCAB: SalesVocab = SalesVocab()
