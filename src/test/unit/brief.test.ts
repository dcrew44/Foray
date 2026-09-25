import { describe, expect, it } from "vitest";

import { EMPTY_PREFERENCES, type Preferences } from "@/lib/profile/interview";
import {
  applyBrief,
  cleanBrief,
  describeBrief,
  EMPTY_BRIEF,
  isEmptyBrief,
  mergeBrief,
  parseBrief,
  requestText,
  SAVED_PREFERENCES_PROMPT,
} from "@/lib/ranking/brief";
import { companyNamePattern } from "@/lib/ranking/prefilter";

const saved: Preferences = {
  ...EMPTY_PREFERENCES,
  target_roles: ["Senior Backend Engineer"],
  seniority: "senior",
  locations: ["San Francisco Bay Area"],
  remote_pref: "any",
};

describe("the search brief", () => {
  it("overrides only what the request states, for this search", () => {
    const effective = applyBrief(saved, {
      ...EMPTY_BRIEF,
      seniority: "entry",
      roles: ["software engineer"],
      companies: ["Anthropic"],
    });
    expect(effective.seniority).toBe("entry");
    expect(effective.target_roles).toEqual(["software engineer"]);
    expect(effective.locations).toEqual(["San Francisco Bay Area"]);
    expect(effective.remote_pref).toBe("any");
    // The saved preferences themselves are untouched.
    expect(saved.seniority).toBe("senior");
  });

  it("changes nothing when the brief is empty", () => {
    expect(applyBrief(saved, EMPTY_BRIEF)).toEqual(saved);
    expect(isEmptyBrief(EMPTY_BRIEF)).toBe(true);
  });

  it("lets the chat agent's hints win field by field", () => {
    const merged = mergeBrief(
      { companies: ["anthropic"], seniority: "mid", focus: "fintech only" },
      { seniority: "entry", roles: ["developer"], companies: [] },
    );
    expect(merged.seniority).toBe("entry");
    expect(merged.roles).toEqual(["developer"]);
    // An empty list is "not stated", so the derived value stands.
    expect(merged.companies).toEqual(["anthropic"]);
    expect(merged.focus).toBe("fintech only");
  });

  it("cleans blanks and duplicates, and reads junk as no brief", () => {
    expect(cleanBrief({ companies: [" Anthropic ", "anthropic", ""], focus: "  " })).toEqual({
      ...EMPTY_BRIEF,
      companies: ["Anthropic"],
    });
    expect(parseBrief({ seniority: "wizard" })).toEqual(EMPTY_BRIEF);
    expect(parseBrief(null)).toEqual(EMPTY_BRIEF);
  });

  it("describes a search in one line, or not at all when it adds nothing", () => {
    expect(
      describeBrief({
        ...EMPTY_BRIEF,
        seniority: "entry",
        roles: ["software engineer"],
        companies: ["Anthropic"],
        locations: ["San Francisco Bay Area"],
      }),
    ).toBe("Entry-level software engineer roles at Anthropic, in San Francisco Bay Area");
    expect(describeBrief({ ...EMPTY_BRIEF, companies: ["Stripe", "Plaid", "Ramp"], remote: "remote" })).toBe(
      "Roles at Stripe, Plaid or Ramp, remote only",
    );
    expect(describeBrief({ ...EMPTY_BRIEF, focus: "fintech only" })).toBeNull();
    // A role that already names its level is not given a second one.
    expect(
      describeBrief({ ...EMPTY_BRIEF, seniority: "senior", roles: ["Senior Backend Engineer"] }),
    ).toBe("Senior Backend Engineer roles");
  });

  it("treats the Home button's placeholder as no request", () => {
    expect(requestText(SAVED_PREFERENCES_PROMPT)).toBeNull();
    expect(requestText("  ")).toBeNull();
    expect(requestText(" roles at Anthropic ")).toBe("roles at Anthropic");
  });
});

describe("matching a company by name", () => {
  const matches = (name: string, company: string) =>
    new RegExp(companyNamePattern(name)!.replace(/\\y/g, "\\b"), "i").test(company);

  it("matches the name as whole words at the start", () => {
    expect(matches("Anthropic", "Anthropic")).toBe(true);
    expect(matches("anthropic", "Anthropic PBC")).toBe(true);
    expect(matches("Scale AI", "Scale AI, Inc.")).toBe(true);
    expect(matches("Meta", "Metabase")).toBe(false);
    expect(matches("Anthropic", "Not Anthropic")).toBe(false);
  });

  it("has nothing to match for a name with no letters or digits", () => {
    expect(companyNamePattern(" - ")).toBeNull();
  });
});
