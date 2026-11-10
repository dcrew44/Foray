import { api, call } from "@/lib/api/client";
import type { StageId } from "@/lib/format";
import type { Lane } from "@/lib/lanes";

/**
 * The match mutations the board, the list and the match page share (`PATCH /api/matches/{id}`).
 * Each answers Django's `{ok, message}` whatever the status (`call()`), so callers branch on
 * `ok` and `router.refresh()` afterwards, as the server actions' `revalidatePath` did.
 */

/**
 * Moves one match to another stage: "Moved <title> at <company> to <Stage>." An Undo passes
 * `enteredAt`, when the match had entered `stage` before the move it undoes, so the match keeps
 * its "found … ago" instead of reading as moved just now.
 */
export function moveMatch(matchId: string, stage: StageId, enteredAt?: string) {
  const body = enteredAt === undefined ? { stage } : { stage, stageChangedAt: enteredAt };
  return call(api.PATCH("/api/matches/{id}", { params: { path: { id: matchId } }, body }));
}

/**
 * Puts a match in the apply lane the user chose; `null` hands it back to the deep review. The
 * answer says where that leaves it: "Moved to the coached lane: Foray coaches, you write and
 * send."
 */
export function setMatchLane(matchId: string, lane: Lane | null) {
  return call(
    api.PATCH("/api/matches/{id}", {
      params: { path: { id: matchId } },
      body: { laneOverride: lane },
    }),
  );
}

/** Replaces a match's private notes; `null` clears them. */
export function saveMatchNotes(matchId: string, notes: string | null) {
  return call(
    api.PATCH("/api/matches/{id}", { params: { path: { id: matchId } }, body: { notes } }),
  );
}
