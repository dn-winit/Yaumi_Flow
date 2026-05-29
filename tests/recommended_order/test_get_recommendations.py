"""POST /get -- the consumer-facing retrieval endpoint.

Used by the webapp recommendation tile + by sales_supervision's
RecommendedOrderClient on every 60s reconcile tick. Must:
  * return 2xx + structured payload on a configured route
  * not 500 on unknown route (return empty data instead)
  * respect ``limit`` parameter
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_not_5xx, assert_ok


class TestGet:
    """``POST /get`` -- date is required; route_code/customer_code/item_code
    are optional filters."""

    def test_happy_one_route(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        resp = client.post(
            f"{base_urls['recommended_order']}/get",
            json={"date": today, "route_code": primary_route, "limit": 100},
        )
        body = assert_ok(resp, f"{primary_route}/{today}")
        # The supervision client reads ``success`` + ``data``;
        # contract: success boolean + data list.
        assert "success" in body, f"missing 'success' in {body}"
        assert isinstance(body.get("data"), list), (
            f"'data' is not a list: {type(body.get('data')).__name__}"
        )

    @pytest.mark.parametrize("route_code", ["0000", "9999"])
    def test_unknown_route(
        self, client: httpx.Client, base_urls: dict[str, str],
        today: str, route_code: str,
    ) -> None:
        resp = client.post(
            f"{base_urls['recommended_order']}/get",
            json={"date": today, "route_code": route_code, "limit": 10},
        )
        assert_not_5xx(resp, f"unknown route {route_code}")

    def test_far_past_date(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, far_past: str,
    ) -> None:
        resp = client.post(
            f"{base_urls['recommended_order']}/get",
            json={"date": far_past, "route_code": primary_route, "limit": 10},
        )
        # Far-past predates the data; route should auto-heal carry chain
        # OR return empty. Either way: not a 5xx.
        assert_not_5xx(resp, f"far-past {far_past}")

    def test_pre_system_date_returns_structured_200(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, pre_system_date: str,
    ) -> None:
        """Regression: year-2020 input used to raise an uncaught
        ``HTTPStatusError`` from the auto-heal path (horizon exceeds
        demand_forecasting's 365-day cap) -> bare 500. Now returns a
        structured 200 + ``diagnosis`` envelope so the UI can render
        a positive "no data this far back" message instead of an
        error toast."""
        resp = client.post(
            f"{base_urls['recommended_order']}/get",
            json={
                "date": pre_system_date,
                "route_code": primary_route,
                "limit": 10,
            },
        )
        body = assert_ok(resp, f"pre-system date {pre_system_date}")
        assert body.get("success") is True, (
            f"expected success=True with empty data, got {body}"
        )
        assert body.get("total") == 0, (
            f"pre-system date should have zero results: {body}"
        )
        assert body.get("data") == [], (
            f"pre-system date should have empty data list: {body}"
        )
        # ``diagnosis`` carries the user-facing explanation; we don't
        # assert specific text, just that the envelope is present.
        assert body.get("diagnosis"), (
            f"missing diagnosis envelope for pre-system date: {body}"
        )

    def test_bad_date_format_rejected(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str,
    ) -> None:
        """Malformed date must be rejected with a 4xx (Pydantic
        validation), never accepted and crash later."""
        resp = client.post(
            f"{base_urls['recommended_order']}/get",
            json={"date": "not-a-date", "route_code": primary_route},
        )
        assert 400 <= resp.status_code < 500, (
            f"bad date should be 4xx, got {resp.status_code}"
        )

    def test_all_routes_no_500(
        self, client: httpx.Client, base_urls: dict[str, str],
        routes: list[str], today: str,
    ) -> None:
        """Cross-fleet sweep on the supervision hot path."""
        for r in routes:
            resp = client.post(
                f"{base_urls['recommended_order']}/get",
                json={"date": today, "route_code": r, "limit": 50},
            )
            assert_not_5xx(resp, f"route={r}")


@pytest.mark.slow
def test_generate_force_one_route(
    client: httpx.Client, base_urls: dict[str, str],
    primary_route: str, today: str,
) -> None:
    """``POST /generate?force=true`` for one route. Slow because it
    re-runs the engine and writes to DB."""
    resp = client.post(
        f"{base_urls['recommended_order']}/generate",
        json={
            "date": today,
            "route_codes": [primary_route],
            "force": True,
        },
        timeout=300.0,
    )
    body = assert_ok(resp, f"generate {primary_route}/{today}")
    assert body.get("success") in (True, None) or "routes_processed" in body, (
        f"generate result lacks success/routes_processed: {body}"
    )


def test_tier2_fallback_seeds_opening_from_yesterday_leftover(tmp_path) -> None:
    """Regression: when ``sales_transactions.csv`` has no row for the
    target date (gap between midnight and the 03:00 Dubai cron, or
    immediately after a manual retrain that didn't refresh today), the
    Tier-2 fallback in ``DataManager.get_van_items`` must seed today's
    ``opening_stock`` from yesterday's ``leftover_to_next_day``. The
    old behaviour silently anchored at zero, dropping the carry chain.

    In-process, no DB/HTTP -- runs in every CI build.
    """
    from unittest.mock import patch

    import pandas as pd

    from recommended_order.config.settings import Settings
    from recommended_order.data.manager import DataManager

    # Tmp shared_data_dir with a sales_transactions.csv that has
    # YESTERDAY's row carrying a non-zero leftover, but no row for
    # TODAY. The Tier-2 path must pick up the leftover.
    shared = tmp_path
    shared.mkdir(parents=True, exist_ok=True)

    today = pd.Timestamp("2026-05-26")
    yesterday = today - pd.Timedelta(days=1)

    # Yesterday's row: fresh_load=10 + opening=20 = van_qty 30, of
    # which 25 sold -> leftover_to_next_day=5. Today must therefore
    # open at 5, not 0.
    sx = pd.DataFrame([{
        "TrxDate": yesterday.strftime("%Y-%m-%d"),
        "RouteCode": "9105",
        "ItemCode": "A",
        "FreshLoad": 10.0,
        "OpeningStock": 20.0,
        "LeftoverToNextDay": 5.0,
    }])
    sx.to_csv(shared / "sales_transactions.csv", index=False)

    s = Settings(shared_data_dir=str(shared))
    dm = DataManager(s)
    # Bust the class-level sales_transactions index cache so this test's
    # tmp CSV is what the lookup sees (other tests may have populated
    # the cache from a different path).
    DataManager._SX_INDEX = None
    DataManager._SX_MTIME = 0
    DataManager._SX_SIZE = 0

    # Stub the demand frame so today has a row for (9105, A) with
    # zero raw prediction. With opening=0 today's recommended van qty
    # would be 0; with the correct carry seed of 5 it must be ceil(0+5)=5.
    demand = pd.DataFrame([{
        "TrxDate": today,
        "RouteCode": "9105",
        "ItemCode": "A",
        "ItemName": "Test Item",
        "Predicted": 0.0,
        "DemandProbability": 1.0,
        "DataSplit": "Forecast",
    }])
    with patch.object(dm, "get_demand_data", return_value=demand):
        result = dm.get_van_items("9105", today)

    assert result.get("A") == 5, (
        f"Tier-2 carry seed missing: expected today's van qty=5 "
        f"(0 predicted + 5 leftover from yesterday), got {result.get('A')}. "
        f"Full result: {result}"
    )


def test_tier2_carry_seed_walks_back_across_non_trip_days(tmp_path) -> None:
    """Regression: when the prior calendar day is a non-trip day
    (weekend / public holiday) so ``sales_transactions.csv`` has no
    row for it, the Tier-2 carry seed must walk BACK until it finds
    the most-recent reconciled row. Naive ``target - 1d`` lookup would
    silently drop the carry chain across the gap.

    Scenario: Thursday has leftover 8 units. Friday is non-trip (no
    row). Today is Saturday. Saturday's opening must be 8 (Thursday's
    leftover passed through), NOT 0.

    Mirrors the engine's own multi-date sim semantic (enrich.py:652-659
    iterates over valid_dates, so sim_leftover persists across gaps).
    """
    from unittest.mock import patch

    import pandas as pd

    from recommended_order.config.settings import Settings
    from recommended_order.data.manager import DataManager

    shared = tmp_path
    saturday = pd.Timestamp("2026-05-30")  # arbitrary Saturday
    thursday = saturday - pd.Timedelta(days=2)
    # Note: NO Friday row. Reconciliation skipped it (non-trip).
    sx = pd.DataFrame([{
        "TrxDate": thursday.strftime("%Y-%m-%d"),
        "RouteCode": "9105", "ItemCode": "A",
        "FreshLoad": 12.0, "OpeningStock": 0.0,
        "LeftoverToNextDay": 8.0,
    }])
    sx.to_csv(shared / "sales_transactions.csv", index=False)

    s = Settings(shared_data_dir=str(shared))
    dm = DataManager(s)
    DataManager._SX_INDEX = None
    DataManager._SX_MTIME = 0
    DataManager._SX_SIZE = 0

    demand = pd.DataFrame([{
        "TrxDate": saturday, "RouteCode": "9105", "ItemCode": "A",
        "ItemName": "X", "Predicted": 0.0, "DemandProbability": 1.0,
        "DataSplit": "Forecast",
    }])
    demand["TrxDate"] = pd.to_datetime(demand["TrxDate"])

    with patch.object(dm, "get_demand_data", return_value=demand):
        result = dm.get_van_items("9105", saturday)

    assert result.get("A") == 8, (
        f"Tier-2 walk-back failed across non-trip Friday: expected "
        f"Saturday open=8 (Thursday's leftover), got {result.get('A')}. "
        f"Without walk-back, the naive target-1d lookup finds no Friday "
        f"row and the carry chain breaks."
    )


def test_tier2_walk_back_respects_configurable_lookback_limit(tmp_path) -> None:
    """The walk-back must honour ``carry_chain_lookback_days`` (default 14,
    overridable for ops with unusual holiday gaps). Setting it to 1 must
    revert to literal yesterday-only lookup. Setting to 0 must disable
    the carry seed entirely.
    """
    from unittest.mock import patch

    import pandas as pd

    from recommended_order.config.settings import Settings
    from recommended_order.data.manager import DataManager

    shared = tmp_path
    target = pd.Timestamp("2026-05-30")
    three_days_back = target - pd.Timedelta(days=3)
    sx = pd.DataFrame([{
        "TrxDate": three_days_back.strftime("%Y-%m-%d"),
        "RouteCode": "9105", "ItemCode": "A",
        "FreshLoad": 0.0, "OpeningStock": 0.0,
        "LeftoverToNextDay": 99.0,
    }])
    sx.to_csv(shared / "sales_transactions.csv", index=False)
    demand = pd.DataFrame([{
        "TrxDate": target, "RouteCode": "9105", "ItemCode": "A",
        "ItemName": "X", "Predicted": 0.0, "DemandProbability": 1.0,
        "DataSplit": "Forecast",
    }])
    demand["TrxDate"] = pd.to_datetime(demand["TrxDate"])

    # lookback=2 means walk-back covers target-1 and target-2, NOT
    # target-3. Row is 3 days back -> not found -> carry seed = 0.
    s_short = Settings(shared_data_dir=str(shared), carry_chain_lookback_days=2)
    dm = DataManager(s_short)
    DataManager._SX_INDEX = None; DataManager._SX_MTIME = 0; DataManager._SX_SIZE = 0
    with patch.object(dm, "get_demand_data", return_value=demand):
        result = dm.get_van_items("9105", target)
    assert result == {}, (
        f"lookback_days=2 must NOT find a row 3 days back; got {result}"
    )

    # lookback=14 (default) catches the 3-day-back row -> carry seed=99.
    s_default = Settings(shared_data_dir=str(shared))
    dm = DataManager(s_default)
    DataManager._SX_INDEX = None; DataManager._SX_MTIME = 0; DataManager._SX_SIZE = 0
    with patch.object(dm, "get_demand_data", return_value=demand):
        result = dm.get_van_items("9105", target)
    assert result.get("A") == 99, (
        f"default lookback_days=14 must find a row 3 days back; got {result}"
    )


def test_tier2_carry_seed_honours_explicit_zero_leftover(tmp_path) -> None:
    """Regression: when the most recent reconciled row says leftover=0
    (rep sold everything), the carry seed MUST be 0 -- NOT walk further
    back to find some older non-zero leftover. Reality (rep's reported
    zero) always beats stale older data. If the walk-back ever started
    skipping zeros to find non-zero values, it would silently override
    yesterday's "van is empty" with last week's "van had 50 cases" --
    a phantom-stock bug where we recommend less fresh allocation than
    needed.
    """
    from unittest.mock import patch

    import pandas as pd

    from recommended_order.config.settings import Settings
    from recommended_order.data.manager import DataManager

    shared = tmp_path
    target = pd.Timestamp("2026-05-30")
    yesterday = target - pd.Timedelta(days=1)
    week_ago = target - pd.Timedelta(days=7)

    # Yesterday: leftover=0 explicitly (rep emptied the van).
    # Week ago: leftover=50 (old large carry).
    # Walk-back MUST return 0 (yesterday's reality), not 50.
    sx = pd.DataFrame([
        {"TrxDate": week_ago.strftime("%Y-%m-%d"),
         "RouteCode": "9105", "ItemCode": "A",
         "FreshLoad": 60.0, "OpeningStock": 0.0, "LeftoverToNextDay": 50.0},
        {"TrxDate": yesterday.strftime("%Y-%m-%d"),
         "RouteCode": "9105", "ItemCode": "A",
         "FreshLoad": 10.0, "OpeningStock": 50.0, "LeftoverToNextDay": 0.0},
    ])
    sx.to_csv(shared / "sales_transactions.csv", index=False)

    s = Settings(shared_data_dir=str(shared))
    dm = DataManager(s)
    DataManager._SX_INDEX = None; DataManager._SX_MTIME = 0; DataManager._SX_SIZE = 0

    demand = pd.DataFrame([{
        "TrxDate": target, "RouteCode": "9105", "ItemCode": "A",
        "ItemName": "X", "Predicted": 0.0, "DemandProbability": 1.0,
        "DataSplit": "Forecast",
    }])
    demand["TrxDate"] = pd.to_datetime(demand["TrxDate"])

    with patch.object(dm, "get_demand_data", return_value=demand):
        result = dm.get_van_items("9105", target)

    # Predicted=0 + opening=0 (from yesterday's explicit zero leftover)
    # = total 0 -> item correctly absent from the output dict.
    assert result == {} or result.get("A") == 0, (
        f"Walk-back ignored yesterday's explicit zero leftover and "
        f"reached for week-ago's stale 50; got {result}. The 'most "
        f"recent wins' invariant is broken -- this would create "
        f"phantom-stock bugs where the engine under-recommends fresh "
        f"allocation because it thinks the van still has old units."
    )


def test_tier2_multi_item_no_unbound_or_key_leak(tmp_path) -> None:
    """Regression for two related bugs an earlier version of Tier-2 had:

    1. ``UnboundLocalError`` when the first row had a non-zero engine
       opening_stock -- ``item_key`` was defined only inside the
       walk-back conditional but used unconditionally on
       ``out[item_key] = ...``.

    2. Cross-row key corruption when later rows had non-zero opening
       after earlier rows had zero -- the second row's qty was
       written under the first row's key in ``out``.

    Fix: hoist ``item_key = str(r.ItemCode)`` to the top of the loop.
    This test runs a mixed-opening scenario (one zero, two non-zero
    via reconcile_demand_frame mocking) to lock both holes shut.
    """
    from unittest.mock import patch

    import pandas as pd

    from recommended_order.config.settings import Settings
    from recommended_order.data.manager import DataManager

    shared = tmp_path
    today = pd.Timestamp("2026-05-30")
    yesterday = today - pd.Timedelta(days=1)
    # sales_transactions has yesterday's row for item A (with leftover)
    # but nothing for B and C -- the walk-back fires for A only.
    sx = pd.DataFrame([{
        "TrxDate": yesterday.strftime("%Y-%m-%d"),
        "RouteCode": "9105", "ItemCode": "A",
        "FreshLoad": 5.0, "OpeningStock": 0.0, "LeftoverToNextDay": 3.0,
    }])
    sx.to_csv(shared / "sales_transactions.csv", index=False)

    s = Settings(shared_data_dir=str(shared))
    dm = DataManager(s)
    DataManager._SX_INDEX = None; DataManager._SX_MTIME = 0; DataManager._SX_SIZE = 0

    demand = pd.DataFrame([
        {"TrxDate": today, "RouteCode": "9105", "ItemCode": "B",
         "ItemName": "B", "Predicted": 7.0, "DemandProbability": 1.0, "DataSplit": "Forecast"},
        {"TrxDate": today, "RouteCode": "9105", "ItemCode": "A",
         "ItemName": "A", "Predicted": 4.0, "DemandProbability": 1.0, "DataSplit": "Forecast"},
        {"TrxDate": today, "RouteCode": "9105", "ItemCode": "C",
         "ItemName": "C", "Predicted": 9.0, "DemandProbability": 1.0, "DataSplit": "Forecast"},
    ])
    demand["TrxDate"] = pd.to_datetime(demand["TrxDate"])

    # Force Tier-2 path: stub reconcile_demand_frame to inject non-zero
    # engine-computed opening_stock on B and C (zero on A so walk-back
    # fires there). This mirrors what the real engine would do for a
    # cold-started future-horizon date where some items inherit
    # leftover from upstream sim while others don't.
    def fake_reconcile(df):
        out_df = df.copy()
        out_df["recommended_load"] = out_df["Predicted"]
        out_df["opening_stock"] = out_df["ItemCode"].map(
            {"A": 0.0, "B": 2.0, "C": 0.0}
        ).astype(float)
        return out_df

    with patch.object(dm, "get_demand_data", return_value=demand), \
         patch.object(dm, "reconcile_demand_frame", side_effect=fake_reconcile):
        result = dm.get_van_items("9105", today)

    # Expected per-item totals (fresh + opening, ceil):
    #   B: fresh=7 + opening=2 (from engine)            = 9
    #   A: fresh=4 + opening=3 (walk-back from yest)    = 7
    #   C: fresh=9 + opening=0 (no carry, no engine)    = 9
    assert result == {"B": 9, "A": 7, "C": 9}, (
        f"Tier-2 multi-item produced wrong dict: {result}. "
        f"Expected each item under its OWN key; if a key is missing "
        f"or a value lands under the wrong key, the item_key hoist "
        f"regressed."
    )


def test_shared_carry_helper_invariants() -> None:
    """Regression for the shared ``common/carry_lookup.py`` used by both
    recommended_order (Tier-2 walk-back) and demand_forecasting
    (van_load_view_enriched carry-seed override). Both consumers MUST
    produce identical carry numbers for the same (route, item, date) --
    so the page tile and the recommendation engine never disagree.

    Locks four invariants:
      1. First row found wins (yesterday's explicit zero beats older
         non-zero -- no phantom-stock).
      2. Walk-back skips non-trip-day gaps (weekend/holiday).
      3. lookback_days=0 disables the seed entirely (legacy behaviour).
      4. Missing pair returns (0, None) without raising.
    """
    import pandas as pd

    from common.carry_lookup import (
        build_yesterday_leftover_map,
        lookup_prior_leftover,
    )

    target = pd.Timestamp("2026-05-30")

    # Inv 1: most-recent wins, even if it's zero.
    sx_idx = {
        ("9105", "A", pd.Timestamp("2026-05-29")): (10.0, 0.0, 0.0),   # explicit 0
        ("9105", "A", pd.Timestamp("2026-05-25")): (10.0, 0.0, 99.0),  # older non-zero
    }
    v, src = lookup_prior_leftover(sx_idx, "9105", "A", target, leftover_pos=2)
    assert v == 0.0 and src == pd.Timestamp("2026-05-29"), (
        f"Most-recent-wins broken: walked past explicit zero to find {v}"
    )

    # Inv 2: non-trip-day skip (gap of 2 days, finds row 3 days back).
    sx_idx = {("9105", "A", pd.Timestamp("2026-05-27")): (10.0, 0.0, 8.0)}
    v, src = lookup_prior_leftover(sx_idx, "9105", "A", target, lookback_days=14, leftover_pos=2)
    assert v == 8.0 and src == pd.Timestamp("2026-05-27"), (
        f"Walk-back across non-trip gap broken; got {v} from {src}"
    )

    # Inv 3: lookback=0 disables.
    v, src = lookup_prior_leftover(sx_idx, "9105", "A", target, lookback_days=0, leftover_pos=2)
    assert v == 0.0 and src is None, (
        f"lookback=0 must disable the seed; got {v} from {src}"
    )

    # Inv 4: missing pair degrades gracefully.
    v, src = lookup_prior_leftover(sx_idx, "9999", "X", target, leftover_pos=2)
    assert v == 0.0 and src is None

    # Batch builder produces the same answer as per-pair calls.
    sx_df = pd.DataFrame([
        {"TrxDate": "2026-05-29", "RouteCode": "9105", "ItemCode": "A",
         "LeftoverToNextDay": 5.0, "YaumiLeftover": 3.0},
        {"TrxDate": "2026-05-27", "RouteCode": "9105", "ItemCode": "B",
         "LeftoverToNextDay": 12.5, "YaumiLeftover": 11.0},
    ])
    m = build_yesterday_leftover_map(sx_df, ["9105"], target, lookback_days=14)
    assert ("9105", "A") in m and ("9105", "B") in m
    eng_a, rep_a, _ = m[("9105", "A")]
    eng_b, rep_b, _ = m[("9105", "B")]
    assert eng_a == 5.0 and rep_a == 3.0, f"Batch map mismatch A: {m[('9105','A')]}"
    assert eng_b == 12.5 and rep_b == 11.0, f"Batch map mismatch B: {m[('9105','B')]}"


def test_db_pusher_emits_serializable_and_lock_hints() -> None:
    """Regression: the bulk recommendation push (DELETE+INSERT) MUST run
    under ``SERIALIZABLE`` with ``HOLDLOCK, UPDLOCK`` on the DELETE so
    the (date, route) range stays locked for the whole transaction.

    Why: without these, a concurrent reader sees an empty window
    between the DELETE commit and the INSERT commit, breaking the
    "one push, one consistent view" contract the other two AIML
    writers (db_pusher in demand_forecasting and
    reconciliation_refresh) already honour. This test pins the
    isolation level and the lock-hint clause so a future settings or
    template edit can't silently weaken the contract.
    """
    from recommended_order.config.settings import DatabaseSettings
    from recommended_order.services.db_pusher import (
        _DELETE_SQL_BASE,
        _ISOLATION_RE,
        _LOCK_HINTS_RE,
    )

    # The DELETE template must contain the hint-clause placeholder so
    # the settings-driven lock hints can be interpolated at runtime.
    assert "{hint_clause}" in _DELETE_SQL_BASE, (
        "_DELETE_SQL_BASE missing {hint_clause} placeholder -- lock "
        "hints can no longer be injected into the DELETE phase."
    )

    # Default settings MUST produce SERIALIZABLE + HOLDLOCK,UPDLOCK so
    # the writer is production-grade out of the box. Ops can drop to
    # READ COMMITTED for debugging by overriding the env vars, but the
    # baseline cannot regress to "no isolation, no hints."
    s = DatabaseSettings()
    assert s.merge_isolation_level == "SERIALIZABLE", (
        f"merge_isolation_level default regressed: {s.merge_isolation_level!r}"
    )
    assert s.merge_target_lock_hints == "HOLDLOCK, UPDLOCK", (
        f"merge_target_lock_hints default regressed: {s.merge_target_lock_hints!r}"
    )

    # Whitelist regexes accept the defaults but reject obvious
    # injection attempts -- the only attack surface here is the
    # interpolated SQL string.
    assert _ISOLATION_RE.match("SERIALIZABLE")
    assert _LOCK_HINTS_RE.match("HOLDLOCK, UPDLOCK")
    assert not _ISOLATION_RE.match("SERIALIZABLE; DROP TABLE x")
    assert not _LOCK_HINTS_RE.match("HOLDLOCK); DROP TABLE x; --")

    # Empty values (ops escape hatch) must round-trip cleanly through
    # the regexes -- they should NOT match (which is how the runtime
    # detects "skip the SET / skip the WITH(...)" without raising).
    assert not _ISOLATION_RE.match("")
    assert not _LOCK_HINTS_RE.match("")

    # End-to-end: the rendered DELETE must include the lock-hint
    # clause exactly once when hints are configured, and be free of
    # any clause when they're not.
    rendered_with = _DELETE_SQL_BASE.format(table="[X]", hint_clause=" WITH (HOLDLOCK, UPDLOCK)")
    assert " WITH (HOLDLOCK, UPDLOCK) WHERE " in rendered_with
    rendered_without = _DELETE_SQL_BASE.format(table="[X]", hint_clause="")
    assert " WITH (" not in rendered_without, (
        "Empty hint_clause must produce a hint-free DELETE for legacy "
        "ops-debug parity."
    )
