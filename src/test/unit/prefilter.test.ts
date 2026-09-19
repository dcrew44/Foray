import { describe, expect, it } from "vitest";

import {
  escapeForRegex,
  titleMatches,
  type DerivedCriteria,
} from "@/lib/ranking/prefilter";

const criteria = (
  include: string[],
  exclude: string[] = [],
): DerivedCriteria => ({
  title_include: include.map(escapeForRegex),
  title_exclude: exclude.map(escapeForRegex),
  keywords: [],
});

describe("escapeForRegex", () => {
  it("neutralises characters that would otherwise be regex syntax", () => {
    // "C++ Engineer" and "Sr." both appear in real derived criteria; unescaped,
    // the first is an invalid quantifier and the second matches anything.
    expect(() => new RegExp(escapeForRegex("C++ engineer"))).not.toThrow();
    expect(new RegExp(escapeForRegex("sr. engineer"), "i").test("srx engineer")).toBe(
      false,
    );
  });
});

describe("titleMatches", () => {
  it("keeps a title matching any include term", () => {
    const c = criteria(["backend engineer", "platform engineer"]);
    expect(titleMatches("Senior Backend Engineer, Payments", c)).toBe(true);
    expect(titleMatches("Staff Platform Engineer", c)).toBe(true);
  });

  it("drops a title matching no include term", () => {
    expect(titleMatches("Account Executive", criteria(["backend engineer"]))).toBe(
      false,
    );
  });

  it("drops a title matching an exclusion", () => {
    const c = criteria(["software engineer"], ["senior", "staff", "principal"]);
    expect(titleMatches("Senior Software Engineer", c)).toBe(false);
    expect(titleMatches("Software Engineer, New Grad", c)).toBe(true);
  });

  it("excludes senior titles from a new-grad search", () => {
    const c = criteria(
      ["software engineer", "new grad"],
      ["senior", "staff", "principal", "director", "manager"],
    );

    for (const title of [
      "Senior Software Engineer",
      "Staff Software Engineer",
      "Engineering Manager",
    ]) {
      expect(titleMatches(title, c)).toBe(false);
    }

    for (const title of [
      "Software Engineer, New Grad (2027)",
      "Software Engineer I",
    ]) {
      expect(titleMatches(title, c)).toBe(true);
    }
  });

  it("excludes intern titles from a senior search", () => {
    const c = criteria(
      ["senior backend engineer", "backend engineer"],
      ["intern", "new grad", "junior"],
    );
    expect(titleMatches("Backend Engineering Intern", c)).toBe(false);
    expect(titleMatches("Senior Backend Engineer", c)).toBe(true);
  });

  it("keeps everything when no criteria were derived", () => {
    // Better to score a wide set than to silently return nothing.
    expect(titleMatches("Anything At All", criteria([]))).toBe(true);
  });
});
