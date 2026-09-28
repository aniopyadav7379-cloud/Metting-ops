/**
 * Filename → meeting metadata parser (TypeScript twin).
 *
 * Mirrors `backend/utils/filename_parser.py` exactly. Used at single-file
 * upload time so the new-session form can show a preview ("This looks like
 * a meeting from 2024-11-05 with Jason Allen — use these values?") before
 * the user commits. The backend re-parses on its own at upload finalize
 * and uses the user's overrides where provided, so a frontend hiccup
 * here never poisons stored metadata.
 *
 * Patterns, in priority order:
 *   1. Mac Notes + Voice Memos export    confidence 1.0
 *      {notes|downloads}__YYYY-MM-DD_HHMMSS__{title}.{ext}
 *   2. ISO date prefix with " - "        confidence 0.9
 *      YYYY-MM-DD - Title.ext
 *   3. ISO prefix loose separator        confidence 0.8
 *      YYYY-MM-DD_Title.ext / YYYY-MM-DD Title.ext
 *   4. US date suffix M-D-YY[YY]         confidence 0.7
 *      Title - M-D-YY.ext / Title M-D-YYYY.ext
 *   5. ISO date buried in basename       confidence 0.5
 *   no match → confidence 0.0
 */

export type FilenameSource = "notes" | "downloads" | "generic" | null;

export interface ParsedFilename {
  /** Cleaned-up display title, or null if the basename was empty. */
  title: string | null;
  /** ISO 8601 YYYY-MM-DD when a valid calendar date was extracted. */
  meetingDate: string | null;
  /** 24h HH:MM:SS when the filename carried a time component. */
  meetingTime: string | null;
  /** Which pattern's source label this came from. */
  source: FilenameSource;
  /** 0.0–1.0 — which pattern matched. */
  confidence: number;
  /** Echo of the original input for traceability. */
  rawFilename: string;
}

const PATTERN_NOTES_EXPORT =
  /^(?<source>notes|downloads)__(?<date>\d{4}-\d{2}-\d{2})_(?<time>\d{6})__(?<title>.+?)\.(?<ext>[A-Za-z0-9]{1,8})$/;
const PATTERN_ISO_PREFIX_DASH = /^(?<date>\d{4}-\d{2}-\d{2})\s*-\s*(?<title>.+?)$/;
const PATTERN_ISO_PREFIX_LOOSE = /^(?<date>\d{4}-\d{2}-\d{2})[ _](?<title>.+?)$/;
const PATTERN_US_DATE_SUFFIX =
  /^(?<title>.+?)(?:\s*[-_]\s*|\s+)(?<month>\d{1,2})[-/](?<day>\d{1,2})[-/](?<year>\d{2}|\d{4})$/;
const PATTERN_ISO_ANYWHERE = /(\d{4})-(\d{2})-(\d{2})/;

/** Strip directory prefixes — works for forward and back slashes. */
function basenameOf(input: string): string {
  if (!input) return "";
  const lastSlash = Math.max(input.lastIndexOf("/"), input.lastIndexOf("\\"));
  return lastSlash >= 0 ? input.slice(lastSlash + 1) : input;
}

/** Strip the last `.ext` from a filename (preserves dots inside the name). */
function stemOf(name: string): string {
  if (!name) return "";
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(0, dot) : name;
}

function isValidDate(y: number, m: number, d: number): boolean {
  if (m < 1 || m > 12 || d < 1 || d > 31) return false;
  const dt = new Date(Date.UTC(y, m - 1, d));
  return (
    dt.getUTCFullYear() === y &&
    dt.getUTCMonth() === m - 1 &&
    dt.getUTCDate() === d
  );
}

function isValidTime(h: number, m: number, s: number): boolean {
  return h >= 0 && h < 24 && m >= 0 && m < 60 && s >= 0 && s < 60;
}

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

function isoDate(y: number, m: number, d: number): string {
  return `${y}-${pad2(m)}-${pad2(d)}`;
}

function isoTime(h: number, m: number, s: number): string {
  return `${pad2(h)}:${pad2(m)}:${pad2(s)}`;
}

function normalizeTitle(raw: string): string {
  if (!raw) return "";
  let cleaned = raw.replace(/_/g, " ");
  cleaned = cleaned.replace(/\s+/g, " ").trim();
  // Strip leading/trailing punctuation we never want in a meeting title.
  cleaned = cleaned.replace(/^[\s\-._]+|[\s\-._]+$/g, "");
  return cleaned.slice(0, 200);
}

function expandTwoDigitYear(yearRaw: string): number {
  const n = parseInt(yearRaw, 10);
  if (yearRaw.length === 2) {
    return n >= 70 ? 1900 + n : 2000 + n;
  }
  return n;
}

/**
 * Parse `filename` into title / meeting date / meeting time / source.
 *
 * Never throws. confidence = 0.0 means no pattern matched and the
 * caller should treat `meetingDate` / `meetingTime` as unknown.
 */
export function parseFilename(filename: string): ParsedFilename {
  const raw = filename || "";
  const basename = basenameOf(raw);

  // Pattern 1 — owns the .ext group so it's checked on the full basename.
  const m1 = basename.match(PATTERN_NOTES_EXPORT);
  if (m1 && m1.groups) {
    const dateStr = m1.groups.date;
    const timeStr = m1.groups.time;
    const y = parseInt(dateStr.slice(0, 4), 10);
    const mo = parseInt(dateStr.slice(5, 7), 10);
    const d = parseInt(dateStr.slice(8, 10), 10);
    const hh = parseInt(timeStr.slice(0, 2), 10);
    const mm = parseInt(timeStr.slice(2, 4), 10);
    const ss = parseInt(timeStr.slice(4, 6), 10);

    return {
      title: normalizeTitle(m1.groups.title) || null,
      meetingDate: isValidDate(y, mo, d) ? isoDate(y, mo, d) : null,
      meetingTime: isValidTime(hh, mm, ss) ? isoTime(hh, mm, ss) : null,
      source: m1.groups.source as "notes" | "downloads",
      confidence: 1.0,
      rawFilename: raw,
    };
  }

  // Patterns 2-5 operate on the stem (no extension).
  const stem = stemOf(basename);

  const m2 = stem.match(PATTERN_ISO_PREFIX_DASH);
  if (m2 && m2.groups) {
    const y = parseInt(m2.groups.date.slice(0, 4), 10);
    const mo = parseInt(m2.groups.date.slice(5, 7), 10);
    const d = parseInt(m2.groups.date.slice(8, 10), 10);
    if (isValidDate(y, mo, d)) {
      return {
        title: normalizeTitle(m2.groups.title) || null,
        meetingDate: isoDate(y, mo, d),
        meetingTime: null,
        source: "generic",
        confidence: 0.9,
        rawFilename: raw,
      };
    }
  }

  const m3 = stem.match(PATTERN_ISO_PREFIX_LOOSE);
  if (m3 && m3.groups) {
    const y = parseInt(m3.groups.date.slice(0, 4), 10);
    const mo = parseInt(m3.groups.date.slice(5, 7), 10);
    const d = parseInt(m3.groups.date.slice(8, 10), 10);
    if (isValidDate(y, mo, d)) {
      return {
        title: normalizeTitle(m3.groups.title) || null,
        meetingDate: isoDate(y, mo, d),
        meetingTime: null,
        source: "generic",
        confidence: 0.8,
        rawFilename: raw,
      };
    }
  }

  const m4 = stem.match(PATTERN_US_DATE_SUFFIX);
  if (m4 && m4.groups) {
    const y = expandTwoDigitYear(m4.groups.year);
    const mo = parseInt(m4.groups.month, 10);
    const d = parseInt(m4.groups.day, 10);
    if (isValidDate(y, mo, d)) {
      return {
        title: normalizeTitle(m4.groups.title) || null,
        meetingDate: isoDate(y, mo, d),
        meetingTime: null,
        source: "generic",
        confidence: 0.7,
        rawFilename: raw,
      };
    }
  }

  const m5 = stem.match(PATTERN_ISO_ANYWHERE);
  if (m5) {
    const y = parseInt(m5[1], 10);
    const mo = parseInt(m5[2], 10);
    const d = parseInt(m5[3], 10);
    if (isValidDate(y, mo, d)) {
      // Recover a title by removing the matched date from the stem.
      const before = stem.slice(0, m5.index!);
      const after = stem.slice(m5.index! + m5[0].length);
      const titleRaw = (before + " " + after).trim();
      const title = normalizeTitle(titleRaw) || normalizeTitle(stem);
      return {
        title: title || null,
        meetingDate: isoDate(y, mo, d),
        meetingTime: null,
        source: "generic",
        confidence: 0.5,
        rawFilename: raw,
      };
    }
  }

  // No pattern matched — best-effort title from the cleaned stem.
  const fallback = normalizeTitle(stem);
  return {
    title: fallback || null,
    meetingDate: null,
    meetingTime: null,
    source: null,
    confidence: 0.0,
    rawFilename: raw,
  };
}
