"use server";

import { revalidatePath } from "next/cache";
import { z } from "zod";

import { requireUser } from "@/lib/auth/session";
import {
  ALL_STAGES,
  getMatchRows,
  setMatchStage,
  updateMatch,
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
 * The matches board and job page's actions.
 *
 * Everything arrives from the browser, so ids and values are validated rather
 * than trusted, and every query is scoped to the signed-in user — a match id
 * belonging to someone else reads exactly like one that does not exist.
 */

type Result = { ok: true; message: string } | { ok: false; message: string };

const STAGE_LABELS: Record<Stage, string> = {
  found: "Found",
  shortlisted: "Shortlisted",
  applied: "Applied",
  interviewing: "Interviewing",
  offer: "Offer",
  rejected: "Rejected",
  archived: "Archived",
};

const MatchId = z.uuid();

export async function moveMatchStageAction(
  matchId: string,
  stage: Stage,
): Promise<Result> {
  const user = await requireUser();

  if (!MatchId.safeParse(matchId).success || !ALL_STAGES.includes(stage)) {
    return { ok: false, message: "That move could not be read." };
  }

  const moved = await setMatchStage(user.id, matchId, stage);
  if (!moved) return { ok: false, message: "That match no longer exists." };

  revalidate();

  const [row] = await getMatchRows(user.id, [matchId]);
  return {
    ok: true,
    message: row
      ? `Moved ${row.title} at ${row.company} to ${STAGE_LABELS[stage]}.`
      : `Moved to ${STAGE_LABELS[stage]}.`,
  };
}

const MatchPatch = z.object({
  notes: z.string().max(20_000).nullable().optional(),
  /**
   * The UI decides the scale (thumbs as -1/1, or stars as 1–5); this only
   * keeps it a small integer.
   */
  userRating: z.number().int().min(-10).max(10).nullable().optional(),
});

export async function updateMatchAction(
  matchId: string,
  patch: { notes?: string | null; userRating?: number | null },
): Promise<Result> {
  const user = await requireUser();

  const parsed = MatchPatch.safeParse(patch);
  if (!MatchId.safeParse(matchId).success || !parsed.success) {
    return { ok: false, message: "That change could not be read." };
  }
  if (parsed.data.notes === undefined && parsed.data.userRating === undefined) {
    return { ok: true, message: "Nothing to change." };
  }

  const updated = await updateMatch(user.id, matchId, parsed.data);
  if (!updated) return { ok: false, message: "That match no longer exists." };

  revalidate();
  return { ok: true, message: "Saved." };
}

/**
 * Queues a deep evaluation of one match. Returns the run id at once — the
 * evaluation takes tens of seconds, which is not a request's to wait for.
 */
export async function evaluateMatchAction(
  matchId: string,
): Promise<
  { ok: true; runId: string; message: string } | { ok: false; message: string }
> {
  const user = await requireUser();

  if (!MatchId.safeParse(matchId).success) {
    return { ok: false, message: "That match does not exist." };
  }

  try {
    const { run, reused, matches } = await startMatchEvaluation(user.id, [
      matchId,
    ]);
    revalidate();

    const [match] = matches;
    const label = `${match.title} at ${match.company}`;
    const cost = costPhrase(estimateEvalCost(1).perItemUsd);

    return {
      ok: true,
      runId: run.id,
      message: reused
        ? `Already evaluating ${label}.`
        : `Evaluating ${label}${cost ? ` (${cost})` : ""}. This takes about half a minute.`,
    };
  } catch (error) {
    if (error instanceof NoResumeError) {
      return { ok: false, message: error.message };
    }
    if (error instanceof UnknownMatchError) {
      return { ok: false, message: "That match does not exist." };
    }
    console.error("[matches] evaluate failed:", error);
    return {
      ok: false,
      message: "Could not start the evaluation just now. Try again in a moment.",
    };
  }
}

function revalidate() {
  revalidatePath("/matches", "layout");
  revalidatePath("/home");
}
