import { isoToDmy } from "@/lib/date";

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Auto-formatter: ISO dates -> dd-mm-yyyy; everything else passes through. */
export function fmtAxisDate(value: unknown): string {
  if (typeof value !== "string") return String(value ?? "");
  return ISO_DATE_RE.test(value) ? isoToDmy(value) : value;
}
