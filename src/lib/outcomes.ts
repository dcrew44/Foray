import type { components } from "@/lib/api/schema";
import {
  formatLag,
  formatPercent,
  isRecommendation,
  NOT_REVIEWED,
  RECOMMENDATION_LABEL,
} from "@/lib/format";
import { isLane, LANE_LABEL } from "@/lib/lanes";

/**
 * The Outcomes page (#65, #6): how long applications took to go out and what came of them.
 * Django counts, and decides when a number rests on too few to be a rate
 * (`apps/applications/outcomes.py`); these helpers only word what it sent. Pure.
 */
type S = components["schemas"];
export type OutcomesPage = S["OutcomesPage"];
export type OutcomeTimings = S["OutcomeTimings"];
export type OutcomeTiming = S["OutcomeTiming"];
export type OutcomeSpan = S["OutcomeSpan"];
export type OutcomeSpeed = S["OutcomeSpeed"];
export type OutcomeBucket = S["OutcomeBucket"];
export type OutcomeFunnel = S["OutcomeFunnel"];
export type OutcomeStep = S["OutcomeStep"];
export type OutcomeGroup = S["OutcomeGroup"];

export type SpanKey = "postedToApplied" | "postedToSeen" | "seenToReady" | "readyToApplied";

/** The whole wait first, then the three spans it is made of, in the order they happen. */
export const SPANS: { key: SpanKey; label: string; hint: string }[] = [
  {
    key: "postedToApplied",
    label: "Posted to applied",
    hint: "From the posting going up to your application going out",
  },
  {
    key: "postedToSeen",
    label: "Posted to seen",
    hint: "Until Foray first saw the posting",
  },
  {
    key: "seenToReady",
    label: "Seen to ready",
    hint: "Until the application was prepared, with every required answer",
  },
  {
    key: "readyToApplied",
    label: "Ready to applied",
    hint: "Until you sent it",
  },
];

export const BUCKET_LABEL: Record<OutcomeBucket["bucket"], string> = {
  same_day: "The same day",
  three_days: "Within three days",
  later: "Later",
};

export const BUCKET_HINT: Record<OutcomeBucket["bucket"], string> = {
  same_day: "Under 24 hours after it was posted",
  three_days: "24 to 72 hours after",
  later: "More than 72 hours after",
};

/** What stands in for a number there is nothing to give. */
export const NO_NUMBER = "–";
export const TOO_FEW = "Too few to say";

/** A median or a 90th percentile in words, or a dash with nothing to measure. */
export function lagText(seconds: number | null | undefined): string {
  return seconds == null ? NO_NUMBER : formatLag(seconds);
}

/**
 * What a rate's cell says. Django sends no rate for a count under its minimum, so the cell
 * says so rather than turning 1 of 3 into 33%; with nothing counted there is nothing to say.
 */
export function rateText(rate: number | null | undefined, outOf: number): string {
  if (rate != null) return formatPercent(rate);
  return outOf > 0 ? TOO_FEW : NO_NUMBER;
}

/**
 * How wide a count's bar is beside the largest, as a percentage. Anything that is not nothing
 * gets a sliver, so one application among five hundred matches can still be seen.
 */
export function barWidth(count: number, most: number): number {
  if (count <= 0 || most <= 0) return 0;
  return Math.min(100, Math.max(2, (count / most) * 100));
}

/** A recommendation group's name; "none" and anything unread is a match with no review. */
export function recommendationName(key: string): string {
  return isRecommendation(key) ? RECOMMENDATION_LABEL[key] : NOT_REVIEWED;
}

export function laneName(key: string): string {
  return isLane(key) ? LANE_LABEL[key] : key;
}

const RESUME_LABEL: Record<string, string> = {
  tailored: "Tailored for the posting",
  untailored: "Not tailored",
};

export function resumeName(key: string): string {
  return RESUME_LABEL[key] ?? key;
}

/**
 * Why the timings cover fewer applications than were sent, one sentence for each reason that
 * applies: moved by hand, moved by hand before its time was kept, or sent to a posting with
 * no date.
 */
export function timingNotes(timing: OutcomeTimings): string[] {
  const all = timing.allTime;
  const undated = all.applications - all.postedToApplied.applications;
  const notes: string[] = [];
  if (all.byHand === 1) {
    notes.push(
      "1 application was moved to Applied by hand, not marked sent on the Apply page. It is timed from when you moved it and has no ready time.",
    );
  } else if (all.byHand > 1) {
    notes.push(
      `${all.byHand.toLocaleString()} applications were moved to Applied by hand, not marked sent on the Apply page. They are timed from when you moved them and have no ready time.`,
    );
  }
  if (timing.untimed === 1) {
    notes.push(
      "1 application was moved to Applied by hand and had moved on before Foray kept when a match reaches Applied (November 2026), so it is counted and not timed.",
    );
  } else if (timing.untimed > 1) {
    notes.push(
      `${timing.untimed.toLocaleString()} applications were moved to Applied by hand and had moved on before Foray kept when a match reaches Applied (November 2026), so they are counted and not timed.`,
    );
  }
  if (undated === 1) {
    notes.push(
      "1 application went to a posting that does not say when it went up, so it has no posted time.",
    );
  } else if (undated > 1) {
    notes.push(
      `${undated.toLocaleString()} applications went to postings that do not say when they went up, so they have no posted time.`,
    );
  }
  return notes;
}
