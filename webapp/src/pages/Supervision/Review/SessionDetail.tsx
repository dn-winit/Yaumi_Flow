import React, { useState } from "react";
import Card from "@/components/ui/Card";
import Table from "@/components/ui/Table";
import MetricCard from "@/components/charts/MetricCard";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Modal from "@/components/ui/Modal";
import Loading from "@/components/ui/Loading";
import { useAnalyzeRoute, useAnalyzeCustomer } from "@/hooks/useAnalytics";
import type { AnalysisResponse } from "@/types/analytics";

interface SessionDetailProps {
  session: Record<string, unknown>;
}

const ANALYSIS_SECTIONS: { key: string; title: string; tone: string }[] = [
  { key: "summary", title: "Summary", tone: "bg-brand-50 border-brand-100" },
  { key: "strengths", title: "Strengths", tone: "bg-success-50 border-success-100" },
  { key: "weaknesses", title: "Weaknesses", tone: "bg-danger-50 border-danger-100" },
  {
    key: "opportunities",
    title: "Opportunities",
    tone: "bg-warning-50 border-warning-100",
  },
  {
    key: "recommendations",
    title: "Recommendations",
    tone: "bg-info-50 border-info-100",
  },
  { key: "quick_tips", title: "Quick Tips", tone: "bg-surface-sunken border-default" },
];

function renderValue(value: unknown): React.ReactNode {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") {
    return <p className="text-sm text-text-secondary whitespace-pre-wrap">{value}</p>;
  }
  if (Array.isArray(value)) {
    return (
      <ul className="list-disc ml-5 space-y-1">
        {value.map((v, i) => (
          <li key={i} className="text-sm text-text-secondary">
            {typeof v === "string" ? v : JSON.stringify(v)}
          </li>
        ))}
      </ul>
    );
  }
  if (typeof value === "object") {
    return (
      <pre className="text-xs text-text-secondary whitespace-pre-wrap font-mono">
        {JSON.stringify(value, null, 2)}
      </pre>
    );
  }
  return <p className="text-sm text-text-secondary">{String(value)}</p>;
}

function AnalysisView({
  loading,
  error,
  result,
}: {
  loading: boolean;
  error: string | null;
  result: AnalysisResponse | null | undefined;
}) {
  if (loading) return <Loading message="Running AI analysis..." />;
  if (error) {
    return (
      <div className="bg-danger-50 border border-danger-100 rounded-lg p-4 text-sm text-danger-700">
        {error}
      </div>
    );
  }
  if (!result) return null;

  const data = (result.data ?? {}) as Record<string, unknown>;
  const known = new Set(ANALYSIS_SECTIONS.map((s) => s.key));
  const extras = Object.keys(data).filter((k) => !known.has(k));

  return (
    <div className="space-y-3">
      {ANALYSIS_SECTIONS.map((section) => {
        const value = data[section.key];
        if (!value) return null;
        return (
          <div
            key={section.key}
            className={`rounded-lg border p-4 ${section.tone}`}
          >
            <h4 className="text-sm font-semibold text-text-primary mb-2">
              {section.title}
            </h4>
            {renderValue(value)}
          </div>
        );
      })}
      {extras.map((key) => (
        <div
          key={key}
          className="rounded-lg border border-default bg-surface-sunken p-4"
        >
          <h4 className="text-sm font-semibold text-text-primary mb-2 capitalize">
            {key.replace(/_/g, " ")}
          </h4>
          {renderValue(data[key])}
        </div>
      ))}
      {result.cached && (
        <p className="text-xs text-text-tertiary italic">Cached result</p>
      )}
    </div>
  );
}

export default function SessionDetail({ session }: SessionDetailProps) {
  const routeCode = String(session.route_code ?? session.routeCode ?? "");
  const date = String(session.date ?? "");
  const customers = (session.customers ?? session.visits ?? []) as Record<
    string,
    unknown
  >[];

  const routeScore = Number(session.route_score ?? session.routeScore ?? 0);
  const visited = customers.filter(
    (c) => c.visited === true || c.status === "visited"
  ).length;

  const routeAnalysis = useAnalyzeRoute();
  const customerAnalysis = useAnalyzeCustomer();

  const [routeModalOpen, setRouteModalOpen] = useState(false);
  const [customerModalOpen, setCustomerModalOpen] = useState(false);
  const [activeCustomer, setActiveCustomer] = useState<string>("");

  const handleRouteAnalyze = () => {
    setRouteModalOpen(true);
    routeAnalysis.execute(session);
  };

  const handleCustomerAnalyze = (customer: Record<string, unknown>) => {
    const code = String(customer.customerCode ?? customer.CustomerCode ?? "");
    setActiveCustomer(code);
    setCustomerModalOpen(true);
    customerAnalysis.execute({ ...customer, route_code: routeCode, date });
  };

  const customerColumns = [
    { key: "customerCode", label: "Customer" },
    {
      key: "visited",
      label: "Status",
      render: (row: Record<string, unknown>) => (
        <Badge
          variant={row.visited || row.status === "visited" ? "success" : "neutral"}
        >
          {row.visited || row.status === "visited" ? "Visited" : "Not Visited"}
        </Badge>
      ),
    },
    {
      key: "score",
      label: "Score",
      render: (row: Record<string, unknown>) => {
        const s = row.score as Record<string, unknown> | number | undefined;
        if (typeof s === "number") return `${(s * 100).toFixed(1)}%`;
        if (s && typeof s === "object")
          return `${(Number(s.score ?? 0) * 100).toFixed(1)}%`;
        return "-";
      },
    },
    {
      key: "actions",
      label: "",
      render: (row: Record<string, unknown>) => (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => handleCustomerAnalyze(row)}
        >
          Analyze
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-text-primary">
          Session: {routeCode} - {date}
        </h2>
        <Button
          variant="primary"
          size="sm"
          onClick={handleRouteAnalyze}
          loading={routeAnalysis.loading && routeModalOpen}
        >
          Run AI Analysis
        </Button>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
        <MetricCard label="Route" value={routeCode} subtitle={date} />
        <MetricCard
          label="Visited"
          value={`${visited}/${customers.length}`}
          subtitle="Customers"
        />
        <MetricCard
          label="Route Score"
          value={`${routeScore.toFixed(1)}%`}
          trend={routeScore > 0.7 ? "up" : "down"}
        />
      </div>

      <Card title="Customer Details">
        <Table
          data={customers}
          columns={customerColumns}
          emptyMessage="No customer data"
        />
      </Card>

      <Modal
        open={routeModalOpen}
        onClose={() => setRouteModalOpen(false)}
        title={`AI Analysis — Route ${routeCode}`}
        size="xl"
      >
        <AnalysisView
          loading={routeAnalysis.loading}
          error={routeAnalysis.error}
          result={routeAnalysis.result}
        />
      </Modal>

      <Modal
        open={customerModalOpen}
        onClose={() => setCustomerModalOpen(false)}
        title={`AI Analysis — Customer ${activeCustomer}`}
        size="xl"
      >
        <AnalysisView
          loading={customerAnalysis.loading}
          error={customerAnalysis.error}
          result={customerAnalysis.result}
        />
      </Modal>
    </div>
  );
}
