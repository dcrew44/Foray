"use server";

import { revalidatePath } from "next/cache";

import { requireUser } from "@/lib/auth/session";
import { getBaseResume } from "@/lib/db/queries/profile";
import { startResumeRender, startTailorRun } from "@/lib/runs/tailor-run";

/**
 * Tailoring and rendering, from the UI.
 *
 * Both only enqueue: tailoring is a strong-model call plus verification and a
 * PDF render, far too slow for a request. The returned run id is what the page
 * follows for progress. Expected failures (no resume yet, unknown job) come
 * back as `{ ok: false, message }` rather than throwing.
 */

type StartResult =
  | { ok: true; runId: string; message: string }
  | { ok: false; message: string };

function revalidate() {
  revalidatePath("/profile");
  revalidatePath("/matches", "layout");
  revalidatePath("/home");
}

export async function tailorResumeAction(matchId: string): Promise<StartResult> {
  const user = await requireUser();

  if (typeof matchId !== "string" || !matchId.trim()) {
    return { ok: false, message: "No job was given to tailor for." };
  }

  const result = await startTailorRun(user.id, matchId);
  if (!result.ok) return result;

  revalidate();
  return { ok: true, runId: result.runId, message: result.message };
}

/** Renders the latest base resume to PDF. */
export async function renderBaseResumeAction(): Promise<StartResult> {
  const user = await requireUser();

  const base = await getBaseResume(user.id);
  if (!base) {
    return {
      ok: false,
      message: "There is no resume on file yet. Upload one first.",
    };
  }

  const result = await startResumeRender(user.id, base.id);
  if (result.ok) revalidate();
  return result;
}
