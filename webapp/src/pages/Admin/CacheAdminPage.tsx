import { useState } from "react";
import { analyticsApi } from "@/api/analytics";
import Card from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import MetricCard from "@/components/charts/MetricCard";
import Loading from "@/components/ui/Loading";
import Alert from "@/components/ui/Alert";
import PageHeader from "@/components/layout/PageHeader";
import { useCacheStats } from "@/hooks/useAnalytics";
import { fmtPct } from "@/lib/format";

export default function CacheAdminPage() {
  const { data: stats, loading, error, refetch } = useCacheStats();
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);

  const handleClear = async () => {
    setClearing(true);
    setClearError(null);
    try {
      await analyticsApi.clearCache();
      await refetch();
    } catch (err) {
      setClearError(err instanceof Error ? err.message : "Failed to clear cache");
    } finally {
      setClearing(false);
    }
  };

  if (loading) {
    return <Loading message="Loading cache stats..." />;
  }

  return (
    <div className="space-y-6">
      <PageHeader title="Cache" />

      {error && <Alert variant="error">{error}</Alert>}

      {stats && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <MetricCard label="Cache Hits" value={stats.hits} />
          <MetricCard label="Cache Misses" value={stats.misses} />
          <MetricCard
            label="Hit Rate"
            value={fmtPct(stats.hit_rate * 100)}
            trend={stats.hit_rate > 0.5 ? "up" : "down"}
          />
          <MetricCard label="Cached Entries" value={stats.cached_entries} />
        </div>
      )}

      <Card title="Cache Actions">
        <div className="flex items-center gap-4">
          <Button variant="danger" loading={clearing} onClick={handleClear}>
            Clear Cache
          </Button>
          <Button variant="ghost" onClick={() => refetch()}>
            Refresh Stats
          </Button>
        </div>
        {clearError && (
          <div className="mt-3">
            <Alert variant="error">{clearError}</Alert>
          </div>
        )}
      </Card>
    </div>
  );
}
