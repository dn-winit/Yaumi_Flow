/** Local-timezone date helpers. UI = dd-mm-yyyy; transport = yyyy-mm-dd. Single conversion surface. */

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const DMY_DATE_RE = /^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$/;
const MISSING = "\u2014";

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** Format a `Date` (or now) as YYYY-MM-DD in local time. */
function toLocalIsoDate(d: Date = new Date()): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/** "Today" in the user's local timezone, YYYY-MM-DD. */
export function todayIso(): string {
  return toLocalIsoDate();
}

/** Local-time calendar days elapsed; null for unparseable input. */
export function daysSince(value: unknown): number | null {
  if (value == null || value === "") return null;
  const dt = value instanceof Date ? value : new Date(String(value));
  const ms = dt.getTime();
  if (!Number.isFinite(ms)) return null;
  const elapsedMs = Date.now() - ms;
  if (elapsedMs < 0) return 0;
  return Math.floor(elapsedMs / (24 * 60 * 60 * 1000));
}

/** Add (or subtract) calendar days to a YYYY-MM-DD date string. Returns YYYY-MM-DD. */
export function addDays(dateIso: string, delta: number): string {
  const [y, m, d] = dateIso.split("-").map(Number);
  const dt = new Date(y, (m ?? 1) - 1, d ?? 1);
  dt.setDate(dt.getDate() + delta);
  return toLocalIsoDate(dt);
}

/** Inclusive (start, end) window ending today from a day-count; shared so math doesn't drift. */
export function trailingWindow(days: number, today: string = todayIso()): {
  start_date: string;
  end_date: string;
} {
  return {
    start_date: addDays(today, -(Math.max(1, days) - 1)),
    end_date: today,
  };
}

/** Default ReportingPeriod: trailing 30 days ending today. */
export function defaultReportingPeriod(today: string = todayIso()): {
  start_date: string;
  end_date: string;
} {
  return trailingWindow(30, today);
}

/** Coerce any input (ISO date/datetime, Date, epoch) to YYYY-MM-DD; null on invalid. */
function toIsoDate(value: unknown): string | null {
  if (value == null || value === "") return null;
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : toLocalIsoDate(value);
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return toLocalIsoDate(new Date(value));
  }
  if (typeof value === "string") {
    const head = value.slice(0, 10);
    if (ISO_DATE_RE.test(head)) return head;
    const parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) return toLocalIsoDate(parsed);
  }
  return null;
}

/** ISO `YYYY-MM-DD` -> display `dd-mm-yyyy`. */
export function isoToDmy(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${d}-${m}-${y}`;
}

/** Format anything as dd-mm-yyyy; em-dash on null/invalid. */
export function fmtDate(value: unknown): string {
  const iso = toIsoDate(value);
  return iso ? isoToDmy(iso) : MISSING;
}

/** Format ISO datetime as dd-mm-yyyy HH:mm in server TZ so audit ts match host logs. */
const DISPLAY_TZ = "Asia/Kolkata";  // matches backend log_timezone default
const DT_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  timeZone: DISPLAY_TZ,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function fmtDateTime(value: unknown): string {
  if (value == null || value === "") return MISSING;
  const dt = value instanceof Date ? value : new Date(value as string | number);
  if (Number.isNaN(dt.getTime())) return MISSING;
  // Normalise en-GB "/" -> "-" to match the rest of the UI.
  const parts = DT_FORMATTER.formatToParts(dt);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return `${get("day")}-${get("month")}-${get("year")} ${get("hour")}:${get("minute")}`;
}

/** Parse dd-mm-yyyy (and / or . separators) to YYYY-MM-DD; null on invalid. */
export function parseDmy(input: string): string | null {
  const m = input.trim().match(DMY_DATE_RE);
  if (!m) return null;
  const [, dStr, mStr, yStr] = m;
  const d = Number(dStr);
  const mo = Number(mStr);
  const y = Number(yStr);
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
  // Validate by round-tripping through Date (catches Feb 30 etc.).
  const dt = new Date(y, mo - 1, d);
  if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== d) return null;
  return toLocalIsoDate(dt);
}

/** Render a [start, end] ISO range as `dd-mm-yyyy to dd-mm-yyyy`. */
export function fmtDateRange(startIso: string, endIso: string): string {
  return `${fmtDate(startIso)} to ${fmtDate(endIso)}`;
}
