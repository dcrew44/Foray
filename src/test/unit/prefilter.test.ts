import { describe, expect, it } from "vitest";

import {
  BAY_AREA_PLACES,
  escapeForRegex,
  guardSeniority,
  titleMatches,
  withoutLevel,
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

describe("title terms for a junior search", () => {
  // What the model proposed for "the most entry-level developer positions at
  // Anthropic": literal phrases that missed "Staff+" and shut out new grads.
  const proposed: DerivedCriteria = {
    title_include: ["software engineer", "developer", "fellows program"],
    title_exclude: ["senior software engineer", "staff software engineer", "intern", "new grad"],
    keywords: [],
  };
  const junior = guardSeniority(proposed, "entry", ["software engineer"]);
  const passes = (title: string) => titleMatches(title, junior);

  it("excludes senior titles however they are written", () => {
    expect(passes("Staff+ Software Engineer, Scaling")).toBe(false);
    expect(passes("Staff + Sr. Software Engineer, Cloud Inference")).toBe(false);
    expect(passes("Sr. Software Engineer")).toBe(false);
    expect(passes("Software Engineer, Team Lead")).toBe(false);
    expect(passes("Engineering Manager")).toBe(false);
  });

  it("keeps plain and early-career titles, including ones that say senior", () => {
    expect(passes("Software Engineer, Research Data Platform")).toBe(true);
    expect(passes("New Grad Software Engineer")).toBe(true);
    expect(passes("Software Engineer Intern")).toBe(true);
    expect(passes("Senior Software Engineer, New Grad Program")).toBe(true);
    expect(passes("Anthropic Fellows Program, ML Systems")).toBe(true);
    // A word boundary, not a substring: SRE is not "sr", Leadership is not "lead".
    expect(passes("Software Engineer, SRE")).toBe(true);
    expect(passes("Software Engineer, Leadership Tools")).toBe(true);
  });

  it("does not exclude a senior word the requested role itself contains", () => {
    const apm = guardSeniority(
      { title_include: ["product manager"], title_exclude: [], keywords: [] },
      "entry",
      ["associate product manager"],
    );
    expect(titleMatches("Associate Product Manager", apm)).toBe(true);
    expect(titleMatches("Product Manager", apm)).toBe(true);
    expect(titleMatches("Senior Product Manager", apm)).toBe(false);
  });

  it("includes every early-career title when the search names companies", () => {
    const base = { title_include: ["software engineer"], title_exclude: [], keywords: [] };
    const targeted = guardSeniority(base, "entry", ["software engineer"], { companies: true });
    expect(titleMatches("Anthropic Fellows Program, ML Systems", targeted)).toBe(true);
    expect(titleMatches("Associate, Rotational Program", targeted)).toBe(true);
    // A word, not a substring: "Internal" and "International" are not "intern".
    expect(titleMatches("Accounting, Revenue Internal Controls", targeted)).toBe(false);
    expect(titleMatches("Marketing Events Manager, International", targeted)).toBe(false);

    const broad = guardSeniority(base, "entry", ["software engineer"]);
    expect(titleMatches("Anthropic Fellows Program, ML Systems", broad)).toBe(false);
  });

  it("leaves other searches alone", () => {
    expect(guardSeniority(proposed, "senior", null)).toEqual(proposed);
    expect(guardSeniority(proposed, null, null)).toEqual(proposed);
  });
});

describe("include terms without a level", () => {
  it("drops a leading level word, since many companies never write one", () => {
    expect(withoutLevel("senior backend engineer")).toBe("backend engineer");
    expect(withoutLevel("Sr. Staff software engineer")).toBe("software engineer");
    expect(withoutLevel("entry-level data analyst")).toBe("data analyst");
    expect(withoutLevel("lead generation specialist")).toBe("lead generation specialist");
    expect(withoutLevel("software engineer, backend")).toBe("software engineer, backend");
  });
});

describe("Bay Area place names", () => {
  // Postgres spells a word boundary \y; JavaScript spells it \b.
  const bayArea = new RegExp(BAY_AREA_PLACES.replace(/\\y/g, "\\b"), "i");

  it("finds the Bay Area in multi-city strings the normalizer leaves without a metro", () => {
    expect(bayArea.test("Seattle, San Francisco, New York")).toBe(true);
    expect(bayArea.test("SEA, SF, NY, Toronto")).toBe(true);
    expect(bayArea.test("San Francisco HQ")).toBe(true);
    expect(bayArea.test("US-Remote, Chicago, Seattle, San Francisco")).toBe(true);
    expect(bayArea.test("Palo Alto, CA")).toBe(true);
  });

  it("does not match places elsewhere", () => {
    expect(bayArea.test("New York City Office")).toBe(false);
    expect(bayArea.test("Seattle, WA")).toBe(false);
    expect(bayArea.test("Dublin")).toBe(false);
    expect(bayArea.test("Sfax, Tunisia")).toBe(false);
  });
});
