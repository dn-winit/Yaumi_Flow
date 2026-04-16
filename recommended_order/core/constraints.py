"""
Van load constraint allocator -- priority-first allocation.
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def apply_van_load_constraints(df: pd.DataFrame) -> pd.DataFrame:
    """
    Allocate van load to recommendations by priority (highest first).
    When an item's total recommended qty exceeds its van load,
    high-priority customers get full qty first; lower-priority get remainder.
    """
    if df.empty:
        return df

    df = df.copy()
    df["RecommendedQuantity"] = pd.to_numeric(df["RecommendedQuantity"], errors="coerce").fillna(0)
    df["VanLoad"] = pd.to_numeric(df["VanLoad"], errors="coerce").fillna(0)
    df["PriorityScore"] = pd.to_numeric(df["PriorityScore"], errors="coerce").fillna(0)

    result_rows: list[dict] = []

    for item_code, item_data in df.groupby("ItemCode", sort=False):
        van_load = int(item_data["VanLoad"].iloc[0])
        total_req = item_data["RecommendedQuantity"].sum()

        if total_req <= van_load:
            result_rows.extend(item_data.to_dict("records"))
            continue

        # Allocate by descending priority
        sorted_data = item_data.sort_values("PriorityScore", ascending=False)
        remaining = van_load

        for _, row in sorted_data.iterrows():
            rec = row.to_dict()
            deserved = int(rec["RecommendedQuantity"])

            if deserved <= remaining:
                result_rows.append(rec)
                remaining -= deserved
            elif remaining >= 1:
                rec["RecommendedQuantity"] = remaining
                suffix = f" [Van constrained: {remaining}/{deserved}]"
                if "WhyQuantity" in rec and isinstance(rec["WhyQuantity"], str):
                    rec["WhyQuantity"] = (rec["WhyQuantity"] or "") + suffix
                result_rows.append(rec)
                remaining = 0
            # else: nothing left -- this customer gets nothing

    if not result_rows:
        return pd.DataFrame()

    out = pd.DataFrame(result_rows)
    out["RecommendedQuantity"] = pd.to_numeric(out["RecommendedQuantity"], errors="coerce").fillna(0)
    return out[out["RecommendedQuantity"] > 0].reset_index(drop=True)
