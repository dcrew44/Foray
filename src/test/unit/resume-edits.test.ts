import { describe, expect, it } from "vitest";

import { stampUserEdits } from "@/lib/resume/edits";
import { ResumeSchema, type Resume } from "@/lib/resume/schema";

function resume(work: Array<Partial<Resume["work"][number]>>): Resume {
  return ResumeSchema.parse({
    basics: { name: "Test Person", links: [] },
    work: work.map((w) => ({
      id: "w1",
      source: "resume",
      company: "Acme",
      position: "Engineer",
      highlights: [],
      ...w,
    })),
  });
}

describe("stampUserEdits", () => {
  it("leaves untouched items with their original provenance", () => {
    const before = resume([{ id: "w1", source: "resume" }]);
    const after = stampUserEdits(structuredClone(before), before);
    expect(after.work[0].source).toBe("resume");
  });

  it("marks an edited item as the user's", () => {
    const before = resume([{ id: "w1", source: "resume" }]);
    const next = structuredClone(before);
    next.work[0].position = "Senior Engineer";

    const after = stampUserEdits(next, before);
    expect(after.work[0].source).toBe("user");
  });

  it("marks a newly added item as the user's", () => {
    const before = resume([{ id: "w1", source: "resume" }]);
    const next = resume([
      { id: "w1", source: "resume" },
      { id: "w2", source: "resume", company: "New Co" },
    ]);

    const after = stampUserEdits(next, before);
    expect(after.work.find((w) => w.id === "w2")?.source).toBe("user");
    expect(after.work.find((w) => w.id === "w1")?.source).toBe("resume");
  });

  it("notices a changed bullet, not just a changed field", () => {
    const before = resume([{ id: "w1", source: "resume", highlights: ["Did a thing"] }]);
    const next = structuredClone(before);
    next.work[0].highlights = ["Did a thing", "Did another thing"];

    expect(stampUserEdits(next, before).work[0].source).toBe("user");
  });

  it("does not launder an item's provenance when only its id order changed", () => {
    const before = resume([
      { id: "w1", source: "resume" },
      { id: "w2", source: "linkedin_pdf", company: "Other" },
    ]);
    const next = structuredClone(before);
    next.work.reverse();

    const after = stampUserEdits(next, before);
    expect(after.work.find((w) => w.id === "w2")?.source).toBe("linkedin_pdf");
    expect(after.work.find((w) => w.id === "w1")?.source).toBe("resume");
  });

  it("is not fooled by key order, which jsonb does not preserve", () => {
    const before = resume([{ id: "w1", source: "resume", highlights: ["a"] }]);
    // Simulate what comes back from the database: same content, keys shuffled.
    const shuffled = structuredClone(before);
    shuffled.work[0] = Object.fromEntries(
      Object.entries(shuffled.work[0]).reverse(),
    ) as (typeof shuffled.work)[number];

    // The incoming (schema-ordered) item must still count as unchanged.
    expect(stampUserEdits(structuredClone(before), shuffled).work[0].source).toBe(
      "resume",
    );
  });

  it("treats a missing optional field and an explicit undefined as the same", () => {
    const before = resume([{ id: "w1", source: "resume" }]);
    const next = structuredClone(before);
    (next.work[0] as Record<string, unknown>).location = undefined;

    expect(stampUserEdits(next, before).work[0].source).toBe("resume");
  });

  it("treats a first-ever save as entirely the user's own", () => {
    const next = resume([{ id: "w1", source: "resume" }]);
    // No previous profile means nothing to compare against; provenance stands
    // as extraction set it.
    expect(stampUserEdits(next, null).work[0].source).toBe("resume");
  });
});
