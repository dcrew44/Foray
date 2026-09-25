import { describe, expect, it } from "vitest";

import {
  fitLevel,
  formatDuration,
  formatLocation,
  formatSalary,
  formatUsd,
  initials,
  nameHue,
  plural,
  timeAgo,
} from "@/lib/format";

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
  it("maps scores onto the pine scale", () => {
    expect([null, 20, 55, 70, 80, 90].map((score) => fitLevel(score))).toEqual([0, 1, 2, 3, 4, 5]);
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
