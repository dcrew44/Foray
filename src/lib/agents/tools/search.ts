import { tool } from "ai";
import { eq } from "drizzle-orm";
import { z } from "zod";

import { db } from "@/lib/db";
import { jobs } from "@/lib/db/schema";
import { QUEUES } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";
import { createRun, getRun, listRuns, requestCancel } from "@/lib/runs/runs";
import {
  assertCanSearch,
  listMatches,
  PreferencesIncompleteError,
} from "@/lib/runs/search-run";

/**
 * Chat's handles on searching.
 *
 * `start_search_run` refuses rather than searching on incomplete preferences:
 * a search without work authorisation or location produces a list that looks
 * plausible and is mostly unusable, and the user cannot tell by looking.
 */

export function startSearchRun(userId: string) {
  return tool({
    description:
      "Start a background search for roles matching the user's profile and " +
      "preferences. Returns a run id immediately; progress appears in the " +
      "activity feed. Refuses if essential preferences are not yet known.",
    inputSchema: z.object({
      prompt: z
        .string()
        .describe("What the user asked for, in their own words."),
      mode: z
        .enum(["approve_each", "autopilot"])
        .describe("approve_each pauses for decisions; autopilot does not."),
    }),
    execute: async ({ prompt, mode }) => {
      try {
        await assertCanSearch(userId);
      } catch (error) {
        if (error instanceof PreferencesIncompleteError) {
          return {
            ok: false,
            needs: error.missing,
            message:
              `Cannot search yet — still need: ${error.missing.join(", ")}. ` +
              "Ask for those first, then start the search.",
          };
        }
        throw error;
      }

      const run = await createRun({
        userId,
        kind: "search",
        mode,
        status: "queued",
        input: { prompt },
      });

      await send(QUEUES.search, { userId, runId: run.id });

      return {
        ok: true,
        runId: run.id,
        mode,
        message: "Search started. Progress will appear in the activity feed.",
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

      if (!run) return { ok: false, message: "No runs yet." };

      return {
        ok: true,
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
        ? { ok: true, message: "Cancelled. Everything found so far was kept." }
        : { ok: false, message: "That run is not running." };
    },
  });
}

export function listJobs(userId: string) {
  return tool({
    description:
      "List the user's matched roles, best first. Optionally filter by " +
      "pipeline stage.",
    inputSchema: z.object({
      limit: z.number().int().min(1).max(50).nullable(),
      stage: z
        .enum([
          "found",
          "shortlisted",
          "applied",
          "interviewing",
          "offer",
          "rejected",
          "archived",
        ])
        .nullable(),
    }),
    execute: async ({ limit, stage }) => {
      const matches = await listMatches(userId, {
        limit: limit ?? 20,
        stage: stage ?? undefined,
      });

      return {
        count: matches.length,
        jobs: matches.map((match) => ({
          jobId: match.jobId,
          title: match.title,
          company: match.company,
          locations: match.locations,
          remote: match.remote,
          stage: match.stage,
          fastScore: match.fastScore,
          deepScore: match.deepScore,
          salary:
            match.salaryMin && match.salaryMax
              ? `${match.salaryMin}–${match.salaryMax}`
              : null,
          url: match.url,
        })),
      };
    },
  });
}

export function getJob() {
  return tool({
    description:
      "Read one job posting in full, including its cleaned description.",
    inputSchema: z.object({ job_id: z.string() }),
    execute: async ({ job_id }) => {
      const [job] = await db
        .select()
        .from(jobs)
        .where(eq(jobs.id, job_id))
        .limit(1);

      if (!job) return { ok: false, message: "No such job." };

      return {
        ok: true,
        title: job.title,
        company: job.companyName,
        locations: job.locations,
        remote: job.remote,
        salary:
          job.salaryMin && job.salaryMax
            ? `${job.salaryCurrency ?? ""}${job.salaryMin}–${job.salaryMax}/${job.salaryPeriod ?? "?"}`
            : null,
        postedAt: job.postedAt,
        url: job.url,
        applyUrl: job.applyUrl,
        // Truncated: a chat turn does not need 20,000 characters, and the
        // scoring pass reads the full text from the database anyway.
        description: job.descriptionText?.slice(0, 6000) ?? null,
      };
    },
  });
}
