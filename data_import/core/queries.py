"""
Dynamic SQL query builder -- parameterised, no hardcoded routes or dates.
Supports both full-load and incremental-load modes.
"""

from __future__ import annotations

from typing import List, Optional

from data_import.config.settings import Settings, get_settings


class QueryBuilder:
    """Builds SQL for customer data, journey plan, and sales recent."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()

    def _route_ph(self, routes: List[str]) -> str:
        return ",".join("?" for _ in routes)

    # ------------------------------------------------------------------
    # Customer data (incremental by TrxDate)
    # ------------------------------------------------------------------

    def customer_data(
        self,
        routes: Optional[List[str]] = None,
        since_date: Optional[str] = None,
        lookback_days: Optional[int] = None,
    ) -> tuple[str, list]:
        """
        If since_date is provided: fetch only rows where TrxDate > since_date (incremental).
        Otherwise: fetch last lookback_days (full refresh).
        """
        routes = routes or self._s.route_codes
        ph = self._route_ph(routes)

        sql = f"""
            SELECT
                s.TrxDate,
                s.RouteCode,
                s.CustomerCode,
                s.CustomerName,
                s.ItemCode,
                s.ItemName,
                s.CategoryCode,
                s.CategoryName,
                SUM(CASE WHEN s.QuantityInPCs > 0 THEN s.QuantityInPCs ELSE 0 END) AS TotalQuantity,
                ROUND(AVG(s.UnitPrice), 2) AS AvgUnitPrice
            FROM {self._s.sales_view} s WITH (NOLOCK)
            WHERE s.ItemType  = 'OrderItem'
              AND s.TrxType   = 'SalesInvoice'
              AND s.RouteCode IN ({ph})
        """
        params: list = list(routes)

        if since_date:
            sql += "  AND s.TrxDate > ?\n"
            params.append(since_date)
        else:
            days = lookback_days or self._s.customer_data_lookback_days
            sql += "  AND s.TrxDate >= DATEADD(day, -?, GETDATE())\n"
            params.append(days)

        sql += """
            GROUP BY
                s.TrxDate, s.RouteCode,
                s.CustomerCode, s.CustomerName,
                s.ItemCode, s.ItemName,
                s.CategoryCode, s.CategoryName
        """
        return sql, params

    # ------------------------------------------------------------------
    # Journey plan (incremental by JourneyDate)
    # ------------------------------------------------------------------

    def journey_plan(
        self,
        routes: Optional[List[str]] = None,
        since_date: Optional[str] = None,
        window_days: Optional[int] = None,
    ) -> tuple[str, list]:
        routes = routes or self._s.route_codes
        ph = self._route_ph(routes)

        sql = f"""
            SELECT *
            FROM {self._s.journey_view} WITH (NOLOCK)
            WHERE RouteCode IN ({ph})
        """
        params: list = list(routes)

        if since_date:
            sql += "  AND JourneyDate > ?\n"
            params.append(since_date)
        else:
            window = window_days or self._s.journey_plan_window_days
            sql += "  AND JourneyDate >= DATEADD(day, -?, GETDATE())\n"
            sql += "  AND JourneyDate <= DATEADD(day,  ?, GETDATE())\n"
            params.extend([window, window])

        return sql, params

    # ------------------------------------------------------------------
    # Demand forecast output (from AIML pipeline; incremental by trx_date)
    # ------------------------------------------------------------------

    def demand_forecast(
        self,
        routes: Optional[List[str]] = None,
        since_date: Optional[str] = None,
        lookback_days: Optional[int] = None,
    ) -> tuple[str, list]:
        routes = routes or self._s.route_codes
        ph = self._route_ph(routes)

        sql = f"""
            SELECT
                trx_date        AS TrxDate,
                route_code      AS RouteCode,
                item_code       AS ItemCode,
                item_name       AS ItemName,
                data_split      AS DataSplit,
                demand_class    AS DemandClass,
                model_used      AS ModelUsed,
                predicted       AS Predicted,
                p_demand        AS DemandProbability,
                qty_if_demand   AS QtyIfDemand,
                actual_qty      AS ActualQty,
                lower_bound     AS LowerBound,
                upper_bound     AS UpperBound,
                adi             AS Adi,
                cv2             AS Cv2,
                nonzero_ratio   AS NonzeroRatio,
                mean_qty        AS MeanQty,
                avg_gap_days    AS AvgGapDays
            FROM {self._s.demand_forecast_table} WITH (NOLOCK)
            WHERE route_code IN ({ph})
        """
        params: list = list(routes)

        if since_date:
            sql += "  AND trx_date > ?\n"
            params.append(since_date)
        else:
            days = lookback_days or self._s.sales_recent_lookback_days
            sql += "  AND trx_date >= DATEADD(day, -?, GETDATE())\n"
            params.append(days)

        return sql, params

    # ------------------------------------------------------------------
    # Sales recent (incremental by TrxDate, for demand forecasting input)
    # ------------------------------------------------------------------

    def sales_recent(
        self,
        routes: Optional[List[str]] = None,
        since_date: Optional[str] = None,
        lookback_days: Optional[int] = None,
    ) -> tuple[str, list]:
        routes = routes or self._s.route_codes
        ph = self._route_ph(routes)

        sql = f"""
            SELECT
                s.TrxDate,
                s.WarehouseCode,
                s.WarehouseName,
                s.RouteCode,
                s.ItemCode,
                s.ItemName,
                s.CategoryName,
                SUM(CASE WHEN s.QuantityInPCs > 0 THEN s.QuantityInPCs ELSE 0 END) AS TotalQuantity,
                ROUND(AVG(s.UnitPrice), 2) AS AvgUnitPrice
            FROM {self._s.sales_view} s WITH (NOLOCK)
            WHERE s.ItemType  = 'OrderItem'
              AND s.TrxType   = 'SalesInvoice'
              AND s.RouteCode IN ({ph})
        """
        params: list = list(routes)

        if since_date:
            sql += "  AND s.TrxDate > ?\n"
            params.append(since_date)
        else:
            days = lookback_days or self._s.sales_recent_lookback_days
            sql += "  AND s.TrxDate >= DATEADD(day, -?, GETDATE())\n"
            params.append(days)

        sql += """
            GROUP BY
                s.TrxDate, s.WarehouseCode, s.WarehouseName,
                s.RouteCode, s.ItemCode, s.ItemName, s.CategoryName
        """
        return sql, params
