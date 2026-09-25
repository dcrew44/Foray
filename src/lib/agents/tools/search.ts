import { tool } from "ai";
import { eq } from "drizzle-orm";
import { z } from "zod";

import { db } from "@/lib/db";
import { jobs } from "@/lib/db/schema";
import {
  ALL_STAGES,
  getMatchByJob,
  listMatches,
  resolveMatch,
  type MatchRow,
} from "@/lib/db/queries/matches";
import { getSettings } from "@/lib/profile/settings";
import { QUEUES } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";
import {
  activeSearch,
  createRun,
  getRun,
  listRuns,
  requestCancel,
} from "@/lib/runs/runs";
import {
  assertCanSearch,
  PreferencesIncompleteError,
} from "@/lib/runs/search-run";

/**
 * Chat's handles on searching.
 *
 * `start_search_run` refuses rather than searching on incomplete preferences:
 * a search without work authorisation or location produces a list that looks
 * plausible and is mostly unusable, and the user cannot tell by looking.
 */

export function startSearchRun(userId: string, conversationId?: string | null) {
  return tool({
    description:
      "Start a background search for roles matching the user's profile and " +
      "preferences: refresh job boards, filter, score every candidate, then " +
      "deep-evaluate the best. Returns a run id immediately; the chat shows a " +
      "live card for it. Refuses if essential preferences are not yet known.",
    inputSchema: z.object({
      prompt: z
        .string()
        .describe("What the user asked for, in their own words."),
      mode: z
        .enum(["approve_each", "autopilot"])
        .nullable()
        .describe(
          "approve_each pauses before deep evaluation and before shortlisting; " +
            "autopilot does both without asking. Null uses the user's default " +
            "from settings — only set it when they say which they want.",
        ),
    }),
    execute: async ({ prompt, mode }) => {
      try {
        await assertCanSearch(userId);
      } catch (error) {
        if (error instanceof PreferencesIncompleteError) {
          return {
            ok: false as const,
            needs: error.missing,
            message:
              `Cannot search yet — still need: ${error.missing.join(", ")}. ` +
              "Ask for those first, then start the search.",
          };
        }
        throw error;
      }

      const inFlight = await activeSearch(userId);
      if (inFlight) {
        // Returned with its id so the chat shows the live card for it.
        return {
          ok: true as const,
          runId: inFlight.id,
          mode: inFlight.mode,
          alreadyRunning: true,
          message:
            inFlight.status === "awaiting_approval"
              ? "A search is already waiting on the user's go-ahead; this is its card. Do not start another."
              : "A search is already running; this is its card. Do not start another.",
        };
      }

      const settings = await getSettings(userId);
      const resolvedMode = mode ?? settings.defaultMode;

      const run = await createRun({
        userId,
        kind: "search",
        mode: resolvedMode,
        status: "queued",
        input: { prompt },
        conversationId: conversationId ?? null,
      });

      await send(QUEUES.search, { userId, runId: run.id });

      return {
        ok: true as const,
        runId: run.id,
        mode: resolvedMode,
        message:
          resolvedMode === "autopilot"
            ? "Search started on autopilot. It will score, evaluate and shortlist without stopping."
            : "Search started. It will pause for approval before the deep evaluation and before shortlisting.",
      };
    },
  });
}

export function getRunStatus(userId: string) {
  return tool({
    description:
      "Check a background run: its status, progress counters and result. " +
      "Pass null for run_id to get the most recent run.",
    inputSchema: z.object({ run_id: z.string().nullable() }),
    execute: async ({ run_id }) => {
      const run = run_id
        ? await getRun(userId, run_id)
        : (await listRuns(userId, 1))[0];

      if (!run) return { ok: false as const, message: "No runs yet." };

      return {
        ok: true as const,
        runId: run.id,
        kind: run.kind,
        status: run.status,
        mode: run.mode,
        stats: run.stats,
        output: run.output,
        error: run.error,
      };
    },
  });
}

export function cancelRun(userId: string) {
  return tool({
    description: "Stop a running background run. Work already done is kept.",
    inputSchema: z.object({ run_id: z.string() }),
    execute: async ({ run_id }) => {
      const run = await requestCancel(userId, run_id);
      return run
        ? { ok: true as const, message: "Cancelled. Everything found so far was kept." }
        : { ok: false as const, message: "That run is not running." };
    },
  });
}

/** The compact shape every job-listing tool returns, so the UI renders one card. */
export function jobCard(match: MatchRow) {
  return {
    matchId: match.matchId,
    jobId: match.jobId,
    title: match.title,
    company: match.company,
    locations: match.locations,
    remote: match.remote,
    stage: match.stage,
    score: match.score,
    fastScore: match.fastScore,
    deepScore: match.deepScore,
    recommendation: match.recommendation,
    headline: match.headline,
    salary: formatSalary(match),
    postedAt: match.postedAt,
    url: match.url,
  };
}

export type JobCard = ReturnType<typeof jobCard>;

function formatSalary(match: Pick<MatchRow, "salaryMin" | "salaryMax" | "salaryCurrency" | "salaryPeriod">) {
  if (!match.salaryMin && !match.salaryMax) return null;
  const fmt = (n: number) =>
    n >= 1000 ? `${Math.round(n / 1000)}k` : String(Math.round(n));
  const range =
    match.salaryMin && match.salaryMax && match.salaryMin !== match.salaryMax
      ? `${fmt(match.salaryMin)}–${fmt(match.salaryMax)}`
      : fmt((match.salaryMin ?? match.salaryMax)!);
  const currency = !match.salaryCurrency || match.salaryCurrency === "USD" ? "$" : `${match.salaryCurrency} `;
  return `${currency}${range}${match.salaryPeriod && match.salaryPeriod !== "year" ? `/${match.salaryPeriod}` : ""}`;
}

export function listJobs(userId: string) {
  return tool({
    description:
      "List the user's matched roles, best first (deep score, else fast score). " +
      "Filter by pipeline stage, a minimum score, or text in the title/company. " +
      "By default excludes rejected and archived matches.",
    inputSchema: z.object({
      limit: z.number().int().min(1).max(50).nullable(),
      stage: z.enum(ALL_STAGES as [string, ...string[]]).nullable(),
      min_score: z.number().int().min(0).max(100).nullable(),
      query: z.string().nullable().describe("Text to match in title or company."),
    }),
    execute: async ({ limit, stage, min_score, query }) => {
      const matches = await listMatches(userId, {
        limit: limit ?? 10,
        stages: stage
          ? [stage as MatchRow["stage"]]
          : ["found", "shortlisted", "applied", "interviewing", "offer"],
        minScore: min_score ?? undefined,
        query: query ?? undefined,
      });

      return { count: matches.length, jobs: matches.map(jobCard) };
    },
  });
}

export function getJob(userId: string) {
  return tool({
    description:
      "Read one job posting in full — its cleaned description, plus this " +
      "user's match for it (stage, scores, and the deep evaluation's " +
      "evidence and gaps when there is one). Accepts a match id or a job id.",
    inputSchema: z.object({ id: z.string().describe("A matchId or jobId.") }),
    execute: async ({ id }) => {
      const match = await resolveMatch(userId, id);
      const jobId = match?.jobId ?? id;

      const [job] = await db
        .select()
        .from(jobs)
        .where(eq(jobs.id, jobId))
        .limit(1);

      if (!job) return { ok: false as const, message: "No such job." };

      const own = match ?? (await getMatchByJob(userId, job.id));

      return {
        ok: true as const,
        matchId: own?.id ?? null,
        jobId: job.id,
        title: job.title,
        company: job.companyName,
        locations: job.locations,
        remote: job.remote,
        salary:
          job.salaryMin || job.salaryMax
            ? `${job.salaryCurrency ?? ""} ${job.salaryMin ?? "?"}–${job.salaryMax ?? "?"} per ${job.salaryPeriod ?? "?"}`.trim()
            : null,
        postedAt: job.postedAt,
        isOpen: job.isActive,
        url: job.url,
        applyUrl: job.applyUrl,
        match: own
          ? {
              stage: own.stage,
              fastScore: own.fastScore,
              fastEval: own.fastEval,
              deepScore: own.deepScore,
              deepEval: own.deepEval,
              notes: own.notes,
              tailoredResumeId: own.tailoredResumeId,
            }
          : null,
        // Truncated: a chat turn does not need 20,000 characters.
        description: job.descriptionText?.slice(0, 8000) ?? null,
      };
    },
  });
}
