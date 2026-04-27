/**
 * Date helpers -- always operate in the user's LOCAL timezone, never UTC.
 *
 * The whole UI displays dates as `dd-mm-yyyy`, but every transport value
 * (URL params, request bodies, React Query keys, backend responses) stays
 * in `yyyy-mm-dd`. These helpers are the only conversion surface, so a
 * future format change means editing one file.
 */

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

/** Add (or subtract) calendar days to a YYYY-MM-DD date string. Returns YYYY-MM-DD. */
export function addDays(dateIso: string, delta: number): string {
  const [y, m, d] = dateIso.split("-").map(Number);
  const dt = new Date(y, (m ?? 1) - 1, d ?? 1);
  dt.setDate(dt.getDate() + delta);
  return toLocalIsoDate(dt);
}

/**
 * Inclusive (start, end) date range ending today, derived from a calendar-day
 * count. Shared helper so any drawer that needs an "X days back through today"
 * window stops re-deriving it (and stops drifting when the math changes).
 */
export function trailingWindow(days: number, today: string = todayIso()): {
  start_date: string;
  end_date: string;
} {
  return {
    start_date: addDays(today, -(Math.max(1, days) - 1)),
    end_date: today,
  };
}

/**
 * Coerce any value the backend or a Date object may hand us into a
 * canonical `YYYY-MM-DD` string. Returns null for nullish/invalid input.
 * Accepts ISO date, ISO datetime ("...T..."), `Date`, or epoch number.
 */
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

/**
 * Display-format a value as `dd-mm-yyyy`. Accepts ISO date, ISO datetime,
 * `Date`, or epoch number. Returns an em dash for nullish or unparseable
 * input so the UI never renders "null"/"undefined"/"Invalid Date".
 */
export function fmtDate(value: unknown): string {
  const iso = toIsoDate(value);
  return iso ? isoToDmy(iso) : MISSING;
}

/**
 * Display-format an ISO datetime as `dd-mm-yyyy HH:mm` in local time.
 * Used for pipeline timestamps and "last refreshed" labels.
 */
export function fmtDateTime(value: unknown): string {
  if (value == null || value === "") return MISSING;
  const dt = value instanceof Date ? value : new Date(value as string | number);
  if (Number.isNaN(dt.getTime())) return MISSING;
  return `${fmtDate(dt)} ${pad2(dt.getHours())}:${pad2(dt.getMinutes())}`;
}

/**
 * Parse a user-typed `dd-mm-yyyy` (also accepts `/` and `.` separators)
 * into canonical ISO `YYYY-MM-DD`. Returns null on invalid input so the
 * caller can revert / show an error.
 */
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
