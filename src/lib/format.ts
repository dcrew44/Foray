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

/** Which step of the pine scale a score sits on. 0 means unscored. */
export function fitLevel(score: number | null | undefined): 0 | 1 | 2 | 3 | 4 | 5 {
  if (score == null) return 0;
  if (score >= 90) return 5;
  if (score >= 80) return 4;
  if (score >= 70) return 3;
  if (score >= 55) return 2;
  return 1;
}

export function fitWord(score: number | null | undefined): string {
  switch (fitLevel(score)) {
    case 5:
      return "Excellent fit";
    case 4:
      return "Strong fit";
    case 3:
      return "Good fit";
    case 2:
      return "Possible fit";
    case 1:
      return "Weak fit";
    default:
      return "Not scored yet";
  }
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
