"""Parameterised SQL builder supporting full and incremental modes."""

from __future__ import annotations

from common.sql_fragments import NET_SOLD_CASE_SQL, RETURNS_SUBQUERY_BODY_SQL
from data_import.config.settings import Settings, get_settings


class QueryBuilder:
    """Builds SQL for customer data, journey plan, and sales recent."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()

    def _route_ph(self, routes: list[str]) -> str:
        return ",".join("?" for _ in routes)

    # ------------------------------------------------------------------
    # Customer data (incremental by TrxDate)
    # ------------------------------------------------------------------

    def customer_data(
        self,
        routes: list[str] | None = None,
        since_date: str | None = None,
        lookback_days: int | None = None,
    ) -> tuple[str, list]:
        """``since_date`` -> incremental (TrxDate > date); else full refresh
        over last ``lookback_days``."""
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
            WHERE s.ItemType  = ?
              AND s.TrxType   = ?
              AND s.RouteCode IN ({ph})
        """
        params: list = [self._s.sales_item_type, self._s.sales_invoice_trx_type, *routes]

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
        routes: list[str] | None = None,
        since_date: str | None = None,
        window_days: int | None = None,
    ) -> tuple[str, list]:
        # Explicit columns pin the view-schema contract; order matches the
        # downstream CSV header so read_csv consumers stay byte-stable.
        sql = f"""
            SELECT
                JourneyDate,
                RouteCode,
                WarehouseCode,
                WarehouseName,
                CustomerCode,
                CustomerName,
                SalesClassCode,
                SalesClassName,
                CustomerGroupCode,
                CustomerGroupName,
                VisitSequence,
                Customer_Latitude,
                Customer_Longitude,
                Warehouse_Latitude,
                Warehouse_Longitude
            FROM {self._s.journey_view} WITH (NOLOCK)
            WHERE RouteCode IN ({self._route_ph(routes or self._s.route_codes)})
        """
        routes = routes or self._s.route_codes
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
        routes: list[str] | None = None,
        since_date: str | None = None,
        lookback_days: int | None = None,
    ) -> tuple[str, list]:
        routes = routes or self._s.route_codes
        ph = self._route_ph(routes)

        # yf_demand_forecast is purely model output; reconciliation columns
        # live in yf_sales_transactions (see ``sales_transactions()`` below).
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
            # Forecast horizon (current+future) -- past in yf_sales_transactions.
            days = lookback_days or self._s.demand_forecast_lookback_days
            sql += "  AND trx_date >= DATEADD(day, -?, GETDATE())\n"
            params.append(days)

        return sql, params

    # Sales transactions: carry chain + diagnostics + actual_sold.
    # One row per (route, item, date); source of truth for reconciliation.

    def sales_transactions(
        self,
        routes: list[str] | None = None,
        since_date: str | None = None,
        lookback_days: int | None = None,
    ) -> tuple[str, list]:
        routes = routes or self._s.route_codes
        ph = self._route_ph(routes)
        sql = f"""
            SELECT
                trx_date                    AS TrxDate,
                route_code                  AS RouteCode,
                item_code                   AS ItemCode,
                opening_stock               AS OpeningStock,
                fresh_load                  AS FreshLoad,
                total_van_load              AS TotalVanLoad,
                leftover_to_next_day        AS LeftoverToNextDay,
                actual_sold                 AS ActualSold,
                bias_pct                    AS BiasPct,
                forecast_corrected          AS ForecastCorrected,
                expected_demand             AS ExpectedDemand,
                van_load_lower_bound        AS VanLoadLowerBound,
                van_load_upper_bound        AS VanLoadUpperBound,
                recent_daily_avg            AS RecentDailyAvg,
                CAST(pattern_floor_applied   AS INT) AS PatternFloorApplied,
                CAST(pattern_ceiling_applied AS INT) AS PatternCeilingApplied,
                CAST(forecast_below_recent   AS INT) AS ForecastBelowRecent,
                CAST(forecast_dormant        AS INT) AS ForecastDormant,
                yaumi_opening_stock        AS YaumiOpeningStock,
                yaumi_fresh_load           AS YaumiFreshLoad,
                yaumi_total_van_load       AS YaumiTotalVanLoad,
                yaumi_leftover             AS YaumiLeftover
            FROM {self._s.sales_transactions_table} WITH (NOLOCK)
            WHERE route_code IN ({ph})
        """
        params: list = list(routes)
        if since_date:
            sql += "  AND trx_date > ?\n"
            params.append(since_date)
        else:
            # Reconciliation history window (NOT the model training window).
            days = lookback_days or self._s.sales_transactions_lookback_days
            sql += "  AND trx_date >= DATEADD(day, -?, GETDATE())\n"
            params.append(days)
        return sql, params

    # Closing stock: end-of-day van inventory; today's opening = yesterday's closing.

    def closing_stock(
        self,
        routes: list[str] | None = None,
        since_date: str | None = None,
        lookback_days: int | None = None,
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
            # Feeds carry-chain ffill (7d) + past-performance (90d) + buffer.
            days = lookback_days or self._s.closing_stock_lookback_days
            sql += "  AND TrxDate >= DATEADD(day, -?, GETDATE())\n"
            params.append(days)
        sql += """
            GROUP BY CAST(TrxDate AS DATE), RouteCode, ItemCode, ItemName,
                     CategoryCode, CategoryName
        """
        return sql, params

    # Load allocation: morning fresh top-up; one row per (date, item).

    def load_allocation(
        self,
        routes: list[str] | None = None,
        since_date: str | None = None,
        lookback_days: int | None = None,
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
            # Pairs with past-performance + load tile (90d + buffer).
            days = lookback_days or self._s.load_allocation_lookback_days
            sql += "  AND MovementDate >= DATEADD(day, -?, GETDATE())\n"
            params.append(days)
        sql += """
            GROUP BY CAST(MovementDate AS DATE), RouteCode, ItemCode
        """
        return sql, params

    # Sales returns: negative QuantityInPCs flipped + split by TrxType
    # so reconciliation can surface bad-vs-good rates separately.

    def sales_returns(
        self,
        routes: list[str] | None = None,
        since_date: str | None = None,
        lookback_days: int | None = None,
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
            # Drives bad/good rate drawer + past-performance (90d + buffer).
            days = lookback_days or self._s.sales_returns_lookback_days
            sql += "  AND TrxDate >= DATEADD(day, -?, GETDATE())\n"
            params.append(days)
        sql += """
            GROUP BY CAST(TrxDate AS DATE), RouteCode, ItemCode
            HAVING SUM(CASE WHEN TrxType IN (?, ?) THEN -QuantityInPCs ELSE 0 END) > 0
        """
        params.extend([self._s.bad_return_trx_type, self._s.good_return_trx_type])
        return sql, params

    # Sales recent: return-netted at source via ReturnItem_InvoiceRef so
    # the reduction lands on the ORIGINAL invoice date (lag-aware demand).
    # Subquery shares route+date predicates with the outer query so the
    # optimiser bounds both scans identically. Orphan returns
    # (InvoiceRef IS NULL) flow through ``sales_returns`` instead.
    # Line-level CASE floors at 0 so an over-return can't go negative.

    def _date_clause(self, *, alias: str, since_date: str | None,
                     lookback_days: int) -> tuple[str, list]:
        """Date-window WHERE fragment shared by outer query + returns
        subquery so both bound the scan to the same window."""
        if since_date:
            return f"  AND {alias}.TrxDate > ?\n", [since_date]
        return (
            f"  AND {alias}.TrxDate >= DATEADD(day, -?, GETDATE())\n",
            [lookback_days],
        )

    def sales_recent(
        self,
        routes: list[str] | None = None,
        since_date: str | None = None,
        lookback_days: int | None = None,
    ) -> tuple[str, list]:
        routes = routes or self._s.route_codes
        ph = self._route_ph(routes)
        days = lookback_days or self._s.sales_recent_lookback_days

        # Subquery + outer share the same window via ``_date_clause``.
        sub_date_sql, sub_date_params = self._date_clause(
            alias="r", since_date=since_date, lookback_days=days,
        )
        out_date_sql, out_date_params = self._date_clause(
            alias="s", since_date=since_date, lookback_days=days,
        )

        # Push route+date predicates into the subquery (pre-aggregation).
        # Body from common.sql_fragments keeps netting semantics
        # byte-identical with reconciliation_refresh._fetch_actual_sold.
        returns_subquery = RETURNS_SUBQUERY_BODY_SQL.format(
            view=self._s.sales_view,
            route_clause=f"AND r.RouteCode IN ({ph})",
            date_clause=sub_date_sql.strip(),
        )

        sql = f"""
            SELECT
                s.TrxDate,
                s.WarehouseCode,
                s.WarehouseName,
                s.RouteCode,
                s.ItemCode,
                s.ItemName,
                s.CategoryName,
                CEILING(SUM({NET_SOLD_CASE_SQL})) AS TotalQuantity,
                ROUND(AVG(s.UnitPrice), 2) AS AvgUnitPrice
            FROM {self._s.sales_view} s WITH (NOLOCK)
            LEFT JOIN ({returns_subquery}) rj
                ON s.TrxCode = rj.InvoiceRef
               AND s.ItemCode = rj.ItemCode
            WHERE s.ItemType  = ?
              AND s.TrxType   = ?
              AND s.RouteCode IN ({ph})
            {out_date_sql.strip()}
            GROUP BY
                s.TrxDate, s.WarehouseCode, s.WarehouseName,
                s.RouteCode, s.ItemCode, s.ItemName, s.CategoryName
        """

        params: list = [
            # Inner subquery params (source order):
            self._s.bad_return_trx_type,
            self._s.good_return_trx_type,
            *routes,
            *sub_date_params,
            # Outer query params:
            self._s.sales_item_type,
            self._s.sales_invoice_trx_type,
            *routes,
            *out_date_params,
        ]
        return sql, params
