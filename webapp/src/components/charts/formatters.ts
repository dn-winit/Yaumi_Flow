import { isoToDmy } from "@/lib/date";

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Auto-formatter for chart X-axis ticks and tooltip labels. Recognises
 * canonical ISO `YYYY-MM-DD` strings (the shape every backend endpoint
 * returns for date columns) and reformats to display `dd-mm-yyyy`.
 * Anything else passes through untouched -- so item codes, route
 * codes, and category names render exactly as the data ships them.
 */
export function fmtAxisDate(value: unknown): string {
  if (typeof value !== "string") return String(value ?? "");
  return ISO_DATE_RE.test(value) ? isoToDmy(value) : value;
}
