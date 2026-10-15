/** Time formatting. Pure: `now`, locale and zone are parameters so tests are exact. */

export interface FormatOptions {
  now?: Date;
  locale?: string;
  timeZone?: string;
}

/**
 * One parser for every backend timestamp. A value without an offset (a
 * SQLite-backed server sends them) is UTC, never the viewer's local time.
 */
export function parseMoment(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  // The backend sends UTC; SQLite-backed deployments may omit the offset.
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const date = new Date(hasZone ? iso : `${iso}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "13 Oct 2026, 15:43" in the viewer's locale, or "unknown" for a bad value. */
export function formatAbsolute(iso: string | null | undefined, options: FormatOptions = {}): string {
  const date = parseMoment(iso);
  if (!date) return "unknown";
  return new Intl.DateTimeFormat(options.locale, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: options.timeZone,
  }).format(date);
}

/** "5 minutes ago", "in 2 hours", "just now". Falls back to "unknown". */
export function formatRelative(iso: string | null | undefined, options: FormatOptions = {}): string {
  const date = parseMoment(iso);
  if (!date) return "unknown";
  const now = options.now ?? new Date();
  const seconds = Math.round((date.getTime() - now.getTime()) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return "just now";
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["day", 86_400],
    ["hour", 3_600],
    ["minute", 60],
  ];
  const formatter = new Intl.RelativeTimeFormat(options.locale, { numeric: "auto" });
  for (const [unit, size] of units) {
    if (abs >= size || unit === "minute") {
      return formatter.format(Math.round(seconds / size), unit);
    }
  }
  return "just now";
}

/** "every 15 minutes", "every 2 hours", "every day". */
export function formatInterval(seconds: unknown): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) {
    return "unknown interval";
  }
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  if (seconds % 86_400 === 0) return seconds === 86_400 ? "every day" : `every ${plural(seconds / 86_400, "day")}`;
  if (seconds % 3_600 === 0) return seconds === 3_600 ? "every hour" : `every ${plural(seconds / 3_600, "hour")}`;
  if (seconds % 60 === 0) return `every ${plural(seconds / 60, "minute")}`;
  return `every ${plural(seconds, "second")}`;
}
