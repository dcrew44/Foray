import { describe, expect, it } from "vitest";

import {
  canMove,
  moveRow,
  rowsFor,
  selectionCounts,
  selectsNothing,
  splitKeywords,
  toDraft,
  toSpec,
  type VariantBase,
  type VariantRow,
} from "@/lib/variants";

const base: VariantBase = {
  work: [
    { id: "lumen", highlights: ["Led settlement.", "Mentored three engineers."] },
    { id: "harbor", highlights: ["Built ingestion."] },
    { id: "fernwood", highlights: [] },
  ],
  projects: [{ id: "tidepool", highlights: ["Used in production."] }],
  education: [{ id: "berkeley" }, { id: "laney" }],
  skills: [{ id: "python" }, { id: "go" }, { id: "sql" }],
};

const row = (id: string, included = true, highlights: string[] | null = null): VariantRow => ({
  id,
  included,
  highlights,
});

describe("toDraft", () => {
  it("lays the stored spec over the base: chosen rows in the spec's order, then the rest", () => {
    const draft = toDraft(base, {
      headline: "Data engineer",
      work: [{ id: "harbor", highlights: ["My words."] }, { id: "lumen" }],
      education: ["laney", "berkeley"],
      skills: ["sql"],
    });
    expect(draft.work).toEqual([
      row("harbor", true, ["My words."]),
      row("lumen"),
      row("fernwood", false),
    ]);
    expect(draft.projects).toEqual([row("tidepool", false)]);
    // Schools and skills are toggled, not ordered: they keep the base's order.
    expect(draft.education).toEqual(["berkeley", "laney"]);
    expect(draft.skills).toEqual(["sql"]);
    expect([draft.headline, draft.summary]).toEqual(["Data engineer", null]);
  });

  it("drops what the base no longer has, and a second mention of a row", () => {
    const draft = toDraft(base, {
      work: [{ id: "gone" }, { id: "lumen" }, { id: "lumen", highlights: ["Again."] }],
      education: ["gone"],
      skills: ["gone", "go"],
    });
    expect(draft.work.map((item) => [item.id, item.included])).toEqual([
      ["lumen", true],
      ["harbor", false],
      ["fernwood", false],
    ]);
    expect(draft.education).toEqual([]);
    expect(draft.skills).toEqual(["go"]);
  });
});

describe("rowsFor", () => {
  it("follows the base: a new role appears unchosen at the end, a removed one disappears", () => {
    const rows = [row("harbor"), row("lumen", false)];
    const edited = [{ id: "new" }, { id: "harbor" }];
    expect(rowsFor(edited, rows)).toEqual([row("harbor"), row("new", false)]);
  });
});

describe("toSpec", () => {
  it("saves chosen rows in order, and bullets that equal the base's as the base's", () => {
    const spec = toSpec(base, {
      headline: "  ",
      summary: " Pipelines. ",
      work: [
        row("harbor", true, ["  Loaded 40 million events.  ", "   "]),
        row("fernwood", false, ["Never saved."]),
        row("lumen", true, ["Led settlement.", "Mentored three engineers."]),
      ],
      projects: [row("tidepool", true, [])],
      education: ["laney", "berkeley", "gone"],
      skills: ["sql", "python"],
    });
    expect(spec).toEqual({
      headline: null,
      summary: "Pipelines.",
      work: [
        { id: "harbor", highlights: ["Loaded 40 million events."] },
        { id: "lumen", highlights: null }, // untouched: it keeps following the resume
      ],
      projects: [{ id: "tidepool", highlights: [] }], // shown without bullets
      education: ["berkeley", "laney"],
      skills: ["python", "sql"],
    });
  });

  it("round-trips: a stored spec opened and saved untouched is the same spec", () => {
    const stored = {
      headline: "Backend engineer",
      summary: null,
      work: [
        { id: "harbor", highlights: ["My words."] },
        { id: "lumen", highlights: null },
      ],
      projects: [],
      education: ["berkeley"],
      skills: ["python", "sql"],
    };
    expect(toSpec(base, toDraft(base, stored))).toEqual(stored);
  });

  it("treats an edit that restores the resume's bullets as no edit", () => {
    const untouched = toSpec(base, toDraft(base, { work: [{ id: "harbor" }] }));
    const draft = toDraft(base, { work: [{ id: "harbor" }] });
    draft.work[0] = row("harbor", true, ["Built ingestion.", ""]);
    expect(toSpec(base, draft)).toEqual(untouched);
  });
});

describe("moveRow", () => {
  const rows = [row("a"), row("b", false), row("c"), row("d")];
  const order = (items: VariantRow[]) => items.map((item) => item.id).join("");

  it("moves a chosen row past the nearest chosen row, leaving unchosen rows in place", () => {
    expect(order(moveRow(rows, "c", -1))).toBe("cbad");
    expect(order(moveRow(rows, "a", 1))).toBe("cbad");
    expect(order(moveRow(rows, "c", 1))).toBe("abdc");
  });

  it("does nothing at an end, for an unchosen row, or for a row that is not there", () => {
    expect(moveRow(rows, "a", -1)).toBe(rows);
    expect(moveRow(rows, "d", 1)).toBe(rows);
    expect(moveRow(rows, "b", 1)).toBe(rows);
    expect(moveRow(rows, "zzz", 1)).toBe(rows);
    expect([canMove(rows, "a", -1), canMove(rows, "a", 1), canMove(rows, "b", -1)]).toEqual([
      false,
      true,
      false,
    ]);
  });
});

describe("selectionCounts", () => {
  it("counts what a spec shows of the resume as it is now", () => {
    const counts = selectionCounts(base, {
      work: [{ id: "lumen" }, { id: "gone" }],
      projects: [{ id: "tidepool" }],
      education: ["berkeley", "laney"],
      skills: ["sql", "gone"],
    });
    expect(counts).toEqual({ work: 1, projects: 1, education: 2, skills: 1 });
    expect(selectsNothing(counts)).toBe(false);
  });

  it("is nothing after a new upload, whose items all have new ids", () => {
    const counts = selectionCounts(base, {
      headline: "Still worded",
      work: [{ id: "old-1" }],
      skills: ["old-2"],
    });
    expect(selectsNothing(counts)).toBe(true);
  });
});

describe("splitKeywords", () => {
  it("splits on commas, trims, and keeps each keyword once whatever its capitals", () => {
    expect(splitKeywords(["backend, API", " api ", "machine   learning", ",", ""])).toEqual([
      "backend",
      "API",
      "machine learning",
    ]);
  });
});
