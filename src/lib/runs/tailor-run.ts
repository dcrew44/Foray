import { and, desc, eq, gt, inArray, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { jobs, runs, type Run } from "@/lib/db/schema";
import {
  ensureMatch,
  getMatchDetail,
  resolveMatch,
} from "@/lib/db/queries/matches";
import {
  getBaseResume,
  getProfile,
  loadBaseResume,
} from "@/lib/db/queries/profile";
import { getResume, saveTailoredResume } from "@/lib/db/queries/resumes";
import { PreferencesSchema } from "@/lib/profile/interview";
import { QUEUES } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";
import { tailorResume } from "@/lib/resume/tailor";
import {
  finalizeVerification,
  reconcileChanges,
  reviseTailored,
  verifyTailored,
  type Revision,
  type Verification,
} from "@/lib/resume/verify";

import { emit } from "./events";
import {
  createRun,
  finishRun,
  isCancelled,
  mergeStats,
  startRun,
} from "./runs";

/**
 * One tailoring run: draft, verify, revise, save.
 *
 * Like the search run, a pipeline with checkpoints rather than an agent loop.
 * The draft is never saved as-is: it is verified against the base resume, the
 * unsupported parts are reverted, and only then does a `resumes(tailored)` row
 * exist. Rendering the PDF happens afterwards in the worker handler, because
 * only the worker may load a browser — a failed render never loses the
 * verified resume.
 *
 * Also here: starting a tailor or render run, shared by the server actions and
 * the chat tool so both refuse, deduplicate and word things the same way.
 */

export type TailorRunInput = {
  matchId: string;
  /** Shown on the run card and in the activity feed. */
  prompt?: string | null;
};

class TailorError extends Error {}

/** What the handler needs to render and finish the run. */
export type TailoredResult = {
  resumeId: string;
  matchId: string;
  /** "Senior Backend Engineer at Stripe" */
  label: string;
  changes: number;
  unsupported: number;
};

export async function executeTailorRun(run: Run): Promise<TailoredResult | null> {
  const userId = run.userId;
  const input = (run.input ?? {}) as Partial<TailorRunInput>;

  await startRun(run.id);

  try {
    if (!input.matchId) throw new TailorError("This tailoring run has no job attached.");

    const detail = await getMatchDetail(userId, input.matchId);
    if (!detail) throw new TailorError("That job is no longer among your matches.");

    const base = await loadBaseResume(userId);
    if (!base) {
      throw new TailorError(
        "There is no resume on file to tailor. Upload one on your profile first.",
      );
    }

    const label = `${detail.job.title} at ${detail.job.companyName}`;
    const context = { userId, runId: run.id };

    await mergeStats(run.id, {
      phase: "tailoring",
      matchId: detail.match.id,
      title: detail.job.title,
      company: detail.job.companyName,
      baseVersion: base.version,
    });
    await emit({
      userId,
      runId: run.id,
      kind: "tailor.started",
      message: `Tailoring your resume for ${label}.`,
      data: { matchId: detail.match.id },
    });

    // 1. The draft.
    const profile = await getProfile(userId);
    const draft = await tailorResume({
      base: base.resume,
      job: detail.job,
      match: detail.match,
      preferences: PreferencesSchema.safeParse(profile?.preferences).data ?? null,
      context,
    });

    if (draft.resume.work.length === 0 && base.resume.work.length > 0) {
      throw new TailorError(
        "The tailored draft came back without any of your experience, so it was discarded. Try again.",
      );
    }
    if (await isCancelled(run.id)) return cancelled(run);

    // 2. Verify every line against the base resume.
    await mergeStats(run.id, { phase: "verifying" });
    const result = await verifyTailored(base.resume, draft.resume, draft.changes, context);

    // Anything the mapping refused (an item or skill that is not on the base
    // resume) was already removed; it belongs in the record all the same.
    const revisions: Revision[] = draft.dropped.map((drop) => ({
      ...drop,
      source: "rule",
      action: "dropped",
      replacement: null,
    }));

    // 3. One deterministic revision of whatever failed.
    let final = draft.resume;
    if (result.findings.length > 0) {
      await mergeStats(run.id, { phase: "revising" });
      const revised = reviseTailored(base.resume, draft.resume, result.findings);
      final = revised.resume;
      revisions.push(...revised.revisions);
    }

    const verification = finalizeVerification({
      base: base.resume,
      final,
      result,
      revisions,
      baseResumeId: base.id,
      baseVersion: base.version,
    });
    const changes = reconcileChanges({
      base: base.resume,
      final,
      changes: draft.changes,
      revisions,
    });

    if (await isCancelled(run.id)) return cancelled(run);

    // 4. Save, and make it the match's current tailored resume.
    const saved = await saveTailoredResume(userId, {
      matchId: detail.match.id,
      resume: final,
      changes,
      verification,
    });

    await mergeStats(run.id, {
      resumeId: saved.id,
      checked: verification.checked,
      revisedCount: revisions.length,
      unsupported: verification.unsupported.length,
      changes: changes.length,
    });
    await emit({
      userId,
      runId: run.id,
      kind: "tailor.verified",
      level: verification.passed ? "info" : "warn",
      message: verifiedMessage(verification),
      data: {
        resumeId: saved.id,
        checked: verification.checked,
        modelChecked: verification.modelChecked,
        revised: revisions.length,
        unsupported: verification.unsupported.length,
        passed: verification.passed,
      },
    });

    return {
      resumeId: saved.id,
      matchId: detail.match.id,
      label,
      changes: changes.length,
      unsupported: verification.unsupported.length,
    };
  } catch (error) {
    // A run the user cancelled stays cancelled, whatever failed after.
    if (await isCancelled(run.id)) return null;

    const message =
      error instanceof TailorError
        ? error.message
        : `Tailoring failed: ${error instanceof Error ? error.message : String(error)}`;
    console.error(`[tailor] run ${run.id} failed:`, error);

    await finishRun(run.id, { status: "failed", error: message });
    await emit({
      userId,
      runId: run.id,
      kind: "tailor.failed",
      level: "error",
      message,
    });
    return null;
  }
}

function verifiedMessage(verification: Verification): string {
  const lines = (count: number) => `${count} ${count === 1 ? "line" : "lines"}`;
  const fixed = verification.revisions.length;
  const open = verification.unsupported.length;

  const parts = [`Checked ${lines(verification.checked)} against your resume.`];
  if (fixed === 0 && open === 0) {
    parts.push("Every one is supported by what you wrote.");
  } else if (fixed > 0) {
    parts.push(
      `${fixed} ${fixed === 1 ? "change was" : "changes were"} not backed by your resume and ${
        fixed === 1 ? "was" : "were"
      } put back to your original wording or removed.`,
    );
  }
  if (open > 0) {
    parts.push(`${lines(open)} still ${open === 1 ? "needs" : "need"} your review before you send it.`);
  }
  if (verification.modelError) {
    parts.push("The line-by-line check was unavailable, so rewrites kept your original wording.");
  }
  return parts.join(" ");
}

async function cancelled(run: Run): Promise<null> {
  await emit({
    userId: run.userId,
    runId: run.id,
    kind: "run.cancelled",
    level: "warn",
    message: "Tailoring cancelled. Nothing was saved.",
  });
  return null;
}

// --- Starting runs -------------------------------------------------------------

export type StartTailorResult =
  | { ok: true; runId: string; matchId: string; message: string }
  | { ok: false; message: string };

/** A tailor run already in flight for this match, so a double click pays once. */
async function activeTailorRun(userId: string, matchId: string): Promise<Run | null> {
  const [run] = await db
    .select()
    .from(runs)
    .where(
      and(
        eq(runs.userId, userId),
        eq(runs.kind, "tailor"),
        inArray(runs.status, ["queued", "running"]),
        sql`${runs.input}->>'matchId' = ${matchId}`,
        // A run stuck longer than this (a worker that died mid-job) must not
        // block a retry forever.
        gt(runs.createdAt, new Date(Date.now() - 15 * 60 * 1000)),
      ),
    )
    .orderBy(desc(runs.createdAt))
    .limit(1);
  return run ?? null;
}

/**
 * Starts tailoring for a match. `id` may be a match id or a job id — from chat
 * it is often the latter — and a job the user has no match for yet gets one.
 */
export async function startTailorRun(
  userId: string,
  id: string,
  options: { conversationId?: string | null } = {},
): Promise<StartTailorResult> {
  if (!(await getBaseResume(userId))) {
    return {
      ok: false,
      message:
        "There is no resume on file to tailor. Upload one on your profile first.",
    };
  }

  const wanted = id.trim();
  let match = wanted ? await resolveMatch(userId, wanted) : null;
  if (!match && wanted) {
    const [job] = await db
      .select({ id: jobs.id })
      .from(jobs)
      .where(eq(jobs.id, wanted))
      .limit(1);
    if (job) match = await ensureMatch(userId, job.id);
  }
  const detail = match ? await getMatchDetail(userId, match.id) : null;
  if (!detail) return { ok: false, message: "I couldn't find that job." };

  const label = `${detail.job.title} at ${detail.job.companyName}`;

  const active = await activeTailorRun(userId, detail.match.id);
  if (active) {
    return {
      ok: true,
      runId: active.id,
      matchId: detail.match.id,
      message: `Already tailoring your resume for ${label}. It will be ready in about a minute.`,
    };
  }

  const run = await createRun({
    userId,
    kind: "tailor",
    mode: "approve_each",
    status: "queued",
    input: { matchId: detail.match.id, prompt: `Tailor your resume for ${label}` },
    conversationId: options.conversationId ?? null,
  });

  try {
    await send(QUEUES.resumeTailor, {
      userId,
      runId: run.id,
      matchId: detail.match.id,
    });
  } catch (error) {
    console.error("[tailor] could not enqueue:", error);
    await finishRun(run.id, { status: "failed", error: "Could not queue the job." });
    return { ok: false, message: "Could not start tailoring right now. Try again in a moment." };
  }

  return {
    ok: true,
    runId: run.id,
    matchId: detail.match.id,
    message:
      `Tailoring your resume for ${label}. It takes about a minute, and every ` +
      "change is checked against your original resume before it is saved.",
  };
}

export type StartRenderResult =
  | { ok: true; runId: string; message: string }
  | { ok: false; message: string };

/** Starts rendering a stored resume (base or tailored) to PDF. */
export async function startResumeRender(
  userId: string,
  resumeId: string,
  options: { conversationId?: string | null } = {},
): Promise<StartRenderResult> {
  const resume = await getResume(userId, resumeId);
  if (!resume) return { ok: false, message: "That resume no longer exists." };

  const run = await createRun({
    userId,
    kind: "resume_render",
    mode: "approve_each",
    status: "queued",
    input: {
      resumeId,
      prompt:
        resume.kind === "base"
          ? "Render your resume to PDF"
          : "Render your tailored resume to PDF",
    },
    conversationId: options.conversationId ?? null,
  });

  try {
    await send(QUEUES.resumeRender, { userId, runId: run.id, resumeId });
  } catch (error) {
    console.error("[render] could not enqueue:", error);
    await finishRun(run.id, { status: "failed", error: "Could not queue the job." });
    return { ok: false, message: "Could not start rendering right now. Try again in a moment." };
  }

  return {
    ok: true,
    runId: run.id,
    message: "Rendering your resume to PDF. It takes a few seconds.",
  };
}
