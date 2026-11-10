import { describe, expect, it } from "vitest";

import {
  barWidth,
  BUCKET_HINT,
  BUCKET_LABEL,
  lagText,
  laneName,
  rateText,
  recommendationName,
  resumeName,
  SPANS,
  timingNotes,
  type OutcomeSpan,
  type OutcomeTiming,
  type OutcomeTimings,
} from "@/lib/outcomes";

function span(applications: number, medianS: number | null = null): OutcomeSpan {
  return { applications, medianS, p90S: null };
}

function window(over: Partial<OutcomeTiming> = {}): OutcomeTiming {
  return {
    applications: 0,
    byHand: 0,
    postedToSeen: span(0),
    seenToReady: span(0),
    readyToApplied: span(0),
    postedToApplied: span(0),
    ...over,
  };
}

function timings(allTime: Partial<OutcomeTiming>, untimed = 0): OutcomeTimings {
  return { days: 30, p90After: 10, allTime: window(allTime), recent: window(), untimed };
}

describe("outcomes", () => {
  it("leads with the whole wait, then its three spans in the order they happen", () => {
    expect(SPANS.map((item) => item.key)).toEqual([
      "postedToApplied",
      "postedToSeen",
      "seenToReady",
      "readyToApplied",
    ]);
    expect(SPANS.map((item) => item.label)).toEqual([
      "Posted to applied",
      "Posted to seen",
      "Seen to ready",
      "Ready to applied",
    ]);
  });

  it("names every bucket Django sends", () => {
    expect(BUCKET_LABEL).toEqual({
      same_day: "The same day",
      three_days: "Within three days",
      later: "Later",
    });
    expect(Object.keys(BUCKET_HINT)).toEqual(Object.keys(BUCKET_LABEL));
  });

  it("words a span, and shows a dash for one with nothing behind it", () => {
    expect(lagText(3 * 3600)).toBe("3 hours");
    expect(lagText(2 * 86400)).toBe("2 days");
    expect(lagText(0)).toBe("under a minute");
    expect(lagText(null)).toBe("–");
    expect(lagText(undefined)).toBe("–");
  });

  it("gives a rate only when Django sent one", () => {
    expect(rateText(0.3, 10)).toBe("30%");
    expect(rateText(0, 12)).toBe("0%");
    // 1 of 3 is not 33%: under the minimum the counts stand alone.
    expect(rateText(null, 3)).toBe("Too few to say");
    expect(rateText(null, 0)).toBe("–");
    expect(rateText(undefined, 0)).toBe("–");
  });

  it("draws a bar against the largest count, with a sliver for anything at all", () => {
    expect(barWidth(528, 528)).toBe(100);
    expect(barWidth(132, 528)).toBe(25);
    expect(barWidth(1, 528)).toBe(2);
    expect(barWidth(0, 528)).toBe(0);
    expect(barWidth(0, 0)).toBe(0);
    expect(barWidth(5, 0)).toBe(0);
  });

  it("names the groups of each split", () => {
    expect(["strong", "apply", "maybe", "skip", "none"].map(recommendationName)).toEqual([
      "Strong fit",
      "Worth applying",
      "Maybe",
      "Skip",
      "Not reviewed",
    ]);
    expect(["coached", "automated", "none"].map(laneName)).toEqual([
      "Coached",
      "Automated",
      "No lane",
    ]);
    expect(["tailored", "untailored"].map(resumeName)).toEqual([
      "Tailored for the posting",
      "Not tailored",
    ]);
    // A key from a newer backend is shown as it came rather than dropped.
    expect(laneName("express")).toBe("express");
    expect(resumeName("variant-b")).toBe("variant-b");
  });

  it("has nothing to add when every application has all its times", () => {
    expect(timingNotes(timings({ applications: 4, postedToApplied: span(4, 3600) }))).toEqual([]);
    expect(timingNotes(timings({}))).toEqual([]);
  });

  it("says why the timings cover fewer applications than were sent", () => {
    const one = timingNotes(timings({ applications: 3, byHand: 1, postedToApplied: span(2) }, 1));
    expect(one).toHaveLength(3);
    expect(one[0]).toMatch(/^1 application is in Applied because you moved it there by hand/);
    expect(one[1]).toMatch(/^1 application was moved to Applied by hand and has since moved on/);
    expect(one[2]).toMatch(/^1 application went to a posting that does not say when it went up/);

    const many = timingNotes(
      timings({ applications: 1200, byHand: 1100, postedToApplied: span(1197) }, 2),
    );
    expect(many[0]).toMatch(/^1,100 applications are in Applied because you moved them there/);
    expect(many[1]).toMatch(/^2 applications were moved to Applied by hand and have since moved on/);
    expect(many[2]).toMatch(/^3 applications went to postings that do not say/);
  });
});
