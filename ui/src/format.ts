// Display helpers shared by the components: dates, people and attachment badges. No state and no I/O.
import type { AttachmentInfo, Person } from "./types";

const DAY_FORMAT: Intl.DateTimeFormatOptions = { day: "numeric", month: "short", year: "numeric" };

/** A UTC epoch-seconds timestamp as a local date ("12 Feb 2026" in an English locale); "undated" for null. */
export function formatDate(ts: number | null): string {
  return ts === null ? "undated" : new Date(ts * 1000).toLocaleDateString(undefined, DAY_FORMAT);
}

/** A timestamp as a local date and time, for the message viewer's headers. */
export function formatDateTime(ts: number | null): string {
  return ts === null
    ? "undated"
    : new Date(ts * 1000).toLocaleString(undefined, { ...DAY_FORMAT, hour: "2-digit", minute: "2-digit" });
}

/** The span of a chain: one date when both ends fall on the same day, else "first – last". */
export function formatSpan(first: number | null, last: number | null): string {
  const a = formatDate(first);
  const b = formatDate(last);
  if (first === null && last === null) return a;
  if (first === null) return b;
  if (last === null || a === b) return a;
  return `${a} – ${b}`;
}

/** How long ago `ts` was, coarsely ("just now", "5 min ago", "3 h ago", "2 days ago"); "never" for null. */
export function formatAgo(ts: number | null, now: number = Date.now() / 1000): string {
  if (ts === null) return "never";
  const seconds = Math.max(0, now - ts);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  const days = Math.floor(seconds / 86400);
  return days === 1 ? "1 day ago" : `${days} days ago`;
}

/** A person's display name, falling back to the address. */
export function personName(person: Person): string {
  return person.name.trim() || person.addr;
}

/**
 * The badges an attachment shows: `text recovered`, `OCR pending` and `text not recovered` from its OCR status,
 * `partially embedded` when only part of its text is in the index, and `not indexed` when its text could not be
 * extracted at all (an unsupported or failed extraction).
 */
export function attachmentBadges(attachment: AttachmentInfo): string[] {
  const badges: string[] = [];
  if (attachment.ocr_status === "ok") badges.push("text recovered");
  else if (attachment.ocr_status === "pending") badges.push("OCR pending");
  else if (attachment.ocr_status === "unavailable") badges.push("text not recovered");
  if (attachment.embedded_partial) badges.push("partially embedded");
  if (attachment.extract_status === "unsupported" || attachment.extract_status === "failed") badges.push("not indexed");
  return badges;
}

/** A `YYYY-MM-DD` date input value as epoch seconds: the local day's first second, or its last when `endOfDay`. */
export function dateInputToTs(value: string, endOfDay: boolean): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const [year, month, day] = [Number(match[1]), Number(match[2]) - 1, Number(match[3])];
  const date = endOfDay ? new Date(year, month, day, 23, 59, 59) : new Date(year, month, day);
  return Math.floor(date.getTime() / 1000);
}

/** Epoch seconds as the `YYYY-MM-DD` value of a date input, in local time; "" for null. */
export function tsToDateInput(ts: number | null): string {
  if (ts === null) return "";
  const date = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** An error's message for the status line. */
export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
