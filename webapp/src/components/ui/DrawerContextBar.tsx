import Badge from "./Badge";
import ContextStrip from "./ContextStrip";

interface DrawerContextBarProps {
  routeCode?: string;
  itemCodes?: string[];
  dateRange?: string;
  extra?: React.ReactNode;
}

/** Top-of-drawer strip showing the active route/items/window scope. */
export default function DrawerContextBar({
  routeCode,
  itemCodes,
  dateRange,
  extra,
}: DrawerContextBarProps) {
  const skuLabel =
    !itemCodes || itemCodes.length === 0
      ? "All items"
      : itemCodes.length === 1
        ? itemCodes[0]
        : `${itemCodes.length} items`;

  const items = [
    {
      label: "Route",
      value: <Badge variant={routeCode ? "info" : "neutral"}>{routeCode || "All"}</Badge>,
    },
    {
      label: "Items",
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
