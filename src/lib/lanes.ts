import type { components } from "@/lib/api/schema";
import type { StageId } from "@/lib/format";

/**
 * The apply lane of a match, in words. Django decides it for every row and for the match page
 * (`apps/matching/lanes.py`: the user's own choice, else what the deep review recommends); this
 * only names it and builds the Matches page's addresses. Pure, so anything may import it.
 */
export type Lane = components["schemas"]["MatchRow"]["lane"];

export const LANE_LABEL: Record<Lane, string> = {
  coached: "Coached",
  automated: "Automated",
  none: "No lane",
};

/** Every lane, in the order a picker lists them. */
export const LANES = Object.keys(LANE_LABEL) as Lane[];

/** What a lane means for whoever applies; a match in no lane has nothing to say. */
export const LANE_HINT: Record<Lane, string | null> = {
  coached: "Foray coaches, you write and send",
  automated: "Foray fills the form; you press submit",
  none: null,
};

export function isLane(value: string | null | undefined): value is Lane {
  return value != null && Object.hasOwn(LANE_LABEL, value);
}

/**
 * What a row says beside its verdict: the lane's name, and nothing for a match in no lane
 * (most matches, which a label on every row would only repeat).
 */
export function laneLabel(lane: string | null | undefined): string | null {
  return isLane(lane) && lane !== "none" ? LANE_LABEL[lane] : null;
}

/**
 * What the match page says beside its lane picker: what the lane asks of the user, and that it
 * is their own choice when they made one. "Foray coaches, you write and send. Your choice."
 * Null when there is nothing to add to the lane's name.
 */
export function laneNote(lane: string | null | undefined, chosen = false): string | null {
  if (!isLane(lane)) return null;
  const hint = LANE_HINT[lane];
  return [hint && `${hint}.`, chosen && "Your choice."].filter(Boolean).join(" ") || null;
}

/**
 * The Matches page's address for a view and its filters, as Django reads them
 * (`GET /api/pages/matches`): a stage lands on the list, `closed` adds the closed columns to
 * the board, and a lane narrows either view.
 */
export function matchesHref({
  view = "board",
  stage = null,
  closed = false,
  lane = null,
}: {
  view?: "board" | "list";
  stage?: StageId | null;
  closed?: boolean;
  lane?: Lane | null;
} = {}): string {
  const query = new URLSearchParams();
  if (stage) query.set("stage", stage);
  else if (view === "list") query.set("view", "list");
  if (closed && view === "board" && !stage) query.set("closed", "1");
  if (lane) query.set("lane", lane);
  return query.size > 0 ? `/matches?${query.toString()}` : "/matches";
}
