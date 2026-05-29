"""Import endpoints -- gated as ``slow`` because each pull hits YaumiLive
and writes the CSV mirror. Running them at unbounded frequency would
saturate the warehouse and stomp on the scheduled cron's mirror state.
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_ok


@pytest.mark.slow
def test_import_single_dataset_incremental(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """``POST /import`` on one well-known small dataset. Verifies the
    incremental path returns a success envelope; doesn't assert row
    counts (those drift as new invoices land)."""
    resp = client.post(
        f"{base_urls['data_import']}/import",
        json={"dataset": "journey_plan", "mode": "incremental"},
        timeout=120.0,
    )
    body = assert_ok(resp, "journey_plan incremental")
    assert body.get("success") is True, f"import did not report success: {body}"


@pytest.mark.slow
def test_import_unknown_dataset_rejects(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """Unknown dataset name must NOT silently write -- the response
    should carry success=False with a structured error."""
    resp = client.post(
        f"{base_urls['data_import']}/import",
        json={"dataset": "no_such_dataset", "mode": "incremental"},
        timeout=30.0,
    )
    # Either 4xx (route/Pydantic rejects) or 2xx with success=False.
    if 200 <= resp.status_code < 300:
        body = resp.json()
        assert body.get("success") is False, (
            f"unknown dataset returned 2xx + success=True: {body}"
        )
    else:
        assert 400 <= resp.status_code < 500, (
            f"unknown dataset should be 4xx, got {resp.status_code}: {resp.text[:200]}"
        )


def test_delete_aware_merge_evicts_db_deleted_rows(tmp_path) -> None:
    """Regression: refresh-window incremental import must drop existing
    CSV rows that the DB has DELETED inside the lookback window.

    The bug: a prior version of ``import_dataset`` did
    ``pd.concat([existing_df, new_df]).drop_duplicates(subset=key_cols,
    keep='last')`` -- which preserves orphan existing rows if no fresh
    row in ``new_df`` shares their key. The DB's
    ``WHEN NOT MATCHED BY SOURCE THEN DELETE`` cascade silently
    accumulated 82,625 stale ``yf_demand_forecast`` rows in the CSV
    mirror over months of retrains. Now the importer drops any
    existing in-window row before the concat so DB DELETEs propagate.

    Runs in-process (no DB/HTTP) so this stays in the non-``slow``
    tier and gates every CI build.
    """
    from unittest.mock import patch

    import pandas as pd

    csv_path = tmp_path / "demand_forecast.csv"
    existing = pd.DataFrame([
        # Outside window (older): MUST be preserved
        {"TrxDate": "2025-01-01", "RouteCode": "9105", "ItemCode": "X",
         "DataSplit": "Forecast", "Predicted": 10},
        # Inside window, key matched in new_df: will UPDATE
        {"TrxDate": "2026-05-26", "RouteCode": "9105", "ItemCode": "A",
         "DataSplit": "Forecast", "Predicted": 100},
        # Inside window, key NOT in new_df (DB deleted): MUST be evicted
        {"TrxDate": "2026-05-26", "RouteCode": "9105", "ItemCode": "Z_stale",
         "DataSplit": "Forecast", "Predicted": 999},
        {"TrxDate": "2026-05-27", "RouteCode": "9105", "ItemCode": "Z_stale",
         "DataSplit": "Forecast", "Predicted": 888},
    ])
    existing.to_csv(csv_path, index=False)

    new_df = pd.DataFrame([
        {"TrxDate": "2026-05-26", "RouteCode": "9105", "ItemCode": "A",
         "DataSplit": "Forecast", "Predicted": 150},
        {"TrxDate": "2026-05-26", "RouteCode": "9105", "ItemCode": "B",
         "DataSplit": "Forecast", "Predicted": 200},
        {"TrxDate": "2026-05-27", "RouteCode": "9105", "ItemCode": "A",
         "DataSplit": "Forecast", "Predicted": 160},
    ])

    from data_import.config.settings import Settings
    from data_import.core.importer import DataImporter

    # Build a fresh Settings instance pointed at tmp_path so
    # ``data_path("demand_forecast.csv")`` resolves to our CSV.
    s = Settings(
        data_dir=str(tmp_path),
        demand_forecast_file="demand_forecast.csv",
    )
    importer = DataImporter(s)

    with patch.object(importer._db, "execute_query", return_value=new_df), \
         patch.object(importer, "_detect_last_date",
                      return_value=("2026-05-26", len(existing))):
        result = importer.import_dataset(
            dataset="demand_forecast",
            mode="incremental",
            lookback_days=30,
        )

    assert result["success"] is True, f"import failed: {result}"
    out = pd.read_csv(csv_path)

    # Pre-window historical row preserved
    assert ((out["TrxDate"] == "2025-01-01") & (out["ItemCode"] == "X")).any(), (
        "DELETE-aware merge wrongly evicted the pre-window historical row"
    )
    # In-window matched key UPDATED to new value
    matched = out[(out["TrxDate"] == "2026-05-26") & (out["ItemCode"] == "A")]
    assert len(matched) == 1 and matched.iloc[0]["Predicted"] == 150, (
        f"in-window UPDATE missing or wrong value: "
        f"{matched.to_dict(orient='records')}"
    )
    # In-window INSERT landed
    inserted = out[(out["TrxDate"] == "2026-05-26") & (out["ItemCode"] == "B")]
    assert len(inserted) == 1 and inserted.iloc[0]["Predicted"] == 200, (
        f"in-window INSERT missing: {inserted.to_dict(orient='records')}"
    )
    # In-window DB-deleted rows MUST be gone
    stale = out[out["ItemCode"] == "Z_stale"]
    assert stale.empty, (
        f"DELETE-aware merge failed to evict stale rows; bug regressed: "
        f"{stale.to_dict(orient='records')}"
    )
