import React, { useState } from "react";
import Button from "@/components/ui/Button";
import Select from "@/components/ui/Select";

interface ImportActionsProps {
  datasetNames: string[];
  onImport: (dataset: string, mode: string) => void;
  onImportAll: (mode: string) => void;
  loading: boolean;
}

export default function ImportActions({
  datasetNames,
  onImport,
  onImportAll,
  loading,
}: ImportActionsProps) {
  const [selectedDataset, setSelectedDataset] = useState("");
  const [mode, setMode] = useState("incremental");

  const modeOptions = [
    { value: "incremental", label: "Incremental" },
    { value: "full", label: "Full" },
  ];

  return (
    <div className="space-y-4">
      {/* Bulk actions */}
      <div className="flex items-center gap-3">
        <Button
          variant="primary"
          loading={loading}
          onClick={() => onImportAll("incremental")}
        >
          Import All (Incremental)
        </Button>
        <Button
          variant="secondary"
          loading={loading}
          onClick={() => onImportAll("full")}
        >
          Import All (Full)
        </Button>
      </div>

      {/* Per-dataset import */}
      <div className="flex items-end gap-3">
        <Select
          value={selectedDataset}
          onChange={setSelectedDataset}
          options={datasetNames.map((d) => ({ value: d, label: d }))}
          placeholder="Select dataset..."
          label="Dataset"
        />
        <Select
          value={mode}
          onChange={setMode}
          options={modeOptions}
          label="Mode"
        />
        <Button
          variant="primary"
          size="sm"
          loading={loading}
          disabled={!selectedDataset}
          onClick={() => onImport(selectedDataset, mode)}
        >
          Import
        </Button>
      </div>
    </div>
  );
}
