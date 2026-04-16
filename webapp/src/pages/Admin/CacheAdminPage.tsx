import { useState } from "react";
import { analyticsApi } from "@/api/analytics";
import Card from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import MetricCard from "@/components/charts/MetricCard";
import Loading from "@/components/ui/Loading";
import PageHeader from "@/components/layout/PageHeader";
import { useCacheStats } from "@/hooks/useAnalytics";

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

      {error && (
        <div className="bg-danger-50 border border-danger-100 rounded-lg p-4 text-body text-danger-700">
          {error}
        </div>
      )}

      {stats && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <MetricCard label="Cache Hits" value={stats.hits} />
          <MetricCard label="Cache Misses" value={stats.misses} />
          <MetricCard
            label="Hit Rate"
            value={`${(stats.hit_rate * 100).toFixed(1)}%`}
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
          <p className="text-body text-danger-600 mt-3">{clearError}</p>
        )}
      </Card>
    </div>
  );
}
