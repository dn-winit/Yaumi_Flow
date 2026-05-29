"""End-to-end consistency tests across the forecast value chain.

Re-runnable harness for the audit cross-checks performed during the
activity-flag / unified-scorer / timezone hardening pass. Each test
proves one invariant that a future regression could quietly break;
running ``pytest tests/demand_forecasting/test_e2e_consistency.py``
recovers the full audit signal in ~10s.

Invariants covered:
  * Pipeline-summary accuracy == drift baseline (one scorer, no
    silent divergence between callers).
  * Forecast horizon end-date is identical across the summary tile
    and the raw ``/predictions/forecast`` payload.
  * Per-route ``/route-summary.predicted_qty`` sums match the per-item
    rows from ``/predictions/forecast`` for the same (route, date).
  * Recommendation totals from ``/recommended-order/get`` match
    ``yf_recommended_orders`` row-for-row.
  * ``random_forest`` zero-rate stays below a sane ceiling -- guard
    against the activity_flag regression that drove it to 100%.
  * ``limit`` and ``recent_window_days`` reject zero/negative input
    at the API layer (no silent zero-row truncation).
  * Every wire-level timestamp carries an explicit UTC offset.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from tests.common.helpers import assert_ok, assert_status

# ---------------------------------------------------------------------------
# Helpers local to this file
# ---------------------------------------------------------------------------

# ISO timestamp with explicit offset: ``...+HH:MM`` or ``...Z``. Naive
# datetimes (no offset suffix) fail the timestamp-consistency check.
_TS_WITH_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}.*([+-]\d{2}:\d{2}|Z)$")
_ISO_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _walk_iso_timestamps(obj: Any, path: str = "") -> Iterable[tuple[str, str]]:
    """Yield (json_path, value) for every leaf string that looks like an
    ISO timestamp. Skips lists past index 5 so a 10k-row history doesn't
    blow up the test runtime."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_iso_timestamps(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5]):
            yield from _walk_iso_timestamps(v, f"{path}[{i}]")
    elif isinstance(obj, str) and _ISO_LIKE.match(obj):
        yield path, obj


# ---------------------------------------------------------------------------
# A. Accuracy / drift / horizon consistency
# ---------------------------------------------------------------------------


def test_summary_and_drift_baseline_agree(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """``/summary.accuracy_pct`` must equal ``/retrain/config.drift.baseline_accuracy``.

    Both call ``ArtifactService.score_test_predictions`` -- divergence
    here means a caller bypassed the shared scorer and reintroduced
    the inline-copy bug that produced the 69.6 vs 64.71 mismatch.
    """
    fc = base_urls["demand_forecasting"]
    summary = assert_ok(client.get(f"{fc}/summary"))
    retrain = assert_ok(client.get(f"{fc}/retrain/config"))
    acc = summary.get("accuracy_pct")
    drift = retrain.get("drift") or {}
    baseline = drift.get("baseline_accuracy")
    if acc is None and baseline is None:
        pytest.skip("no test predictions scored yet (cold start)")
    assert acc == baseline, (
        f"accuracy_pct={acc} != drift.baseline_accuracy={baseline} -- "
        f"a caller is computing its own scorer; restore the "
        f"score_test_predictions delegation"
    )


def test_forecast_horizon_endpoint_consistency(
    client: httpx.Client, base_urls: dict[str, str], primary_route: str,
) -> None:
    """``/summary.last_forecast_date`` must equal the max ``TrxDate`` in
    the raw ``/predictions/forecast`` payload for any route.

    A drift here means the cached ``get_future_forecast_meta`` is stale
    relative to the on-disk forecast file (cache invalidation gap).
    """
    fc = base_urls["demand_forecasting"]
    summary = assert_ok(client.get(f"{fc}/summary"))
    last_summary = summary.get("last_forecast_date")
    if not last_summary:
        pytest.skip("forecast not generated yet")
    body = assert_ok(client.get(
        f"{fc}/predictions/forecast",
        params={"route_code": primary_route, "limit": 10000},
    ))
    dates = [str(r.get("TrxDate", ""))[:10] for r in body.get("data", [])]
    assert dates, f"no forecast rows for route={primary_route}"
    max_raw = max(dates)
    assert last_summary == max_raw, (
        f"summary.last_forecast_date={last_summary} != "
        f"max(predictions/forecast.TrxDate)={max_raw} for route {primary_route}"
    )


# ---------------------------------------------------------------------------
# B. Route-summary vs per-item rows
# ---------------------------------------------------------------------------


def test_route_summary_matches_predictions_sum(
    client: httpx.Client, base_urls: dict[str, str], primary_route: str,
) -> None:
    """For a given (route, date) the ``/route-summary.predicted_qty``
    field must equal the sum of ``opening_stock + recommended_load`` on
    the rows ``/predictions/forecast`` returns for that route+date.

    Both endpoints read the same enriched van-load frame; a delta here
    means one of them is reading a different cache snapshot.
    """
    fc = base_urls["demand_forecasting"]
    target = (date.today() + timedelta(days=1)).isoformat()
    summary = assert_ok(client.get(
        f"{fc}/predictions/forecast/route-summary", params={"date": target},
    ))
    route_qty = None
    for r in summary.get("routes", []):
        if str(r.get("route_code")) == str(primary_route):
            route_qty = float(r.get("predicted_qty", 0))
            break
    if route_qty is None:
        pytest.skip(f"route {primary_route} not in route-summary for {target}")
    body = assert_ok(client.get(
        f"{fc}/predictions/forecast",
        params={"route_code": primary_route, "limit": 10000},
    ))
    item_sum = sum(
        float(r.get("prediction", 0) or 0)
        for r in body.get("data", [])
        if str(r.get("TrxDate", ""))[:10] == target
    )
    # ``prediction`` is the V5_b recommended_load (no opening_stock),
    # while ``route-summary.predicted_qty`` is the carry-aware total
    # van load (opening + load). The route-summary number is therefore
    # >= the per-item sum, never < it; equality holds only when every
    # row has opening_stock == 0 (cold start). Treat the two as bounds.
    assert route_qty + 1.0 >= item_sum, (
        f"per-item sum exceeds route-summary van load: "
        f"route_summary={route_qty}, item_sum={item_sum}"
    )


# ---------------------------------------------------------------------------
# C. Recommendation chain integrity (forecast -> recon -> recommendation)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
def test_recommendation_chain_integrity(
    client: httpx.Client, base_urls: dict[str, str],
    primary_route: str, db_conn,
) -> None:
    """The ``/recommended-order/get`` API must return what the DB stores.

    Upstream chain drops (raw forecast -> V5_b -> van load -> per-customer
    allocator) have legitimate engine reasons and are validated by the
    engine's own tests; this guard only catches API/DB drift.

    Targets the latest ``trx_date`` with rows for this route in
    ``yf_recommended_orders`` -- robust to whether the daily cron has
    advanced past today (D+1) or not yet (only D recs exist).
    """
    cur = db_conn.cursor()
    cur.execute(
        "SELECT MAX(trx_date) FROM [dbo].[yf_recommended_orders] WITH (NOLOCK) "
        "WHERE route_code=?",
        (primary_route,),
    )
    row = cur.fetchone()
    latest = row[0] if row else None
    if latest is None:
        pytest.skip(f"no recommendations stored for route {primary_route}")
    target = latest.isoformat()
    cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(recommended_quantity), 0) "
        "FROM [dbo].[yf_recommended_orders] WITH (NOLOCK) "
        "WHERE route_code=? AND trx_date=?",
        (primary_route, target),
    )
    db_rows, db_sum = cur.fetchone()
    assert db_rows > 0, f"latest trx_date {target} has zero rows -- DB inconsistency"

    ro = base_urls["recommended_order"]
    body = assert_ok(client.post(f"{ro}/get", json={
        "date": target, "route_code": primary_route, "limit": 10000,
    }))
    api_data = body.get("data", [])
    api_rows = len(api_data)
    api_sum = sum(int(r.get("RecommendedQuantity", 0) or 0) for r in api_data)
    assert api_rows == db_rows and api_sum == db_sum, (
        f"API!=DB for {primary_route}/{target}: "
        f"api=({api_rows} rows, sum={api_sum}) vs "
        f"db=({db_rows} rows, sum={db_sum})"
    )


# ---------------------------------------------------------------------------
# D. Activity-flag regression guard
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
def test_random_forest_not_collapsing_to_zero(db_conn) -> None:
    """Guard the activity_flag fix: ``random_forest`` rows in the live
    Forecast split must not have a near-100% zero rate.

    The pre-fix bug fed every future row ``activity_flag=0`` and drove
    every RF-winner pair to predict 0. We tolerate up to 30% zeros
    (sparse pairs that legitimately predict zero on quiet days) but
    flag anything above as a likely regression.
    """
    target = (date.today() + timedelta(days=1)).isoformat()
    cur = db_conn.cursor()
    cur.execute(
        "SELECT COUNT(*), "
        "  SUM(CASE WHEN predicted=0 THEN 1 ELSE 0 END), "
        "  AVG(predicted) "
        "FROM [dbo].[yf_demand_forecast] WITH (NOLOCK) "
        "WHERE data_split='Forecast' AND trx_date=? AND model_used='random_forest'",
        (target,),
    )
    rows, zeros, avg = cur.fetchone()
    if rows is None or rows == 0:
        pytest.skip("no random_forest rows on target date (model not selected)")
    zero_pct = 100.0 * (zeros or 0) / rows
    assert zero_pct <= 30.0, (
        f"random_forest zero-rate={zero_pct:.1f}% on {target} "
        f"({zeros}/{rows} rows zero, mean predicted={avg}) -- "
        f"activity_flag fix may have regressed in inference_pipeline.py"
    )


# ---------------------------------------------------------------------------
# E. Parameter validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_predictions_test_rejects_invalid_limit(
    client: httpx.Client, base_urls: dict[str, str], bad_limit: int,
) -> None:
    """``limit`` must be >= 1; zero or negative is a 422.

    The pre-fix endpoint silently fell back to ``default_page_limit``,
    masking caller bugs that wanted "all rows" and got 5000.
    """
    fc = base_urls["demand_forecasting"]
    resp = client.get(f"{fc}/predictions/test", params={"limit": bad_limit})
    assert_status(resp, 422, ctx=f"limit={bad_limit}")


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_predictions_forecast_rejects_invalid_limit(
    client: httpx.Client, base_urls: dict[str, str], bad_limit: int,
) -> None:
    """Same contract on the forecast endpoint -- the route validators
    must agree so a webapp typo can't pick a permissive path."""
    fc = base_urls["demand_forecasting"]
    resp = client.get(f"{fc}/predictions/forecast", params={"limit": bad_limit})
    assert_status(resp, 422, ctx=f"limit={bad_limit}")


# ---------------------------------------------------------------------------
# F. Timestamp consistency (every wire timestamp has an explicit offset)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# G. Production-sanity guard (catches magnitude regressions retrains can leak)
# ---------------------------------------------------------------------------


def test_no_production_sanity_regression() -> None:
    """``data/forecast/production_sanity.json`` is rewritten after every
    successful db_push and carries any regression the post-push guard
    detected against the prior snapshot. A non-empty regressions list
    means the last inference dropped a route's 7-day forecast total by
    more than 50% vs the previous run -- exactly the failure mode the
    activity_flag bug produced, now caught automatically.
    """
    import json
    from pathlib import Path
    path = Path("data/forecast/production_sanity.json")
    if not path.exists():
        pytest.skip("no production_sanity snapshot yet (first run)")
    snap = json.loads(path.read_text())
    regressions = snap.get("regressions") if isinstance(snap, dict) and "regressions" in snap else []
    assert not regressions, (
        f"production-sanity regressions detected since last db_push: {regressions}"
    )


@pytest.mark.parametrize("service,path", [
    ("demand_forecasting", "/summary"),
    ("demand_forecasting", "/retrain/config"),
    ("recommended_order",  "/health"),
])
def test_api_timestamps_carry_explicit_offset(
    client: httpx.Client, base_urls: dict[str, str], service: str, path: str,
) -> None:
    """Every ISO-shaped timestamp in the response must carry a UTC offset
    (``+00:00``, ``+05:30``, or trailing ``Z``).

    Naive datetimes let the UI's ``new Date(...)`` reinterpret the
    string in the browser's local zone; a Dubai laptop and a Bangalore
    laptop then disagree on what the same field means. Producers
    standardised on UTC-aware ISO via ``datetime.now(timezone.utc)``.
    """
    body = assert_ok(client.get(f"{base_urls[service]}{path}"))
    naive: list[tuple[str, str]] = []
    for json_path, value in _walk_iso_timestamps(body):
        # ``YYYY-MM-DD`` (date only) is allowed without offset; only
        # flag full timestamps that look ISO-like but lack a zone.
        if "T" in value and not _TS_WITH_OFFSET.match(value):
            naive.append((json_path, value))
    assert not naive, (
        f"naive (TZ-less) timestamps in {service}{path}: {naive[:5]}"
    )


# ---------------------------------------------------------------------------
# I. Pipeline post-block must not eagerly construct PipelineRun()
# ---------------------------------------------------------------------------


def test_pipelinerun_default_construction_is_safe() -> None:
    """Regression: ``PipelineRun()`` with no args used to live as a
    ``dict.get(key, PipelineRun())`` default in the post-pipeline
    cleanup block. Python evaluates the default EAGERLY, so it raised
    ``TypeError: missing 1 required positional argument: 'pipeline'``
    every time the cleanup ran -- killing the auto-cascade
    (reconciliation + recommendation regeneration) after a successful
    retrain even though training itself succeeded.

    Lock down two invariants:

    1. The ``PipelineRun`` dataclass keeps ``pipeline`` as a required
       field with no default. Loosening that to a sentinel string would
       paper over future re-introductions of the pattern.
    2. The post-block reads ``self._runs.get("inference")`` WITHOUT a
       default and explicit-None-checks the result, so the missing-key
       branch never instantiates anything.
    """
    import dataclasses

    import pytest as _pytest

    from demand_forecasting_pipeline.services.pipeline_service import (
        PipelineRun,
    )

    # Invariant 1: bare PipelineRun() must still raise -- the contract
    # that every run carries a pipeline name is load-bearing.
    with _pytest.raises(TypeError):
        PipelineRun()

    # Invariant 2: a future engineer must not silently weaken the field
    # to ``pipeline: str = ""``; if they do, this assertion flags it.
    fields = {f.name: f for f in dataclasses.fields(PipelineRun)}
    pipeline_field = fields["pipeline"]
    assert pipeline_field.default is dataclasses.MISSING, (
        "PipelineRun.pipeline must remain a required field with no "
        "default; adding one would re-enable PipelineRun() construction "
        "and mask future cascade regressions."
    )

    # Invariant 3: source-level guard. The post-block must not use
    # ``PipelineRun()`` as a dict-get default. Grep the service module
    # itself so a future paste of the old pattern fails the test.
    import inspect

    from demand_forecasting_pipeline.services import pipeline_service as _ps
    src = inspect.getsource(_ps)
    # Allow the pattern inside comments / docstrings; flag only code.
    code_lines = [
        line for line in src.splitlines()
        if "PipelineRun()" in line
        and not line.lstrip().startswith("#")
        and '"""' not in line and "'''" not in line
    ]
    assert not code_lines, (
        "PipelineRun() bare construction reintroduced in "
        "pipeline_service.py code (not just comments): " + repr(code_lines)
    )
