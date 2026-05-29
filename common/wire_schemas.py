"""Canonical wire-schema rename maps (PascalCase <-> snake_case).

Single source of truth for the column-name conventions that cross service
boundaries via CSV mirrors. Producers emit PascalCase (matches the DB row
shape); consumers translate to snake_case via these maps. Keeping the maps
here in ``common/`` avoids cross-service imports (``common/`` reaching back
into a service module was a latent circular-dep risk).
"""

from __future__ import annotations

# yf_sales_transactions CSV mirror schema. The reconciliation engine emits
# PascalCase rows via the data_import cascade; everyone downstream
# (recommended_order, supervision, common.carry_lookup) renames to snake_case
# via this map. Producer-side SQL aliases validated against it at import.
SALES_TRANSACTIONS_RENAME: dict[str, str] = {
    "TrxDate":                "trx_date",
    "RouteCode":              "route_code",
    "ItemCode":               "item_code",
    "OpeningStock":           "opening_stock",
    "FreshLoad":              "fresh_load",
    "TotalVanLoad":           "total_van_load",
    "LeftoverToNextDay":      "leftover_to_next_day",
    "ActualSold":             "actual_sold",
    "BiasPct":                "bias_pct",
    "ForecastCorrected":      "forecast_corrected",
    "ExpectedDemand":         "expected_demand",
    "VanLoadLowerBound":      "van_load_lower_bound",
    "VanLoadUpperBound":      "van_load_upper_bound",
    "RecentDailyAvg":         "recent_daily_avg",
    "PatternFloorApplied":    "pattern_floor_applied",
    "PatternCeilingApplied":  "pattern_ceiling_applied",
    "ForecastBelowRecent":    "forecast_below_recent",
    "ForecastDormant":        "forecast_dormant",
    "YaumiOpeningStock":      "yaumi_opening_stock",
    "YaumiFreshLoad":         "yaumi_fresh_load",
    "YaumiTotalVanLoad":      "yaumi_total_van_load",
    "YaumiLeftover":          "yaumi_leftover",
}
