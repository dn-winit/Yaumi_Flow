import type { ProcessStep } from "@/components/ui/InfoPanel";

export const VAN_LOAD_INFO = {
  title: "How demand forecasting works",
  subtitle:
    "Every item on the van is backed by a data-driven prediction. Here's how the system determines what to load and how much.",
  steps: [
    {
      title: "Data collection",
      description:
        "Sales history is gathered from multiple sources and continuously kept up to date. Every transaction, across every route and customer, feeds into the system.",
      highlights: ["Multi-source integration", "Real-time updates", "Full transaction history"],
    },
    {
      title: "Intelligent data processing",
      description:
        "Raw data goes through automated cleaning — handling gaps, removing anomalies, normalising returns, and filtering noise so the models work with reliable signals.",
      highlights: ["Anomaly detection", "Gap handling", "Return normalisation"],
    },
    {
      title: "Context-aware feature engineering",
      description:
        "The system automatically builds contextual signals that affect demand. It knows when holidays fall, detects weekly and seasonal rhythms, and understands how each route behaves differently.",
      highlights: [
        "Holiday & festival calendars",
        "Day-of-week patterns",
        "Seasonal trends",
        "Route-specific behaviour",
      ],
    },
    {
      title: "Demand classification",
      description:
        "Each item is classified by its demand pattern — fast-moving, slow-moving, intermittent, or erratic. Different patterns need different forecasting strategies, and the system picks the right approach for each.",
      highlights: ["Pattern recognition", "Adaptive model selection", "Per-item strategy"],
    },
    {
      title: "Multi-model forecasting",
      description:
        "Multiple forecasting models compete on every item. The system evaluates each model's track record and selects the most accurate one — no single approach is forced on all items.",
      highlights: ["Ensemble approach", "Best-model selection", "Continuous evaluation"],
    },
    {
      title: "Confidence & range estimation",
      description:
        "Every prediction carries a confidence score and a likely range (low to high estimate). This helps supervisors know which items have solid predictions and which need extra attention.",
      highlights: ["Confidence scoring", "Low-to-high range", "Uncertainty visibility"],
    },
    {
      title: "Accuracy tracking",
      description:
        "Forecast accuracy is measured daily against actual sales. The system tracks performance trends over time, so you can see it improving as it learns your data.",
      highlights: ["Daily measurement", "Trend tracking", "Self-improving"],
    },
  ] satisfies ProcessStep[],
};

export const RECOMMENDED_ORDERS_INFO = {
  title: "How order recommendations work",
  subtitle:
    "Every recommendation is personalised to the specific customer, based on their buying history and what works on their route.",
  steps: [
    {
      title: "Customer profiling",
      description:
        "Each customer's complete buying history is analysed — what items they purchase, how frequently, how much per visit, and whether their patterns are consistent or changing.",
      highlights: ["Per-customer analysis", "Frequency tracking", "Trend detection"],
    },
    {
      title: "Purchase cycle detection",
      description:
        "The system identifies each customer's natural buying rhythm per item. Some items are bought weekly, others monthly — the system learns each cycle and knows when the customer is due.",
      highlights: ["Per-item cycles", "Due-date prediction", "Pattern confidence"],
    },
    {
      title: "Three recommendation strategies",
      description:
        "Three independent approaches work together to build the most complete recommendation for each customer:",
      highlights: [
        "History-based — items the customer regularly buys, timed to their cycle",
        "Peer matching — items similar customers on the same route buy",
        "Basket analysis — items commonly purchased together",
      ],
    },
    {
      title: "Smart quantity sizing",
      description:
        "Quantities are based on recent purchase behaviour, not just long-term averages. Recent visits carry more weight, and trending items get adjusted upward or downward automatically.",
      highlights: ["Recency-weighted", "Trend-adjusted", "Van-load aware"],
    },
    {
      title: "Per-route calibration",
      description:
        "Every threshold and parameter is calculated from the route's own data — not generic settings. What works on one route is different from another, and the system adapts accordingly.",
      highlights: ["Route-specific tuning", "Data-driven thresholds", "No manual configuration"],
    },
    {
      title: "Priority ranking & explainability",
      description:
        "Every recommendation is scored and ranked so drivers focus on the highest-value items first. Each recommendation comes with a plain-language explanation of why it was chosen and how the quantity was sized.",
      highlights: ["Transparent reasoning", "Priority scoring", "Actionable explanations"],
    },
    {
      title: "Continuous learning",
      description:
        "The system tracks which recommendations actually convert to sales. Over time, it learns which strategies work best for each route and adjusts future recommendations automatically.",
      highlights: ["Outcome tracking", "Adaptive feedback", "Self-improving"],
    },
  ] satisfies ProcessStep[],
};

export const SUPERVISION_INFO = {
  title: "How live supervision works",
  subtitle:
    "The supervision module connects to live sales data so you can track route performance as it happens — not after the day is over.",
  steps: [
    {
      title: "Real-time data connection",
      description:
        "The system connects to live sales data and updates every 60 seconds. When a customer invoice is created, you see it on screen within a minute.",
      highlights: ["60-second refresh", "Live invoicing data", "No manual entry"],
    },
    {
      title: "Planned vs unplanned tracking",
      description:
        "The system automatically separates planned customer visits (on today's route) from drop-in sales (customers not on the plan). Both are tracked and scored independently.",
      highlights: ["Automatic classification", "Drop-in detection", "Full route visibility"],
    },
    {
      title: "Visit scoring",
      description:
        "Every customer visit is scored in three dimensions: how many recommended items were bought (items matched), how close the quantities were (quantity accuracy), and an overall weighted score.",
      highlights: ["Item matching", "Quantity accuracy", "Overall score"],
    },
    {
      title: "Smart redistribution",
      description:
        "When a customer buys less than recommended, the unsold items are automatically redistributed to remaining customers on the route — prioritised by who's most likely to buy them.",
      highlights: ["Automatic reallocation", "Priority-based", "Van utilisation"],
    },
    {
      title: "AI-powered route review",
      description:
        "Request an AI analysis of any customer visit or the entire route. Get actionable insights — strengths, areas for improvement, and specific instructions for the supervisor.",
      highlights: ["Per-customer analysis", "Route-level summary", "Actionable insights"],
    },
    {
      title: "Session management",
      description:
        "Every supervision session is saved with full scoring history, redistribution records, and visit outcomes. Review past sessions for team coaching, performance tracking, and trend analysis.",
      highlights: ["Complete history", "Team coaching", "Performance trends"],
    },
  ] satisfies ProcessStep[],
};
