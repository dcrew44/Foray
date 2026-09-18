import { describe, expect, it } from "vitest";

import { cleanDescription, htmlToText } from "@/lib/sources/clean";
import {
  contentHash,
  dedupeKey,
  normalizeCompany,
  normalizeTitle,
} from "@/lib/sources/dedupe";

describe("htmlToText", () => {
  it("keeps list structure as bullets", () => {
    const text = htmlToText("<ul><li>First thing</li><li>Second thing</li></ul>");
    expect(text).toContain("• First thing");
    expect(text).toContain("• Second thing");
  });

  it("decodes entities, including escaped markup", () => {
    expect(htmlToText("<p>Ben &amp; Jerry&rsquo;s</p>")).toBe("Ben & Jerry’s");
  });

  it("drops script and style content", () => {
    const text = htmlToText(
      "<p>Real text</p><script>var x = 'not text';</script><style>.a{}</style>",
    );
    expect(text).toBe("Real text");
  });
});

describe("cleanDescription", () => {
  it("handles entity-escaped HTML, as Greenhouse sends it", () => {
    const raw = "&lt;p&gt;We are hiring an engineer.&lt;/p&gt;";
    const { text } = cleanDescription(raw);
    expect(text).toBe("We are hiring an engineer.");
  });

  it("strips a trailing equal-opportunity block", () => {
    const body = "We are hiring.\n\n".repeat(30);
    const { text, removed } = cleanDescription(
      `${body}Acme Corp is an equal opportunity employer and does not discriminate.`,
    );

    expect(removed).toContain("equal opportunity");
    expect(text).not.toContain("equal opportunity");
    expect(text).toContain("We are hiring.");
  });

  it("strips E-Verify and accommodation notices from the tail", () => {
    const body = "Responsibilities include shipping code.\n\n".repeat(20);
    const { removed } = cleanDescription(
      `${body}E-Verify is used by this employer.\n\nIf you need an accommodation, contact us.`,
    );

    expect(removed).toEqual(
      expect.arrayContaining(["E-Verify", "accommodation"]),
    );
  });

  it("leaves the same wording alone when it appears in the body", () => {
    // A diversity-focused role legitimately discusses this in its duties.
    const tail = "You will ship features.\n\n".repeat(40);
    const { text, removed } = cleanDescription(
      `We are an equal opportunity employer and you will own that programme.\n\n${tail}`,
    );

    expect(removed).toHaveLength(0);
    expect(text).toContain("equal opportunity");
  });

  it("never summarises — every kept paragraph survives verbatim", () => {
    const paragraphs = [
      "Build and operate the billing service.",
      "Work with a team of six engineers.",
      "Requirements: three years of TypeScript.",
    ];
    const { text } = cleanDescription(paragraphs.join("\n\n"));
    for (const paragraph of paragraphs) expect(text).toContain(paragraph);
  });

  it("accepts plain text as readily as HTML", () => {
    expect(cleanDescription("Just plain text.").text).toBe("Just plain text.");
  });

  it("returns empty for nothing", () => {
    expect(cleanDescription(null)).toEqual({ text: "", removed: [] });
  });
});

describe("dedupe", () => {
  it("ignores legal suffixes on a company name", () => {
    expect(normalizeCompany("Stripe, Inc.")).toBe(normalizeCompany("Stripe"));
    expect(normalizeCompany("Acme Corp")).toBe(normalizeCompany("ACME"));
  });

  it("strips bracketed and trailing qualifiers from a title", () => {
    expect(normalizeTitle("Software Engineer (Remote)")).toBe(
      normalizeTitle("Software Engineer"),
    );
    expect(normalizeTitle("Backend Engineer - Remote")).toBe(
      normalizeTitle("Backend Engineer"),
    );
    expect(normalizeTitle("Data Engineer [REQ-1234]")).toBe(
      normalizeTitle("Data Engineer"),
    );
  });

  it("keeps a seniority numeral, which is a different job", () => {
    expect(normalizeTitle("Engineer II")).not.toBe(normalizeTitle("Engineer III"));
  });

  it("keeps symbols that distinguish a language", () => {
    expect(normalizeTitle("C++ Engineer")).not.toBe(normalizeTitle("C# Engineer"));
  });

  it("collapses the same job seen through two sources", () => {
    // What an aggregator and the company's own board would each report.
    const fromAggregator = dedupeKey({
      companyName: "Stripe, Inc.",
      title: "Software Engineer, Payments (Remote)",
      primaryLocation: "San Francisco",
    });
    const fromBoard = dedupeKey({
      companyName: "Stripe",
      title: "Software Engineer, Payments",
      primaryLocation: "san francisco",
    });

    expect(fromAggregator).toBe(fromBoard);
  });

  it("does not collapse genuinely different roles at one company", () => {
    const base = { companyName: "Stripe", primaryLocation: "San Francisco" };
    expect(dedupeKey({ ...base, title: "Backend Engineer" })).not.toBe(
      dedupeKey({ ...base, title: "Frontend Engineer" }),
    );
  });

  it("does not collapse the same role in two cities", () => {
    const base = { companyName: "Stripe", title: "Backend Engineer" };
    expect(dedupeKey({ ...base, primaryLocation: "San Francisco" })).not.toBe(
      dedupeKey({ ...base, primaryLocation: "New York" }),
    );
  });
});

describe("contentHash", () => {
  const base = {
    title: "Engineer",
    locations: ["San Francisco, CA"],
    descriptionRaw: "Build things.",
    salaryMin: 100,
    salaryMax: 200,
  };

  it("is stable across refetches of an unchanged posting", () => {
    expect(contentHash(base)).toBe(contentHash({ ...base }));
  });

  it("ignores the order locations happen to arrive in", () => {
    const a = { ...base, locations: ["A", "B"] };
    const b = { ...base, locations: ["B", "A"] };
    expect(contentHash(a)).toBe(contentHash(b));
  });

  it("changes when the description or the pay changes", () => {
    expect(contentHash({ ...base, descriptionRaw: "Build other things." })).not.toBe(
      contentHash(base),
    );
    expect(contentHash({ ...base, salaryMax: 250 })).not.toBe(contentHash(base));
  });
});
