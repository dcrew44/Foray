"use server";

import { revalidatePath } from "next/cache";

import { requireUser } from "@/lib/auth/session";
import { getSettings, type AutonomyMode } from "@/lib/profile/settings";
import { QUEUES } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";
import { SAVED_PREFERENCES_PROMPT } from "@/lib/ranking/brief";
import { activeSearch, createRun, getRun, requestCancel } from "@/lib/runs/runs";
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
  /** Repeat this earlier search: the same request, companies, level and places. */
  againFrom?: string;
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

  const inFlight = await activeSearch(user.id);
  if (inFlight) {
    return {
      ok: false,
      message:
        inFlight.status === "awaiting_approval"
          ? "A search is already waiting on your go-ahead. Answer it on Home first."
          : "A search is already running. It will finish in a minute or two.",
    };
  }

  const settings = await getSettings(user.id);
  const earlier = input.againFrom ? await getRun(user.id, input.againFrom) : null;
  const previous = (earlier?.kind === "search" ? earlier.input : null) as {
    prompt?: string | null;
    hints?: unknown;
  } | null;
  const mode = input.mode ?? earlier?.mode ?? settings.defaultMode;

  const run = await createRun({
    userId: user.id,
    kind: "search",
    mode,
    status: "queued",
    input: {
      prompt: input.prompt?.trim() || previous?.prompt || SAVED_PREFERENCES_PROMPT,
      // The brief is derived again from the same request and hints rather than
      // copied, so a search repeated after a profile edit reads the new one.
      hints: previous?.hints ?? null,
    },
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
