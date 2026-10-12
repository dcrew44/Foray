import type { LiveEvent, LiveRun, RunSummary } from "@/lib/api/types";

/**
 * A run as the activity page lists it (`GET /api/pages/activity`): the live shape, plus what
 * Django looked up for it — what the run was about (`target`: "Senior Backend Engineer at
 * Life360") and what it cost (`costUsd`, null for a run the live feed added after the page
 * loaded).
 */
export type { RunSummary };

/** What each kind of run is called, on run cards and in the activity list. */
export const KIND_TITLE: Record<LiveRun["kind"], string> = {
  search: "Search",
  rank_deep: "Deep review",
  tailor: "Tailored resume",
  resume_render: "Resume PDF",
  company_discovery: "Company discovery",
  company_refresh: "Board refresh",
  profile_import: "Profile import",
};

export const STATUS: Record<
  LiveRun["status"],
  { label: string; tone: "signal" | "ok" | "error" | "idle"; pulse?: boolean }
> = {
  queued: { label: "Queued", tone: "idle" },
  running: { label: "Running", tone: "signal", pulse: true },
  awaiting_approval: { label: "Waiting on you", tone: "signal" },
  completed: { label: "Done", tone: "ok" },
  failed: { label: "Failed", tone: "error" },
  cancelled: { label: "Stopped", tone: "idle" },
};

/** The same mapping Home uses, so a level looks the same everywhere. */
export function eventTone(event: Pick<LiveEvent, "level" | "kind">): "signal" | "ok" | "error" | "idle" {
  if (event.level === "error") return "error";
  if (event.level === "warn" || event.kind === "approval.requested") return "signal";
  if (event.level === "debug") return "idle";
  return "ok";
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** One or two numbers that say what a finished run produced. */
export function runOutcome(run: LiveRun): string | null {
  const output = run.output ?? {};
  const stats = run.stats ?? {};
  const pick = (key: string) => num(output[key]) ?? num(stats[key]);

  switch (run.kind) {
    case "search": {
      const scored = pick("scored");
      const strong = pick("strong");
      const shortlisted = pick("shortlisted");
      const parts = [
        scored != null && `${scored} scored`,
        shortlisted ? `${shortlisted} shortlisted` : strong ? `${strong} strong` : null,
      ].filter(Boolean);
      return parts.length ? parts.join(", ") : null;
    }
    case "rank_deep": {
      const evaluated = pick("evaluated");
      return evaluated != null ? `${evaluated} reviewed` : null;
    }
    case "tailor": {
      if (run.status !== "completed") return null;
      const unsupported = pick("unsupported") ?? 0;
      return unsupported > 0
        ? `${unsupported} ${unsupported === 1 ? "claim" : "claims"} to check`
        : "Every claim checked";
    }
    case "resume_render": {
      const pages = pick("pages");
      return pages != null ? `${pages} ${pages === 1 ? "page" : "pages"}` : null;
    }
    case "company_refresh": {
      const ok = pick("ok");
      const boards = pick("boards");
      return ok != null && boards != null ? `${ok} of ${boards} boards` : null;
    }
    default:
      return null;
  }
}
