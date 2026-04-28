import { TOLERANCE_BY_CLASS } from "@/lib/format";

/** Popup body shown by every accuracy InfoBubble. Reads `TOLERANCE_BY_CLASS`
 *  so the table cannot drift from the scoring formula. */
export default function ForecastAccuracyExplanation() {
  const classes: { key: string; label: string; tone: string; description: string }[] = [
    {
      key: "smooth",
      label: "Stable",
      tone: "bg-success-50 text-success-700 border-success-200",
      description: "Regular buyers, low size variability — should be predicted tightly",
    },
    {
      key: "intermittent",
      label: "Sparse",
      tone: "bg-info-50 text-info-700 border-info-200",
      description: "Patterned but with gaps between purchases",
    },
    {
      key: "erratic",
      label: "Swingy",
      tone: "bg-warning-50 text-warning-700 border-warning-200",
      description: "Regular buyers but with large quantity swings",
    },
    {
      key: "lumpy",
      label: "Random",
      tone: "bg-danger-50 text-danger-700 border-danger-200",
      description: "Sparse and swingy — genuinely hard to predict",
    },
  ];

  return (
    <div className="space-y-6">
      {/* Headline */}
      <p className="text-body text-text-secondary leading-relaxed">
        Forecast accuracy measures how well our predictions match actual sales,
        with each item held to a fair standard based on how predictable its
        buying pattern is. <span className="font-medium text-text-primary">
        Misses inside the standard count as &ldquo;good enough&rdquo;; only the
        part beyond it counts as a real error.</span>
      </p>

      {/* Tolerance table */}
      <div>
        <h3 className="text-caption font-semibold uppercase tracking-wider text-text-tertiary mb-2">
          Tolerance by buying pattern
        </h3>
        <div className="border border-default rounded-lg overflow-hidden">
          <table className="w-full text-body">
            <thead className="bg-surface-sunken">
              <tr className="text-left text-caption font-medium text-text-tertiary uppercase tracking-wide">
                <th className="px-4 py-2">Pattern</th>
                <th className="px-4 py-2">Tolerance</th>
                <th className="px-4 py-2">Description</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-subtle">
              {classes.map((c) => (
                <tr key={c.key}>
                  <td className="px-4 py-2.5">
                    <span className={`inline-block px-2 py-0.5 rounded-full border text-caption font-semibold ${c.tone}`}>
                      {c.label}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 font-semibold text-text-primary">
                    ±{Math.round((TOLERANCE_BY_CLASS[c.key] ?? 0.20) * 100)}%
                  </td>
                  <td className="px-4 py-2.5 text-text-secondary">
                    {c.description}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Formula */}
      <div>
        <h3 className="text-caption font-semibold uppercase tracking-wider text-text-tertiary mb-2">
          The formula
        </h3>
        <div className="bg-surface-sunken border border-subtle rounded-lg px-4 py-3 space-y-1.5 text-body text-text-secondary">
          <p>
            For each forecast row where both predicted and sold are above zero:
          </p>
          <p className="font-mono text-caption text-text-primary pl-4">
            real miss = max(0, |predicted − actual| − tolerance × actual)
          </p>
          <p className="pt-1">Then aggregate across all rows in scope:</p>
          <p className="font-mono text-caption text-text-primary pl-4">
            Accuracy = 100% − (sum of real misses ÷ sum of actuals) × 100%
          </p>
        </div>
      </div>

      {/* Worked example */}
      <div>
        <h3 className="text-caption font-semibold uppercase tracking-wider text-text-tertiary mb-2">
          Worked example
        </h3>
        <div className="border border-default rounded-lg overflow-hidden">
          <table className="w-full text-body">
            <thead className="bg-surface-sunken">
              <tr className="text-left text-caption font-medium text-text-tertiary uppercase tracking-wide">
                <th className="px-3 py-2">Item</th>
                <th className="px-3 py-2 text-right">Forecast</th>
                <th className="px-3 py-2 text-right">Sold</th>
                <th className="px-3 py-2 text-right">Tolerance</th>
                <th className="px-3 py-2 text-right">Real miss</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-subtle text-text-secondary">
              <tr>
                <td className="px-3 py-2">Stable</td>
                <td className="px-3 py-2 text-right">10</td>
                <td className="px-3 py-2 text-right">8</td>
                <td className="px-3 py-2 text-right">±0.8</td>
                <td className="px-3 py-2 text-right font-semibold text-text-primary">1.2</td>
              </tr>
              <tr>
                <td className="px-3 py-2">Sparse</td>
                <td className="px-3 py-2 text-right">5</td>
                <td className="px-3 py-2 text-right">7</td>
                <td className="px-3 py-2 text-right">±1.4</td>
                <td className="px-3 py-2 text-right font-semibold text-text-primary">0.6</td>
              </tr>
              <tr>
                <td className="px-3 py-2">Random</td>
                <td className="px-3 py-2 text-right">12</td>
                <td className="px-3 py-2 text-right">8</td>
                <td className="px-3 py-2 text-right">±3.2</td>
                <td className="px-3 py-2 text-right font-semibold text-text-primary">0.8</td>
              </tr>
              <tr className="bg-surface-sunken">
                <td className="px-3 py-2 font-semibold text-text-primary">Total</td>
                <td className="px-3 py-2 text-right font-semibold text-text-primary">27</td>
                <td className="px-3 py-2 text-right font-semibold text-text-primary">23</td>
                <td className="px-3 py-2"></td>
                <td className="px-3 py-2 text-right font-semibold text-text-primary">2.6</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className="text-caption text-text-tertiary mt-2 leading-relaxed">
          Accuracy = 100% − (2.6 ÷ 23) × 100% =&nbsp;
          <span className="font-semibold text-text-primary">88.7%</span>
        </p>
      </div>

      {/* Why */}
      <div className="border-l-3 border-brand-500 bg-brand-50/40 px-4 py-3 rounded-r-lg">
        <p className="text-body text-text-secondary leading-relaxed">
          <span className="font-semibold text-text-primary">Why class-aware?</span>{" "}
          Stable items can be predicted tightly, so we hold the model to a
          high standard. Random items are inherently unpredictable; punishing
          the model for missing them precisely would hide its real performance.
          This formula is fair to each item's nature and gives one honest
          number for the whole portfolio.
        </p>
      </div>
    </div>
  );
}
