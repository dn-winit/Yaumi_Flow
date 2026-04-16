import React, { useState } from "react";
import { recommendedOrderApi } from "@/api/recommended-order";
import { supervisionApi } from "@/api/supervision";
import { useFilterOptions } from "@/hooks/useRecommendedOrder";
import Card from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import DatePicker from "@/components/ui/DatePicker";
import Select from "@/components/ui/Select";
import { todayIso } from "@/lib/date";

interface SessionInitProps {
  onSessionCreated: (sessionId: string, sessionData: Record<string, unknown>) => void;
}

export default function SessionInit({ onSessionCreated }: SessionInitProps) {
  const [date, setDate] = useState(todayIso);
  const [routeCode, setRouteCode] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: filterOptions } = useFilterOptions();

  const routeOptions = (filterOptions?.routes ?? []).map((r) => ({
    value: r,
    label: r,
  }));

  const handleInit = async () => {
    if (!routeCode || !date) return;
    setLoading(true);
    setError(null);
    try {
      // Fetch recommendations first
      const recsRes = await recommendedOrderApi.getRecommendations({
        date,
        route_code: routeCode,
        limit: 1000,
        offset: 0,
      });

      // Pass the raw records straight through -- the supervision service reads
      // PascalCase keys (CustomerCode, ItemCode, RecommendedQuantity, ...).
      const recommendations = (recsRes.data ?? []) as unknown as Record<string, unknown>[];

      // Initialize session
      const sessionRes = await supervisionApi.initSession(
        routeCode,
        date,
        recommendations
      );

      const session = sessionRes.session ?? {};
      const sessionId = String(session.session_id ?? session.sessionId ?? "");
      // Server returns a summary-only payload; hydrate with the recommendations
      // list we already have so the live tab can render customer cards.
      onSessionCreated(sessionId, { ...session, recommendations });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to initialize session");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card title="Initialize Session">
      <div className="flex items-end gap-4">
        <Select
          value={routeCode}
          onChange={setRouteCode}
          options={routeOptions}
          placeholder="Select route..."
          label="Route"
        />
        <DatePicker value={date} onChange={setDate} label="Date" />
        <Button
          variant="primary"
          loading={loading}
          disabled={!routeCode || !date}
          onClick={handleInit}
        >
          Initialize Session
        </Button>
      </div>
      {error && (
        <p className="text-body text-danger-600 mt-3">{error}</p>
      )}
    </Card>
  );
}
