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
                CEILING(SUM(CASE WHEN s.QuantityInPCs > 0 THEN s.QuantityInPCs ELSE 0 END)) AS TotalQuantity,
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

        # The four reconciliation columns (recommended_load, forecast_corrected,
        # bias_pct, opening_stock) are guarded with ISNULL so this query keeps
        # working against an older table shape that hasn't had the migration
        # applied yet -- the values fall through to 0 and the API path will
        # recompute them on read. Once the migration lands the real values come
        # straight from the DB and no recomputation is needed.
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
                avg_gap_days    AS AvgGapDays,
                ISNULL(recommended_load,   0) AS RecommendedLoad,
                ISNULL(forecast_corrected, 0) AS ForecastCorrected,
                ISNULL(bias_pct,           0) AS BiasPct,
                ISNULL(opening_stock,      0) AS OpeningStock,
                ISNULL(load_lower_bound,   0) AS LoadLowerBound,
                ISNULL(load_upper_bound,   0) AS LoadUpperBound
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
    # Closing stock (end-of-day inventory still on the van). Today's
    # opening = yesterday's closing. Drives carry-over arithmetic in the
    # reconciliation layer.
    # ------------------------------------------------------------------

    def closing_stock(
        self,
        routes: Optional[List[str]] = None,
        since_date: Optional[str] = None,
        lookback_days: Optional[int] = None,
    ) -> tuple[str, list]:
        routes = routes or self._s.route_codes
        ph = self._route_ph(routes)
        sql = f"""
            SELECT
                CAST(TrxDate AS DATE) AS TrxDate,
                RouteCode,
                ItemCode,
                ItemName,
                CategoryCode,
                CategoryName,
                CAST(SUM(ClosingQty) AS DECIMAL(18,2)) AS ClosingQty
            FROM {self._s.closing_stock_view} WITH (NOLOCK)
            WHERE RouteCode IN ({ph})
        """
        params: list = list(routes)
        if since_date:
            sql += "  AND TrxDate > ?\n"
            params.append(since_date)
        else:
            days = lookback_days or self._s.sales_recent_lookback_days
            sql += "  AND TrxDate >= DATEADD(day, -?, GETDATE())\n"
            params.append(days)
        sql += """
            GROUP BY CAST(TrxDate AS DATE), RouteCode, ItemCode, ItemName,
                     CategoryCode, CategoryName
        """
        return sql, params

    # ------------------------------------------------------------------
    # Load allocation -- the fresh top-up the rep loads each morning.
    # Multiple movements per day collapse to one row per (date, item).
    # ------------------------------------------------------------------

    def load_allocation(
        self,
        routes: Optional[List[str]] = None,
        since_date: Optional[str] = None,
        lookback_days: Optional[int] = None,
    ) -> tuple[str, list]:
        routes = routes or self._s.route_codes
        ph = self._route_ph(routes)
        sql = f"""
            SELECT
                CAST(MovementDate AS DATE) AS TrxDate,
                RouteCode,
                ItemCode,
                MAX(ItemName)     AS ItemName,
                MAX(CategoryCode) AS CategoryCode,
                MAX(CategoryName) AS CategoryName,
                CAST(SUM(AllocatedQuantityInPC) AS DECIMAL(18,2)) AS AllocatedPC
            FROM {self._s.load_allocation_view} WITH (NOLOCK)
            WHERE RouteCode IN ({ph})
        """
        params: list = list(routes)
        if since_date:
            sql += "  AND MovementDate > ?\n"
            params.append(since_date)
        else:
            days = lookback_days or self._s.sales_recent_lookback_days
            sql += "  AND MovementDate >= DATEADD(day, -?, GETDATE())\n"
            params.append(days)
        sql += """
            GROUP BY CAST(MovementDate AS DATE), RouteCode, ItemCode
        """
        return sql, params

    # ------------------------------------------------------------------
    # Sales returns (Bad + Good). Stored as negative QuantityInPCs in
    # VW_GET_SALES_DETAILS; we flip sign and split by TrxType so the
    # reconciliation layer can surface bad-vs-good rates separately.
    # ------------------------------------------------------------------

    def sales_returns(
        self,
        routes: Optional[List[str]] = None,
        since_date: Optional[str] = None,
        lookback_days: Optional[int] = None,
    ) -> tuple[str, list]:
        routes = routes or self._s.route_codes
        ph = self._route_ph(routes)
        sql = f"""
            SELECT
                CAST(TrxDate AS DATE) AS TrxDate,
                RouteCode,
                ItemCode,
                MAX(ItemName)     AS ItemName,
                MAX(CategoryCode) AS CategoryCode,
                MAX(CategoryName) AS CategoryName,
                CAST(SUM(CASE WHEN TrxType = ? THEN -QuantityInPCs ELSE 0 END) AS DECIMAL(18,2)) AS BadReturnQty,
                CAST(SUM(CASE WHEN TrxType = ? THEN -QuantityInPCs ELSE 0 END) AS DECIMAL(18,2)) AS GoodReturnQty
            FROM {self._s.sales_view} WITH (NOLOCK)
            WHERE ItemType = ?
              AND TrxType IN (?, ?)
              AND RouteCode IN ({ph})
        """
        params: list = [
            self._s.bad_return_trx_type,
            self._s.good_return_trx_type,
            self._s.sales_item_type,
            self._s.bad_return_trx_type,
            self._s.good_return_trx_type,
        ] + list(routes)
        if since_date:
            sql += "  AND TrxDate > ?\n"
            params.append(since_date)
        else:
            days = lookback_days or self._s.sales_recent_lookback_days
            sql += "  AND TrxDate >= DATEADD(day, -?, GETDATE())\n"
            params.append(days)
        sql += """
            GROUP BY CAST(TrxDate AS DATE), RouteCode, ItemCode
            HAVING SUM(CASE WHEN TrxType IN (?, ?) THEN -QuantityInPCs ELSE 0 END) > 0
        """
        params.extend([self._s.bad_return_trx_type, self._s.good_return_trx_type])
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
                CEILING(SUM(CASE WHEN s.QuantityInPCs > 0 THEN s.QuantityInPCs ELSE 0 END)) AS TotalQuantity,
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
