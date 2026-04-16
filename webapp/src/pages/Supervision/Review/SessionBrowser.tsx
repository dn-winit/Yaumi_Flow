import React, { useState, useEffect } from "react";
import { supervisionApi } from "@/api/supervision";
import Card from "@/components/ui/Card";
import Table from "@/components/ui/Table";
import Loading from "@/components/ui/Loading";
import DatePicker from "@/components/ui/DatePicker";
import Select from "@/components/ui/Select";
import Filters from "@/components/ui/Filters";
import { useFilterOptions } from "@/hooks/useRecommendedOrder";
import type { SessionListItem } from "@/types/supervision";

interface SessionBrowserProps {
  onSelectSession: (routeCode: string, date: string) => void;
}

export default function SessionBrowser({
  onSelectSession,
}: SessionBrowserProps) {
  const [date, setDate] = useState("");
  const [routeFilter, setRouteFilter] = useState("");
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [loading, setLoading] = useState(false);

  const { data: filterOptions } = useFilterOptions();

  const routeOptions = [
    { value: "", label: "All Routes" },
    ...(filterOptions?.routes ?? []).map((r) => ({ value: r, label: r })),
  ];

  const fetchSessions = async () => {
    setLoading(true);
    try {
      const res = await supervisionApi.listSessions(date || undefined);
      let list = res.sessions ?? [];
      if (routeFilter) {
        list = list.filter((s) => s.routeCode === routeFilter);
      }
      setSessions(list);
    } catch {
      setSessions([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSessions();
  }, []);

  const columns = [
    { key: "routeCode", label: "Route" },
    { key: "date", label: "Date" },
    {
      key: "sizeBytes",
      label: "Size",
      render: (row: Record<string, unknown>) => {
        const bytes = Number(row.sizeBytes);
        return bytes > 1024
          ? `${(bytes / 1024).toFixed(1)} KB`
          : `${bytes} B`;
      },
    },
    {
      key: "modified",
      label: "Modified",
      render: (row: Record<string, unknown>) =>
        new Date(Number(row.modified) * 1000).toLocaleString(),
    },
  ];

  return (
    <Card title="Saved Sessions">
      <Filters onApply={fetchSessions} onReset={() => { setDate(""); setRouteFilter(""); }}>
        <DatePicker value={date} onChange={setDate} label="Date" />
        <Select
          value={routeFilter}
          onChange={setRouteFilter}
          options={routeOptions}
          label="Route"
        />
      </Filters>
      <div className="mt-4">
        {loading ? (
          <Loading message="Loading sessions..." />
        ) : (
          <Table
            data={sessions as unknown as Record<string, unknown>[]}
            columns={columns}
            emptyMessage="No saved sessions found"
            onRowClick={(row) =>
              onSelectSession(
                String(row.routeCode),
                String(row.date)
              )
            }
          />
        )}
      </div>
    </Card>
  );
}
