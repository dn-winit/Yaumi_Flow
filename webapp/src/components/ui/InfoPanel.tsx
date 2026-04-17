import { useState } from "react";
import Modal from "./Modal";

export interface ProcessStep {
  title: string;
  description: string;
  highlights?: string[];
}

interface InfoPanelProps {
  title: string;
  subtitle: string;
  steps: ProcessStep[];
}

export function InfoButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="inline-flex items-center justify-center w-6 h-6 rounded-full border border-default text-text-tertiary hover:text-brand-600 hover:border-brand-300 transition-colors"
      title="How this works"
      aria-label="How this works"
    >
      <span className="text-caption font-semibold leading-none">i</span>
    </button>
  );
}

export default function InfoPanel({ title, subtitle, steps }: InfoPanelProps) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <InfoButton onClick={() => setOpen(true)} />
      <Modal open={open} onClose={() => setOpen(false)} title={title} size="lg">
        <div className="space-y-1">
          <p className="text-body text-text-secondary mb-6">{subtitle}</p>

          <div className="relative pl-8">
            {steps.map((step, i) => {
              const isLast = i === steps.length - 1;
              return (
                <div key={step.title} className="relative pb-6">
                  {!isLast && (
                    <div className="absolute left-[-21px] top-4 bottom-0 w-px bg-brand-100" />
                  )}
                  <div className="absolute left-[-25px] top-0.5 flex items-center justify-center w-[18px] h-[18px] rounded-full bg-brand-600 text-white text-caption font-bold leading-none">
                    {i + 1}
                  </div>
                  <div>
                    <h4 className="text-body font-semibold text-text-primary">{step.title}</h4>
                    <p className="text-caption text-text-secondary mt-0.5 leading-relaxed">
                      {step.description}
                    </p>
                    {step.highlights && step.highlights.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mt-2">
                        {step.highlights.map((h) => (
                          <span
                            key={h}
                            className="inline-block text-caption px-2 py-0.5 rounded-full bg-brand-50 text-brand-700 border border-brand-100"
                          >
                            {h}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </Modal>
    </>
  );
}
