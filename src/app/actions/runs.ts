"use server";

import { revalidatePath } from "next/cache";

import { requireUser } from "@/lib/auth/session";
import { getSettings, type AutonomyMode } from "@/lib/profile/settings";
import { QUEUES } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";
import { createRun, requestCancel } from "@/lib/runs/runs";
import {
  assertCanSearch,
  PreferencesIncompleteError,
} from "@/lib/runs/search-run";

/**
 * Starting and stopping runs from buttons rather than from chat. Both paths
 * end in the same run row and queue, so the activity feed cannot tell which
 * one started a search — and should not need to.
 */

export async function startSearchAction(input: {
  prompt?: string;
  mode?: AutonomyMode;
} = {}): Promise<
  { ok: true; runId: string; message: string } | { ok: false; message: string }
> {
  const user = await requireUser();

  try {
    await assertCanSearch(user.id);
  } catch (error) {
    if (error instanceof PreferencesIncompleteError) {
      return {
        ok: false,
        message: `Foray still needs your ${error.missing.join(", ").toLowerCase()} before it can search. Add them on your profile or tell Foray in chat.`,
      };
    }
    throw error;
  }

  const settings = await getSettings(user.id);
  const mode = input.mode ?? settings.defaultMode;

  const run = await createRun({
    userId: user.id,
    kind: "search",
    mode,
    status: "queued",
    input: { prompt: input.prompt?.trim() || "Search with my saved preferences" },
  });

  await send(QUEUES.search, { userId: user.id, runId: run.id });
  revalidatePath("/home");

  return {
    ok: true,
    runId: run.id,
    message:
      mode === "autopilot"
        ? "Search started on autopilot."
        : "Search started. Foray will check with you before the deep evaluation.",
  };
}

export async function cancelRunAction(
  runId: string,
): Promise<{ ok: boolean; message: string }> {
  const user = await requireUser();
  const run = await requestCancel(user.id, runId);
  revalidatePath("/home");
  revalidatePath("/activity");
  return run
    ? { ok: true, message: "Stopped. Everything found so far was kept." }
    : { ok: false, message: "That run had already finished." };
}
