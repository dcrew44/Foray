import { tool } from "ai";
import { z } from "zod";

import {
  ALL_STAGES,
  appendMatchNote,
  getMatchRows,
  resolveOrCreateMatch,
  setMatchStage,
  type Stage,
} from "@/lib/db/queries/matches";
import {
  NoResumeError,
  costPhrase,
  estimateEvalCost,
} from "@/lib/ranking/deep-eval";
import {
  UnknownMatchError,
  startMatchEvaluation,
} from "@/lib/runs/evaluate-run";

/**
 * Chat's handles on individual roles: moving one through the pipeline, and
 * asking for a deep evaluation of one.
 *
 * Both accept a match id or a job id. The agent sees both in tool output, and
 * a posting the user found themselves (a job with no match yet) is as valid a
 * target as one a search surfaced — the match is created on the way.
 */

export function moveJobStage(userId: string) {
  return tool({
    description:
      "Move one of the user's roles to a pipeline stage — shortlisted, " +
      "applied, interviewing, offer, rejected (they or the company said no), " +
      "archived (not interested), or back to found. Use when they say they " +
      "applied, got an interview, want to shortlist or drop a role. Accepts a " +
      "matchId or jobId. Optionally appends a note to the role's notes.",
    inputSchema: z.object({
      id: z.string().describe("A matchId or jobId."),
      stage: z.enum(ALL_STAGES as [Stage, ...Stage[]]),
      note: z
        .string()
        .nullable()
        .describe(
          "A short note to add to the role's notes (e.g. who they spoke to), or null.",
        ),
    }),
    execute: async ({ id, stage, note }) => {
      const match = await resolveOrCreateMatch(userId, id.trim());
      if (!match) {
        return {
          ok: false,
          matchId: null,
          stage: null,
          message: "No role with that id.",
        };
      }

      await setMatchStage(userId, match.id, stage);
      if (note?.trim()) await appendMatchNote(userId, match.id, note);

      const [row] = await getMatchRows(userId, [match.id]);
      const label = row ? `${row.title} at ${row.company}` : "That role";

      return {
        ok: true,
        matchId: match.id,
        stage,
        message:
          `Moved ${label} to ${stage}.` + (note?.trim() ? " Note added." : ""),
      };
    },
  });
}

export function evaluateJob(userId: string, conversationId?: string | null) {
  return tool({
    description:
      "Deep-evaluate one role against the user's full resume with the strong " +
      "model: a 0–100 score, a recommendation (strong / apply / maybe / " +
      "skip), the requirements they meet with evidence from their resume, " +
      "gaps, seniority/location/salary fit, risks and talking points. Costs a " +
      "few cents and runs in the background for about half a minute; the " +
      "chat shows a live card, and get_job returns the result once it is " +
      "done. Accepts a matchId or jobId. If get_job already shows a deep " +
      "evaluation, only re-run it when the user asks or their resume changed.",
    inputSchema: z.object({
      id: z.string().describe("A matchId or jobId."),
    }),
    execute: async ({ id }) => {
      const match = await resolveOrCreateMatch(userId, id.trim());
      if (!match) {
        return { ok: false, matchId: null, message: "No role with that id." };
      }

      try {
        const { run, reused, matches } = await startMatchEvaluation(
          userId,
          [match.id],
          { conversationId },
        );

        const [row] = matches;
        const label = `${row.title} at ${row.company}`;
        const cost = costPhrase(estimateEvalCost(1).perItemUsd);

        return {
          ok: true,
          runId: run.id,
          matchId: match.id,
          message: reused
            ? `${label} is already being evaluated; the card shows its progress.`
            : `Evaluating ${label} in the background${cost ? ` (${cost})` : ""}. ` +
              "The card shows progress; the result is saved on the role.",
        };
      } catch (error) {
        if (error instanceof NoResumeError || error instanceof UnknownMatchError) {
          return { ok: false, matchId: match.id, message: error.message };
        }
        throw error;
      }
    },
  });
}
