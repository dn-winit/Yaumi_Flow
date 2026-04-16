import { useMemo, useState } from "react";
import Select from "@/components/ui/Select";
import DatePicker from "@/components/ui/DatePicker";
import Button from "@/components/ui/Button";
import Modal from "@/components/ui/Modal";
import { useFilterOptions } from "@/hooks/useRecommendedOrder";
import { useWorkflow } from "@/pages/Workflow/workflowContext";

interface Props {
  availableItems: string[];
  routeCode: string;
  setRouteCode: (v: string) => void;
}

export default function VanLoadFilters({ availableItems, routeCode, setRouteCode }: Props) {
  const { date, selectedItems, setDate, setSelectedItems } = useWorkflow();
  const filterOptions = useFilterOptions();
  const [skuModalOpen, setSkuModalOpen] = useState(false);
  const [draftItems, setDraftItems] = useState<string[]>(selectedItems);

  const routeOptions = useMemo(() => {
    const opts = [{ value: "", label: "All routes" }];
    (filterOptions.data?.routes ?? []).forEach((r) =>
      opts.push({ value: r, label: r })
    );
    return opts;
  }, [filterOptions.data]);

  // availableItems is already sorted by VanLoadTab; re-alias for readability
  const sortedItems = availableItems;

  const skuLabel =
    selectedItems.length === 0
      ? "SKUs: All"
      : `SKUs: ${selectedItems.length} selected`;

  const disabled = !routeCode;

  function openSkuModal() {
    setDraftItems(selectedItems);
    setSkuModalOpen(true);
  }

  function toggleDraft(item: string) {
    setDraftItems((prev) =>
      prev.includes(item) ? prev.filter((i) => i !== item) : [...prev, item]
    );
  }

  function applyDraft() {
    setSelectedItems(draftItems);
    setSkuModalOpen(false);
  }

  return (
    <div className="flex flex-wrap items-end gap-4 bg-surface-raised rounded-xl shadow-1 border border-default p-4">
      <Select
        label="Route"
        value={routeCode}
        onChange={setRouteCode}
        options={routeOptions}
        className="min-w-[200px]"
      />

      <DatePicker label="Date" value={date} onChange={setDate} className="min-w-[180px]" />

      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-text-tertiary uppercase tracking-wider">
          SKUs
        </label>
        <Button
          variant="secondary"
          size="md"
          onClick={openSkuModal}
          disabled={disabled}
        >
          {disabled ? "Select a route first" : skuLabel}
        </Button>
      </div>

      <div className="ml-auto flex items-center gap-2">
        {routeCode && (
          <Button variant="ghost" size="sm" onClick={() => setRouteCode("")}>
            &larr; Back to routes
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setSelectedItems([])}
          disabled={selectedItems.length === 0}
        >
          Reset SKUs
        </Button>
      </div>

      <Modal
        open={skuModalOpen}
        onClose={() => setSkuModalOpen(false)}
        title="Select SKUs"
        size="lg"
      >
        <div className="space-y-3">
          <div className="flex items-center justify-between text-sm">
            <span className="text-text-tertiary">
              {draftItems.length} of {sortedItems.length} selected
            </span>
            <div className="flex gap-2">
              <button
                onClick={() => setDraftItems([...sortedItems])}
                className="text-brand-600 hover:text-brand-700 text-caption font-medium"
              >
                Select all
              </button>
              <button
                onClick={() => setDraftItems([])}
                className="text-text-tertiary hover:text-text-secondary text-xs font-medium"
              >
                Clear
              </button>
            </div>
          </div>
          <div className="max-h-96 overflow-auto border border-default rounded-lg divide-y divide-subtle">
            {sortedItems.length === 0 ? (
              <p className="text-sm text-text-tertiary p-4 text-center">
                No items available for this route/date.
              </p>
            ) : (
              sortedItems.map((item) => (
                <label
                  key={item}
                  className="flex items-center gap-3 px-3 py-2 hover:bg-surface-sunken cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={draftItems.includes(item)}
                    onChange={() => toggleDraft(item)}
                    className="rounded border-strong text-brand-600 focus:ring-brand-500"
                  />
                  <span className="text-sm text-text-secondary">{item}</span>
                </label>
              ))
            )}
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onClick={() => setSkuModalOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" onClick={applyDraft}>
              OK
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
