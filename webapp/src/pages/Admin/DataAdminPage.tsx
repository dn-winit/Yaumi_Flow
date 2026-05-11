import {
  useDataStatus,
  useImportDataset,
  useImportAll,
} from "@/hooks/useDataImport";
import Loading from "@/components/ui/Loading";
import Card from "@/components/ui/Card";
import Alert from "@/components/ui/Alert";
import PageHeader from "@/components/layout/PageHeader";
import { fmtDuration } from "@/lib/format";
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

      {error && <Alert variant="error">{error}</Alert>}
      {importDataset.error && <Alert variant="error">Import error: {importDataset.error}</Alert>}
      {importAll.error && <Alert variant="error">Import all error: {importAll.error}</Alert>}
      {importDataset.result && (
        <Alert variant="success">
          {importDataset.result.message} -- {importDataset.result.new_rows} new rows ({fmtDuration(importDataset.result.duration_seconds)})
        </Alert>
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
