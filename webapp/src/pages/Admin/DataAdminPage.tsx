import {
  useDataStatus,
  useImportDataset,
  useImportAll,
} from "@/hooks/useDataImport";
import Loading from "@/components/ui/Loading";
import Card from "@/components/ui/Card";
import PageHeader from "@/components/layout/PageHeader";
import DatasetStatusTable from "./DatasetStatusTable";
import ImportActions from "./ImportActions";

export default function DataAdminPage() {
  const { datasets, loading, error, refetch } = useDataStatus();
  const importDataset = useImportDataset();
  const importAll = useImportAll();

  const handleImport = async (dataset: string, mode: string) => {
    try {
      await importDataset.execute(dataset, mode);
      refetch();
    } catch {
      // error managed by hook
    }
  };

  const handleImportAll = async (mode: string) => {
    try {
      await importAll.execute(mode);
      refetch();
    } catch {
      // error managed by hook
    }
  };

  if (loading) {
    return <Loading message="Loading data status..." />;
  }

  return (
    <div className="space-y-6">
      <PageHeader title="Data Management" />

      {error && (
        <div className="bg-danger-50 border border-danger-100 rounded-lg p-4 text-body text-danger-700">
          {error}
        </div>
      )}

      {importDataset.error && (
        <div className="bg-danger-50 border border-danger-100 rounded-lg p-4 text-body text-danger-700">
          Import error: {importDataset.error}
        </div>
      )}

      {importAll.error && (
        <div className="bg-danger-50 border border-danger-100 rounded-lg p-4 text-body text-danger-700">
          Import all error: {importAll.error}
        </div>
      )}

      {importDataset.result && (
        <div className="bg-success-50 border border-success-100 rounded-lg p-4 text-body text-success-700">
          {importDataset.result.message} -- {importDataset.result.new_rows} new
          rows ({importDataset.result.duration_seconds.toFixed(1)}s)
        </div>
      )}

      <Card title="Import Actions">
        <ImportActions
          datasetNames={datasets ? Object.keys(datasets) : []}
          onImport={handleImport}
          onImportAll={handleImportAll}
          loading={importDataset.loading || importAll.loading}
        />
      </Card>

      <Card title="Dataset Status">
        {datasets ? (
          <DatasetStatusTable datasets={datasets} />
        ) : (
          <p className="text-body text-text-tertiary">
            No dataset information available.
          </p>
        )}
      </Card>
    </div>
  );
}
