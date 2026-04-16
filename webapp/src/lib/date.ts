/**
 * Date helpers -- always operate in the user's LOCAL timezone, never UTC.
 *
 * `new Date().toISOString()` gives UTC, which is off-by-one for users in
 * non-UTC zones (notably Asia/Dubai before 04:00 local). All "today" / window
 * math in the app must go through these helpers so dates stay consistent with
 * what the supervisor sees on their wall clock.
 */

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
