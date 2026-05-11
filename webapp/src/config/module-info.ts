import type { ProcessStep } from "@/components/ui/InfoPanel";

export const VAN_LOAD_INFO = {
  title: "How demand forecasting works",
  subtitle:
    "Every item on the van is backed by a data-driven prediction. Here's how the system decides what to load and how much.",
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
        "Every prediction carries a confidence score and a likely range — the lower and upper bounds you see on the Van Load table are the conformal-calibrated band the truck's load is guaranteed to sit inside.",
      highlights: ["Confidence scoring", "Conformal lower / upper band", "Uncertainty visibility"],
    },
    {
      title: "Journey-aware allocation",
      description:
        "If an item is bought by only one or two customers and none of them is on today's journey plan, recommended load drops to zero for that day. Stops the truck from carrying stock no scheduled customer can buy.",
      highlights: ["Whale-buyer detection", "Journey-plan intersection", "Phantom-load prevention"],
    },
    {
      title: "Accuracy tracking",
      description:
        "Forecast accuracy is measured daily against actual sales. The system tracks performance trends over time, so you can see it improving as it learns your data.",
      highlights: ["Daily measurement", "Trend tracking", "Self-improving"],
    },
  ] satisfies ProcessStep[],
};
