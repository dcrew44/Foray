import { describe, expect, it } from "vitest";

import {
  clockLabel,
  clockOptions,
  fitLevel,
  formatDuration,
  formatLag,
  formatLocation,
  formatPercent,
  formatSalary,
  formatUsd,
  initials,
  isRecommendation,
  nameHue,
  plural,
  RECOMMENDATION_LABEL,
  RECOMMENDATION_LEVEL,
  requirementsLine,
  stripFactIds,
  summaryRest,
  timeAgo,
  verdictLabel,
} from "@/lib/format";
import { clearanceLabel, levelLabel, workAuthLabel, yearsLabel } from "@/lib/job-facts";

describe("formatSalary", () => {
  it("renders a yearly USD range compactly", () => {
    expect(
      formatSalary({ salaryMin: 148000, salaryMax: 216000, salaryCurrency: "USD", salaryPeriod: "year" }),
    ).toBe("$148k–216k");
  });

  it("accepts numeric strings, as Postgres numerics arrive", () => {
    expect(formatSalary({ salaryMin: "190000", salaryMax: "190000" })).toBe("$190k");
  });

  it("says the period for hourly pay and keeps cents", () => {
    expect(formatSalary({ salaryMin: 55.5, salaryMax: 70, salaryPeriod: "hour" })).toBe(
      "$55.50–70 an hour",
    );
  });

  it("uses the currency code when it has no symbol", () => {
    expect(formatSalary({ salaryMin: 90000, salaryCurrency: "CAD" })).toBe("CAD 90k");
  });

  it("returns null when there is no pay", () => {
    expect(formatSalary({ salaryMin: null, salaryMax: null })).toBeNull();
  });
});

describe("formatLocation", () => {
  it("adds the remote policy when the location does not say it", () => {
    expect(formatLocation(["San Francisco, CA"], "hybrid")).toBe("San Francisco, CA, hybrid");
  });

  it("does not repeat remote", () => {
    expect(formatLocation(["Remote, USA"], "remote")).toBe("Remote, USA");
  });

  it("counts extra locations", () => {
    expect(formatLocation(["Seattle, WA", "Austin, TX", "NYC"], "onsite")).toBe(
      "Seattle, WA + 2 more",
    );
  });

  it("falls back to the policy, then to a plain statement", () => {
    expect(formatLocation([], "remote")).toBe("Remote");
    expect(formatLocation(null, null)).toBe("Location not stated");
  });
});

describe("timeAgo", () => {
  const now = new Date("2026-09-24T12:00:00Z").getTime();

  it("buckets recent times", () => {
    expect(timeAgo(new Date(now - 10_000), now)).toBe("just now");
    expect(timeAgo(new Date(now - 5 * 60_000), now)).toBe("5m ago");
    expect(timeAgo(new Date(now - 3 * 3_600_000), now)).toBe("3h ago");
    expect(timeAgo(new Date(now - 2 * 86_400_000), now)).toBe("2d ago");
  });

  it("switches to a date after two weeks", () => {
    expect(timeAgo("2026-08-12T12:00:00Z", now)).toBe("Aug 12");
  });

  it("is empty for a missing date", () => {
    expect(timeAgo(null, now)).toBe("");
  });
});

describe("fitLevel", () => {
  it("steps a triage score at the fast rubric's bands: 85, 70, 55, 30", () => {
    const scores = [null, undefined, 0, 29, 30, 54, 55, 69, 70, 84, 85, 100];
    expect(scores.map((score) => fitLevel(score))).toEqual([0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]);
  });

  it("steps a reviewed score at the deep rubric's bands: 85, 70, 50", () => {
    const scores = [null, 0, 49, 50, 54, 69, 70, 84, 85, 100];
    expect(scores.map((score) => fitLevel(score, true))).toEqual([0, 1, 1, 3, 3, 3, 4, 4, 5, 5]);
  });

  it("puts every verdict on the step its band of scores sits on", () => {
    // The deep rubric: strong 85-100, apply 70-84, maybe 50-69, skip 0-49.
    const bands = { strong: [85, 100], apply: [70, 84], maybe: [50, 69], skip: [0, 49] } as const;
    for (const [verdict, [low, high]] of Object.entries(bands)) {
      const level = RECOMMENDATION_LEVEL[verdict as keyof typeof bands];
      expect([fitLevel(low, true), fitLevel(high, true)], verdict).toEqual([level, level]);
    }
  });
});

describe("verdictLabel", () => {
  it("names a reviewed match by its recommendation", () => {
    expect(["strong", "apply", "maybe", "skip"].map(verdictLabel)).toEqual([
      "Strong fit",
      "Worth applying",
      "Maybe",
      "Skip",
    ]);
  });

  it("says a match without one has not been reviewed", () => {
    expect([null, undefined, "", "great", "toString"].map(verdictLabel)).toEqual(
      Array(5).fill("Not reviewed"),
    );
  });

  it("knows a recommendation from any other string", () => {
    expect(Object.keys(RECOMMENDATION_LABEL).every(isRecommendation)).toBe(true);
    expect(["constructor", "Strong", "", null, undefined].some(isRecommendation)).toBe(false);
  });
});

describe("requirementsLine", () => {
  const strong = { strength: "strong" };
  const partial = { strength: "partial" };
  const learnable = { severity: "learnable" };
  const blocker = { severity: "disqualifying" };

  it("counts met requirements against all the review weighed", () => {
    expect(
      requirementsLine({
        matched_requirements: [strong, strong, strong, strong, strong, partial, partial],
        gaps: [learnable, learnable],
      }),
    ).toBe("7 of 9 requirements met (2 partly), 2 learnable gaps");
    expect(requirementsLine({ matched_requirements: [strong], gaps: [learnable] })).toBe(
      "1 of 2 requirements met, 1 learnable gap",
    );
  });

  it("says when nothing is missing", () => {
    expect(requirementsLine({ matched_requirements: [strong, partial], gaps: [] })).toBe(
      "2 of 2 requirements met (1 partly), no gaps",
    );
    expect(requirementsLine({ matched_requirements: [strong] })).toBe(
      "1 of 1 requirement met, no gaps",
    );
  });

  it("counts blockers apart from learnable gaps", () => {
    expect(
      requirementsLine({ matched_requirements: [strong], gaps: [learnable, blocker, blocker] }),
    ).toBe("1 of 4 requirements met, 1 learnable gap, 2 blockers");
    expect(requirementsLine({ matched_requirements: [], gaps: [blocker] })).toBe(
      "0 of 1 requirement met, 1 blocker",
    );
  });

  it("is null when the review listed no requirements", () => {
    expect(requirementsLine({ matched_requirements: [], gaps: [] })).toBeNull();
    expect(requirementsLine({ matched_requirements: null, gaps: null })).toBeNull();
    expect(requirementsLine({})).toBeNull();
  });
});

describe("stripFactIds", () => {
  it("drops the bracketed resume ids a model left in prose", () => {
    expect(
      stripFactIds("Dropped events by 94% [3d98a1f0-42c8-4f9d-8a69-aeafe57027b6]. Then more."),
    ).toBe("Dropped events by 94%. Then more.");
  });

  it("keeps other brackets and reads nothing as empty", () => {
    expect(stripFactIds("Go [preferred] and Rust")).toBe("Go [preferred] and Rust");
    expect([stripFactIds(null), stripFactIds(undefined), stripFactIds("  ")]).toEqual(["", "", ""]);
  });
});

describe("summaryRest", () => {
  const summary = "A close fit.  The gaps are small.\nWorth applying.";

  it("is what the summary says after the sentence the match leads with", () => {
    expect(summaryRest(summary, "A close fit.")).toBe("The gaps are small. Worth applying.");
    expect(summaryRest("A close fit.", "A close fit.")).toBe("");
  });

  it("is the whole summary when it does not open with that sentence", () => {
    expect(summaryRest(summary, "Backend work in Python")).toBe(
      "A close fit. The gaps are small. Worth applying.",
    );
    expect(summaryRest(summary, null)).toBe("A close fit. The gaps are small. Worth applying.");
    expect(summaryRest(null, "A close fit.")).toBe("");
  });
});

describe("small helpers", () => {
  it("formats money and durations", () => {
    expect(formatUsd(0)).toBe("$0");
    expect(formatUsd(0.004)).toBe("<$0.01");
    expect(formatUsd(0.756)).toBe("$0.76");
    expect(formatDuration(42_000)).toBe("42s");
    expect(formatDuration(125_000)).toBe("2m 5s");
  });

  it("pluralizes", () => {
    expect(plural(1, "role")).toBe("1 role");
    expect(plural(1200, "role")).toBe("1,200 roles");
    expect(plural(2, "match", "matches")).toBe("2 matches");
  });

  it("makes monograms and stable hues", () => {
    expect(initials("Kestrel Logistics Group")).toBe("KL");
    expect(initials("airbyte")).toBe("AI");
    expect(initials("")).toBe("?");
    expect(nameHue("Stripe")).toBe(nameHue("Stripe"));
  });
});

describe("formatLag", () => {
  it("rounds to the unit a person would say", () => {
    expect(formatLag(0)).toBe("under a minute");
    expect(formatLag(59)).toBe("under a minute");
    expect(formatLag(60)).toBe("1 minute");
    expect(formatLag(9 * 60 + 20)).toBe("9 minutes");
    expect(formatLag(89 * 60)).toBe("89 minutes");
    expect(formatLag(2 * 3600 + 600)).toBe("2 hours");
    expect(formatLag(35 * 3600)).toBe("35 hours");
    expect(formatLag(3 * 86400)).toBe("3 days");
  });

  it("says a long wait in weeks, then months", () => {
    expect(formatLag(13 * 86400)).toBe("13 days");
    expect(formatLag(14 * 86400)).toBe("2 weeks");
    expect(formatLag(45 * 86400)).toBe("6 weeks");
    expect(formatLag(59 * 86400)).toBe("8 weeks");
    expect(formatLag(60 * 86400)).toBe("2 months");
    expect(formatLag(213 * 86400)).toBe("7 months");
  });
});

describe("formatPercent", () => {
  it("rounds a share to a whole percentage", () => {
    expect(formatPercent(0.3)).toBe("30%");
    expect(formatPercent(7 / 31)).toBe("23%");
    expect(formatPercent(0)).toBe("0%");
    expect(formatPercent(1)).toBe("100%");
  });

  it("never rounds a rare thing to never or a common one to always", () => {
    expect(formatPercent(0.004)).toBe("under 1%");
    expect(formatPercent(0.005)).toBe("1%");
    expect(formatPercent(0.996)).toBe("over 99%");
    expect(formatPercent(0.994)).toBe("99%");
  });
});

describe("clock times", () => {
  it("says a stored time of day the way people do", () => {
    expect(clockLabel("00:00")).toBe("12:00 am");
    expect(clockLabel("08:00")).toBe("8:00 am");
    expect(clockLabel("12:00")).toBe("12:00 pm");
    expect(clockLabel("22:30")).toBe("10:30 pm");
    expect(clockLabel("23:59")).toBe("11:59 pm");
  });

  it("offers every half hour, and keeps a stored time that is not one", () => {
    const options = clockOptions();
    expect(options).toHaveLength(48);
    expect(options.slice(0, 3)).toEqual(["00:00", "00:30", "01:00"]);
    expect(options.at(-1)).toBe("23:30");
    expect(clockOptions("22:00")).toHaveLength(48);
    expect(clockOptions("06:45").slice(13, 16)).toEqual(["06:30", "06:45", "07:00"]);
    expect(clockOptions("nonsense")).toHaveLength(48);
  });
});

describe("job facts", () => {
  it("names the level and where it came from", () => {
    expect(levelLabel({ level: "new_grad", levelFrom: "title" })).toBe(
      "New graduate role, from its title",
    );
    expect(levelLabel({ level: "senior", levelFrom: "years" })).toBe(
      "Senior, from the years it asks for",
    );
    expect(levelLabel({ level: "unknown", levelFrom: null })).toBe("Not stated");
  });

  it("says what experience is required, preferred or unsaid", () => {
    expect(yearsLabel({ yearsMin: 3, yearsPreferred: null })).toBe("3 years or more required");
    expect(yearsLabel({ yearsMin: 1, yearsPreferred: null })).toBe("1 year or more required");
    expect(yearsLabel({ yearsMin: 0, yearsPreferred: null })).toBe("None required");
    expect(yearsLabel({ yearsMin: null, yearsPreferred: 5 })).toBe(
      "5 years or more preferred, none required",
    );
    expect(yearsLabel({ yearsMin: null, yearsPreferred: null })).toBe("Not stated");
  });

  it("names work authorization and clearance", () => {
    expect(workAuthLabel({ workAuth: "no_sponsorship" })).toBe("No visa sponsorship");
    expect(workAuthLabel({ workAuth: "unknown" })).toBe("Not stated");
    expect(clearanceLabel({ clearance: "eligible" })).toBe(
      "Needs a security clearance you can obtain",
    );
  });
});
