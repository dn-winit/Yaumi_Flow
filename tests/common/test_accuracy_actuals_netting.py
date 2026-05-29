"""Hermetic guards on the live accuracy actuals path.

Two invariants this suite locks in:

1. ``AccuracyService._fetch_actuals`` reads from the pre-netted
   ``yf_sales_transactions.actual_sold`` column (same AIML DB as
   predictions). A future "simplification" that points it back at
   YaumiLive's gross sales view would silently re-introduce the
   gross/net scale mismatch that biases drift.

2. The reconciliation cron that writes ``actual_sold`` uses the shared
   ``NET_SOLD_CASE_SQL`` fragment training uses for ``sales_recent.csv``.
   That's why reading ``actual_sold`` is equivalent to scoring against
   the training target -- if the cron's source changes, this assumption
   breaks and ``recent_accuracy`` drifts off scale again.

Both checks introspect static artefacts (SQL string, source file) so
they run in CI with no DB.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from common.sql_fragments import NET_SOLD_CASE_SQL
from demand_forecasting_pipeline.config.settings import get_settings
from demand_forecasting_pipeline.services.accuracy_service import AccuracyService


def _capture_sql(svc: AccuracyService) -> tuple[str, list, str]:
    """Spy ``_query`` to capture SQL + params + the connection string it
    was called against -- without touching the database."""
    captured: dict = {}

    def _spy(conn_str: str, sql: str, params: list) -> pd.DataFrame:
        captured["sql"] = sql
        captured["params"] = list(params)
        captured["conn"] = conn_str
        return pd.DataFrame(columns=["trx_date", "route_code", "item_code", "actual_qty"])

    svc._query = _spy  # type: ignore[method-assign]
    pred_df = pd.DataFrame(
        {
            "trx_date":   ["2026-05-01", "2026-05-15"],
            "route_code": ["R01", "R01"],
            "item_code":  ["I01", "I02"],
        }
    )
    svc._fetch_actuals(pred_df, route_code="R01", item_code=None)
    return captured["sql"], captured["params"], captured["conn"]


def test_actuals_query_targets_pre_netted_table() -> None:
    """Must read from ``yf_sales_transactions.actual_sold`` -- not from
    the YaumiLive sales view. The cron has already done the netting; we
    just SELECT it."""
    svc = AccuracyService(get_settings())
    sql, _, _ = _capture_sql(svc)
    assert "actual_sold" in sql, (
        "actuals SQL no longer reads pre-netted actual_sold column -- "
        "drift will compare against the wrong scale"
    )
    assert svc._s.sales_transactions_table in sql, (
        f"actuals SQL must target {svc._s.sales_transactions_table}, "
        "the table the reconciliation cron writes net actuals to"
    )


def test_actuals_query_uses_aiml_not_yaumilive() -> None:
    """Connection string must be the AIML DB -- same one ``_fetch_predicted``
    uses. Pointing it back at the live DB would re-introduce both the
    cross-DB latency AND the gross-vs-net scale mismatch."""
    svc = AccuracyService(get_settings())
    _, _, conn = _capture_sql(svc)
    assert conn == svc._s.db.connection_string(), (
        "actuals query must hit the AIML predictions DB (no cross-DB hop)"
    )


def test_reconciliation_cron_writes_net_actuals() -> None:
    """The chain of trust: AccuracyService trusts ``actual_sold`` to be
    return-adjusted *because* the reconciliation cron computes it with
    ``NET_SOLD_CASE_SQL`` -- the same fragment training's
    ``sales_recent.csv`` uses. If a future commit swaps the cron to a
    gross SQL, this test catches the divergence before recent_accuracy
    silently drifts off the baseline scale."""
    refresh_path = (
        Path(__file__).resolve().parent.parent.parent
        / "demand_forecasting_pipeline"
        / "services"
        / "reconciliation_refresh.py"
    )
    src = refresh_path.read_text(encoding="utf-8")
    assert "NET_SOLD_CASE_SQL" in src, (
        "reconciliation_refresh.py no longer references NET_SOLD_CASE_SQL "
        "-- actual_sold may not be return-adjusted; recent_accuracy will "
        "fall off the baseline scale silently"
    )
    # Sanity: the fragment itself still subtracts returns (defensive
    # check in case someone edits the fragment instead of the caller).
    assert "rj.ret_qty" in NET_SOLD_CASE_SQL, (
        "NET_SOLD_CASE_SQL no longer subtracts return quantity"
    )
