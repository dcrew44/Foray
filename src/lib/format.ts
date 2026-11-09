/**
 * Presentation helpers shared by server pages, client components and chat
 * cards, so a salary or a date reads the same everywhere it appears.
 * Pure functions only — safe to import from anywhere.
 */

export type StageId =
  | "found"
  | "shortlisted"
  | "applied"
  | "interviewing"
  | "offer"
  | "rejected"
  | "archived";

export const STAGE_LABEL: Record<StageId, string> = {
  found: "Found",
  shortlisted: "Shortlisted",
  applied: "Applied",
  interviewing: "Interviewing",
  offer: "Offer",
  rejected: "Rejected",
  archived: "Archived",
};

export const STAGE_HINT: Record<StageId, string> = {
  found: "New matches from searches, best first",
  shortlisted: "Worth applying to",
  applied: "Application sent",
  interviewing: "In conversation",
  offer: "Offer in hand",
  rejected: "Not moving forward",
  archived: "Set aside",
};

export type RecommendationId = "strong" | "apply" | "maybe" | "skip";

export const RECOMMENDATION_LABEL: Record<RecommendationId, string> = {
  strong: "Strong fit",
  apply: "Worth applying",
  maybe: "Maybe",
  skip: "Skip",
};

/** What a match says in place of a verdict while it has only its triage score. */
export const NOT_REVIEWED = "Not reviewed";

export function isRecommendation(value: string | null | undefined): value is RecommendationId {
  return value != null && Object.hasOwn(RECOMMENDATION_LABEL, value);
}

/**
 * The verdict a match leads with, and the only words that name a fit: the deep review's
 * recommendation, or that there is no review yet.
 */
export function verdictLabel(recommendation: string | null | undefined): string {
  return isRecommendation(recommendation) ? RECOMMENDATION_LABEL[recommendation] : NOT_REVIEWED;
}

/**
 * Which step of the pine scale a score sits on. 0 means unscored.
 *
 * The steps are the bands the two rubrics score in (`RUBRIC` in
 * `backend/apps/matching/deep_eval.py` and `fast_score.py`), so a chip's green and the verdict
 * beside it agree. A deep review's bands are its recommendations: strong from 85, apply from
 * 70, maybe from 50, skip below. Triage gives no verdict and has five bands: 85, 70, 55, 30.
 */
export function fitLevel(
  score: number | null | undefined,
  reviewed = false,
): 0 | 1 | 2 | 3 | 4 | 5 {
  if (score == null) return 0;
  if (score >= 85) return 5;
  if (score >= 70) return 4;
  if (reviewed) return score >= 50 ? 3 : 1;
  if (score >= 55) return 3;
  return score >= 30 ? 2 : 1;
}

/** The pine step each verdict sits on: `fitLevel` of every reviewed score in its band. */
export const RECOMMENDATION_LEVEL: Record<RecommendationId, 1 | 3 | 4 | 5> = {
  strong: 5,
  apply: 4,
  maybe: 3,
  skip: 1,
};

/**
 * "7 of 9 requirements met (2 partly), 1 learnable gap": how much of what the posting asks a
 * deep review found in the resume. The total is every requirement the review weighed, met or
 * not; a partial match counts as met and is said. Null when the review listed none.
 */
export function requirementsLine(evaluation: {
  matched_requirements?: { strength?: string }[] | null;
  gaps?: { severity?: string }[] | null;
}): string | null {
  const met = evaluation.matched_requirements ?? [];
  const gaps = evaluation.gaps ?? [];
  const total = met.length + gaps.length;
  if (total === 0) return null;

  const partly = met.filter((item) => item.strength === "partial").length;
  const blockers = gaps.filter((gap) => gap.severity === "disqualifying").length;
  const learnable = gaps.length - blockers;
  return [
    `${met.length} of ${plural(total, "requirement")} met${partly ? ` (${partly} partly)` : ""}`,
    gaps.length === 0 && "no gaps",
    learnable > 0 && plural(learnable, "learnable gap"),
    blockers > 0 && plural(blockers, "blocker"),
  ]
    .filter(Boolean)
    .join(", ");
}

/**
 * Models sometimes leave the resume's bracketed fact ids in prose meant for
 * people ("… dropped events by 94% [3d98…]"). They are for the machine.
 */
export function stripFactIds(text: string | null | undefined): string {
  return (text ?? "").replace(/\s*\[[0-9a-f]{8}-[0-9a-f-]{27,}\]/gi, "").trim();
}

/**
 * What a review's summary goes on to say after the sentence the match leads with, so a page
 * showing both never says that sentence twice. The whole summary when it does not open with it.
 */
export function summaryRest(
  summary: string | null | undefined,
  reason: string | null | undefined,
): string {
  const text = (summary ?? "").replace(/\s+/g, " ").trim();
  const lead = (reason ?? "").replace(/\s+/g, " ").trim();
  return lead && text.startsWith(lead) ? text.slice(lead.length).trim() : text;
}

export function formatSalary(input: {
  salaryMin?: number | string | null;
  salaryMax?: number | string | null;
  salaryCurrency?: string | null;
  salaryPeriod?: string | null;
}): string | null {
  const min = input.salaryMin != null ? Number(input.salaryMin) : null;
  const max = input.salaryMax != null ? Number(input.salaryMax) : null;
  if (!min && !max) return null;

  const period = input.salaryPeriod ?? "year";
  const compact = (n: number) => {
    if (period === "hour") return n.toFixed(n % 1 ? 2 : 0);
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
    if (n >= 1000) return `${Math.round(n / 1000)}k`;
    return String(Math.round(n));
  };

  const currency = input.salaryCurrency ?? "USD";
  const symbol =
    currency === "USD"
      ? "$"
      : currency === "EUR"
        ? "€"
        : currency === "GBP"
          ? "£"
          : `${currency} `;

  const range =
    min && max && min !== max
      ? `${symbol}${compact(min)}–${compact(max)}`
      : `${symbol}${compact((min ?? max)!)}`;

  const suffix =
    period === "hour" ? " an hour" : period === "month" ? " a month" : "";

  return `${range}${suffix}`;
}

const REMOTE_LABEL: Record<string, string> = {
  remote: "Remote",
  hybrid: "Hybrid",
  onsite: "On-site",
};

/** "San Francisco, CA + 2 more" or "Remote", from the posting's own strings. */
export function formatLocation(
  locations: string[] | null | undefined,
  remote?: string | null,
): string {
  const list = (locations ?? []).filter(Boolean);
  const remoteLabel = remote ? REMOTE_LABEL[remote] : undefined;

  if (list.length === 0) return remoteLabel ?? "Location not stated";

  const first = list[0]!;
  const more = list.length > 1 ? ` + ${list.length - 1} more` : "";
  const alreadySaysRemote = /remote/i.test(first);
  return `${first}${more}${remoteLabel && !alreadySaysRemote && remote !== "onsite" ? `, ${remoteLabel.toLowerCase()}` : ""}`;
}

export function remoteLabel(remote: string | null | undefined): string | null {
  return remote ? (REMOTE_LABEL[remote] ?? null) : null;
}

/** "just now", "5m ago", "3h ago", "2d ago", then a short date. */
export function timeAgo(value: Date | string | null | undefined, now = Date.now()): string {
  if (!value) return "";
  const at = typeof value === "string" ? new Date(value) : value;
  const seconds = Math.max(0, Math.round((now - at.getTime()) / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days}d ago`;
  return at.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(at.getFullYear() !== new Date(now).getFullYear() ? { year: "numeric" } : {}),
  });
}

/** "Posted 3d ago" — or nothing when the source gave no date. */
export function postedLabel(value: Date | string | null | undefined): string | null {
  if (!value) return null;
  return `Posted ${timeAgo(value)}`;
}

export function formatUsd(value: number, options: { precise?: boolean } = {}): string {
  if (value === 0) return "$0";
  if (value < 0.01) return "<$0.01";
  return `$${value.toFixed(options.precise || value < 10 ? 2 : 0)}`;
}

export function formatDuration(ms: number): string {
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

/**
 * A lag in words, coarse on purpose: "under a minute", "9 minutes", "2 hours", "3 days".
 * Rounded to the nearest unit, since nobody acts on the seconds of a quarter-hour wait.
 */
export function formatLag(seconds: number): string {
  if (seconds < 60) return "under a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return plural(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (hours < 36) return plural(hours, "hour");
  return plural(Math.round(hours / 24), "day");
}

/** A stored time of day ("22:00", "08:30") as people say it: "10:00 pm", "8:30 am". */
export function clockLabel(value: string): string {
  const [hours = 0, minutes = 0] = value.split(":").map(Number);
  const hour = hours % 12 === 0 ? 12 : hours % 12;
  return `${hour}:${String(minutes).padStart(2, "0")} ${hours < 12 ? "am" : "pm"}`;
}

/** Every half hour of the day as "HH:MM", plus `extra` in its place when it is not one. */
export function clockOptions(extra?: string): string[] {
  const options = Array.from(
    { length: 48 },
    (_, index) => `${String(Math.floor(index / 2)).padStart(2, "0")}:${index % 2 ? "30" : "00"}`,
  );
  if (extra && /^\d{2}:\d{2}$/.test(extra) && !options.includes(extra)) options.push(extra);
  return options.sort();
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

/** Initials for a company or person monogram. */
export function initials(name: string | null | undefined): string {
  const words = (name ?? "")
    .replace(/[^\p{L}\p{N}\s&.-]/gu, " ")
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0]!.slice(0, 2).toUpperCase();
  return (words[0]![0]! + words[1]![0]!).toUpperCase();
}

/** A stable hue for a name, so a company keeps its colour across the app. */
export function nameHue(name: string | null | undefined): number {
  let hash = 0;
  for (const char of name ?? "") hash = (hash * 31 + char.charCodeAt(0)) | 0;
  return Math.abs(hash) % 360;
}
