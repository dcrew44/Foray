import { describe, expect, it } from "vitest";

import { ResumeSchema, type Resume } from "@/lib/resume/schema";
import {
  buildTailorPrompt,
  evaluationNotes,
  mapTailoredOutput,
  TailorOutputSchema,
  type TailorOutput,
} from "@/lib/resume/tailor";

/**
 * The tailor's model output is untrusted. Mapping it back onto the base is
 * where an invented item first gets stopped: only base ids survive, basics are
 * the base's, and skills are base skills with the base's grouping.
 */

function base(): Resume {
  return ResumeSchema.parse({
    basics: {
      name: "Dana Reyes",
      email: "dana@example.com",
      links: [{ id: "l1", url: "github.com/dana", label: "github.com/dana" }],
    },
    summary: "Backend engineer.",
    work: [
      {
        id: "w1",
        source: "user",
        company: "Kestrel",
        position: "Engineer II",
        startDate: "2023",
        endDate: "Present",
        highlights: ["Rebuilt ingestion."],
      },
      {
        id: "w2",
        company: "Northgate",
        position: "Engineer I",
        startDate: "2021",
        endDate: "2023",
        highlights: ["Built the reporting API."],
      },
    ],
    education: [{ id: "e1", institution: "Bellwater", highlights: ["Minor in Statistics."] }],
    projects: [
      { id: "p1", name: "Tideline", description: "A CLI." },
      { id: "p2", name: "Slowhand", description: "A profiler." },
    ],
    skills: [
      { id: "s1", name: "TypeScript", category: "Languages" },
      { id: "s2", name: "PostgreSQL", category: "Data" },
    ],
  });
}

function output(overrides: Partial<TailorOutput> = {}): TailorOutput {
  return TailorOutputSchema.parse({
    summary: "Backend engineer who rebuilt ingestion.",
    work: [
      {
        id: "w2",
        company: "Northgate",
        position: "Engineer I",
        location: null,
        startDate: "2021",
        endDate: "2023",
        summary: null,
        highlights: ["Built the reporting API.", "  "],
      },
      {
        id: "w1",
        company: "Kestrel",
        position: "Engineer II",
        location: null,
        startDate: "2023",
        endDate: "Present",
        summary: null,
        highlights: ["Rebuilt the ingestion service."],
      },
    ],
    education: [
      {
        id: "e1",
        institution: "Bellwater",
        area: null,
        studyType: null,
        startDate: null,
        endDate: null,
        score: null,
        highlights: ["Minor in Statistics."],
      },
    ],
    projects: [
      { id: "p2", name: "Slowhand", description: "A profiler.", url: null, startDate: null, endDate: null, highlights: [] },
      { id: "p1", name: "Tideline", description: "A CLI.", url: null, startDate: null, endDate: null, highlights: [] },
    ],
    skills: ["postgresql", "TypeScript"],
    changes: [
      {
        section: "work",
        item_id: "w1",
        before: "Rebuilt ingestion.",
        after: "Rebuilt the ingestion service.",
        reason: "Clearer",
        evidence_fact_ids: ["w1"],
      },
    ],
    ...overrides,
  });
}

describe("mapTailoredOutput", () => {
  it("drops an item whose id is not a base id, and reports it", () => {
    const { resume, dropped } = mapTailoredOutput(
      base(),
      output({
        work: [
          ...output().work,
          {
            id: "w-invented",
            company: "Stripe",
            position: "Staff Engineer",
            location: null,
            startDate: "2024",
            endDate: "Present",
            summary: null,
            highlights: ["Scaled payments."],
          },
        ],
      }),
    );

    expect(resume.work.map((item) => item.id)).toEqual(["w1", "w2"]);
    expect(dropped).toEqual([
      expect.objectContaining({ section: "work", item_id: "w-invented", text: "Staff Engineer — Stripe" }),
    ]);
  });

  it("keeps roles in base order but projects in the tailor's order", () => {
    const { resume } = mapTailoredOutput(base(), output());
    expect(resume.work.map((item) => item.id)).toEqual(["w1", "w2"]);
    expect(resume.projects.map((item) => item.id)).toEqual(["p2", "p1"]);
  });

  it("copies provenance from the base item and drops blank bullets", () => {
    const { resume } = mapTailoredOutput(base(), output());
    expect(resume.work[0].source).toBe("user");
    expect(resume.work[1].source).toBe("resume");
    expect(resume.work[1].highlights).toEqual(["Built the reporting API."]);
  });

  it("copies basics from the base verbatim", () => {
    const original = base();
    const { resume } = mapTailoredOutput(original, output());
    expect(resume.basics).toEqual(original.basics);
  });

  it("maps skills to base skills by name, with the base's id and category", () => {
    const { resume, dropped } = mapTailoredOutput(
      base(),
      output({ skills: ["postgresql", "TypeScript", "Kubernetes", "TYPESCRIPT"] }),
    );
    expect(resume.skills).toEqual([
      { id: "s2", source: "resume", name: "PostgreSQL", category: "Data" },
      { id: "s1", source: "resume", name: "TypeScript", category: "Languages" },
    ]);
    expect(dropped).toEqual([expect.objectContaining({ section: "skills", text: "Kubernetes" })]);
  });

  it("ignores a repeated id rather than duplicating the item", () => {
    const first = output().work[1];
    const { resume } = mapTailoredOutput(base(), output({ work: [first, { ...first, highlights: ["Again."] }] }));
    expect(resume.work).toHaveLength(1);
    expect(resume.work[0].highlights).toEqual(["Rebuilt the ingestion service."]);
  });

  it("replaces an impossible date with the base value instead of failing", () => {
    const long = "x".repeat(60);
    const work = output().work.map((item) => ({ ...item, startDate: long }));
    const { resume } = mapTailoredOutput(base(), output({ work }));
    expect(resume.work[0].startDate).toBe("2023");
  });

  it("passes the change log through, trimmed", () => {
    const { changes } = mapTailoredOutput(base(), output());
    expect(changes).toEqual([
      {
        section: "work",
        item_id: "w1",
        before: "Rebuilt ingestion.",
        after: "Rebuilt the ingestion service.",
        reason: "Clearer",
        evidence_fact_ids: ["w1"],
      },
    ]);
  });
});

describe("evaluationNotes", () => {
  it("reads a deep evaluation's requirements, talking points and gaps", () => {
    const notes = evaluationNotes({
      deepEval: {
        matched_requirements: [
          { requirement: "Postgres at scale", evidence: "40M-row table", fact_ids: ["w1"], strength: "strong" },
        ],
        gaps: [{ gap: "Kubernetes", severity: "learnable", note: "not on resume" }],
        talking_points: ["Latency work"],
      },
      fastEval: { hard_requirement_misses: ["Security clearance"] },
    });
    expect(notes).toEqual({
      matched: ["Postgres at scale — shown by: 40M-row table"],
      talkingPoints: ["Latency work"],
      gaps: ["Kubernetes (not on resume)", "Security clearance"],
    });
  });

  it("tolerates a missing or malformed evaluation", () => {
    expect(evaluationNotes(null)).toEqual({ matched: [], talkingPoints: [], gaps: [] });
    expect(evaluationNotes({ deepEval: "nonsense", fastEval: [1, 2] })).toEqual({
      matched: [],
      talkingPoints: [],
      gaps: [],
    });
  });
});

describe("buildTailorPrompt", () => {
  const job = {
    title: "Backend Engineer",
    companyName: "Acme",
    locations: ["Remote"],
    remote: "remote" as const,
    descriptionText: "We use Postgres. ".repeat(2000),
    descriptionRaw: null,
  };

  it("puts the stable facts in the instructions and the posting in the prompt", () => {
    const { instructions, prompt } = buildTailorPrompt({
      base: base(),
      job,
      notes: { matched: [], talkingPoints: [], gaps: ["Kubernetes"] },
      preferences: null,
    });

    expect(instructions).toContain("[w1] Engineer II — Kestrel");
    expect(instructions).toContain("<exact_fields>");
    expect(instructions).toContain('"Minor in Statistics."'); // education highlights the prose omits
    expect(prompt).toContain("Title: Backend Engineer");
    expect(prompt).toContain("never claim or imply them:\n- Kubernetes");
  });

  it("caps the posting", () => {
    const { prompt } = buildTailorPrompt({ base: base(), job });
    expect(prompt.length).toBeLessThan(11_000);
  });
});
