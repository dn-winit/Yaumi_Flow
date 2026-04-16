import Badge from "./Badge";
import ContextStrip from "./ContextStrip";

interface DrawerContextBarProps {
  routeCode?: string;
  itemCodes?: string[];
  dateRange?: string;
  extra?: React.ReactNode;
}

/**
 * Compact strip shown at the top of analytics drawers. Gives the supervisor a
 * one-line read of what the numbers below are filtered by -- route, SKU
 * selection, and the time window.
 */
export default function DrawerContextBar({
  routeCode,
  itemCodes,
  dateRange,
  extra,
}: DrawerContextBarProps) {
  const skuLabel =
    !itemCodes || itemCodes.length === 0
      ? "All SKUs"
      : itemCodes.length === 1
      ? itemCodes[0]
      : `${itemCodes.length} SKUs`;

  const items = [
    {
      label: "Route",
      value: (
        <Badge variant={routeCode ? "info" : "neutral"}>{routeCode || "All"}</Badge>
      ),
    },
    {
      label: "SKUs",
      value: <Badge variant="neutral">{skuLabel}</Badge>,
    },
  ];
  if (dateRange) {
    items.push({
      label: "Window",
      value: <span className="font-medium text-text-primary">{dateRange}</span>,
    });
  }

  return <ContextStrip items={items} actions={extra} />;
}
