import { MockLanguageModelV4 } from "ai/test";
import { describe, expect, it } from "vitest";

import {
  EVAL_OUTPUT_TOKENS,
  MAX_DESCRIPTION_CHARS,
  costPhrase,
  estimateEvalCost,
  estimateEvalCostFor,
  evalInstructions,
  evalPrompt,
  runDeepEval,
  sanitizeDeepEval,
  stripFactIds,
  type EvalCandidate,
  type EvalPosting,
} from "@/lib/ranking/deep-eval";
import { DeepEval } from "@/lib/ranking/schemas";
import { EMPTY_PREFERENCES } from "@/lib/profile/interview";
import { ResumeSchema, factIds } from "@/lib/resume/schema";
import { resumeToText } from "@/lib/resume/text";

/**
 * The deep evaluation's pure parts — the prompt, the citation check, the cost
 * quote — and one full pass through `generateStructured` with the SDK's mock
 * model, so no test here spends money.
 */

const resume = ResumeSchema.parse({
  basics: { name: "Ada Example", links: [] },
  work: [
    {
      id: "work-1",
      company: "Initech",
      position: "Backend Engineer",
      startDate: "2022-01",
      highlights: ["Built the billing service in TypeScript and Postgres"],
    },
  ],
  education: [
    { id: "edu-1", institution: "State University", area: "Computer Science", studyType: "BS" },
  ],
  projects: [{ id: "proj-1", name: "Queue visualiser", highlights: [] }],
  skills: [{ id: "skill-1", name: "TypeScript", category: "Languages" }],
});

const candidate: EvalCandidate = {
  resumeId: "resume-1",
  resumeVersion: 1,
  resumeText: resumeToText(resume),
  preferences: {
    ...EMPTY_PREFERENCES,
    target_roles: ["Backend engineer"],
    locations: ["San Francisco, CA"],
  },
  factIds: new Set(factIds(resume)),
};

const posting: EvalPosting = {
  title: "Backend Engineer, Payments",
  company: "Stripe",
  locations: ["San Francisco, CA"],
  remote: "hybrid",
  employmentType: "full-time",
  salaryMin: "150000",
  salaryMax: "190000",
  salaryCurrency: "USD",
  salaryPeriod: "year",
  postedAt: new Date("2026-09-10T00:00:00Z"),
  description: "You will build payment systems in TypeScript.",
};

const evaluation: DeepEval = {
  matched_requirements: [
    {
      requirement: "Backend experience in TypeScript",
      evidence: "Built the billing service in TypeScript and Postgres",
      fact_ids: ["work-1", "[skill-1]", "invented-99", "work-1"],
      strength: "strong",
    },
    {
      requirement: "A CS degree",
      evidence: "BS in Computer Science",
      fact_ids: ["made-up"],
      strength: "strong",
    },
  ],
  gaps: [{ gap: "Payments domain", severity: "learnable", note: "Adjacent billing work." }],
  seniority_fit: "match",
  location_fit: "match",
  salary_fit: "unstated",
  risk_flags: [],
  talking_points: ["Lead with the billing service."],
  summary: "A direct fit for the backend work, with billing close to payments.",
  score: 84,
  recommendation: "apply",
};

describe("sanitizeDeepEval", () => {
  it("keeps real fact ids, forgives brackets, drops invented ones and duplicates", () => {
    const clean = sanitizeDeepEval(evaluation, candidate.factIds);

    expect(clean.matched_requirements[0].fact_ids).toEqual(["work-1", "skill-1"]);
    // A requirement whose citations were all invented keeps its evidence
    // text but cites nothing.
    expect(clean.matched_requirements[1].fact_ids).toEqual([]);
    expect(clean.matched_requirements[1].evidence).toBe("BS in Computer Science");
  });

  it("changes nothing but the citations", () => {
    const clean = sanitizeDeepEval(evaluation, candidate.factIds);
    expect({ ...clean, matched_requirements: [] }).toEqual({
      ...evaluation,
      matched_requirements: [],
    });
  });
});

describe("prose clean-up", () => {
  const id1 = "3d987730-884c-4e77-b414-92f932ccc77d";
  const id2 = "71ac0b2e-1111-4222-8333-944455556666";
  const ids = new Set([id1, id2, "work-1"]);

  it("strips bracketed ids from every prose field and keeps the sentence", () => {
    const dirty: DeepEval = {
      ...evaluation,
      matched_requirements: [
        {
          requirement: `Event pipelines [${id1}]`,
          evidence: `Cut dropped events by 94% [${id1}] and led the migration [${id2}, ${id1}].`,
          fact_ids: [],
          strength: "strong",
        },
      ],
      gaps: [{ gap: `Kafka [${id2}]`, severity: "learnable", note: `Adjacent to [${id2}] work.` }],
      risk_flags: [`[${id1}]`, "Contract-to-hire"],
      talking_points: [`Lead with the migration [${id2}].`, "Mention the billing work [work-1]"],
      summary: `Strong backend fit [${id1}]. Payments is adjacent.`,
    };

    const clean = sanitizeDeepEval(dirty, ids);

    expect(clean.summary).toBe("Strong backend fit. Payments is adjacent.");
    expect(clean.matched_requirements[0].requirement).toBe("Event pipelines");
    expect(clean.matched_requirements[0].evidence).toBe(
      "Cut dropped events by 94% and led the migration.",
    );
    // Ids cited only in the evidence prose become citations, not losses.
    expect(clean.matched_requirements[0].fact_ids).toEqual([id1, id2]);
    expect(clean.gaps[0]).toEqual({
      gap: "Kafka",
      severity: "learnable",
      note: "Adjacent to work.",
    });
    // A flag that was nothing but an id is dropped rather than shown empty.
    expect(clean.risk_flags).toEqual(["Contract-to-hire"]);
    expect(clean.talking_points).toEqual([
      "Lead with the migration.",
      "Mention the billing work",
    ]);
  });

  it("does not cite a bracketed id the resume does not have", () => {
    const stranger = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
    const clean = sanitizeDeepEval(
      {
        ...evaluation,
        matched_requirements: [
          { requirement: "Go", evidence: `Wrote Go [${stranger}]`, fact_ids: [], strength: "partial" },
        ],
      },
      ids,
    );
    expect(clean.matched_requirements[0].evidence).toBe("Wrote Go");
    expect(clean.matched_requirements[0].fact_ids).toEqual([]);
  });

  it("leaves ordinary brackets alone", () => {
    expect(stripFactIds("Uses [Go] and TypeScript [2021–2023]")).toBe(
      "Uses [Go] and TypeScript [2021–2023]",
    );
  });
});

describe("the prompt", () => {
  it("puts the rubric, preferences and the resume with its ids in the stable half", () => {
    const instructions = evalInstructions(candidate);
    expect(instructions).toContain("[work-1] Backend Engineer — Initech");
    expect(instructions).toContain("[edu-1]");
    expect(instructions).toContain("target_roles");
    expect(instructions).not.toContain("Stripe");
  });

  it("describes the posting, with pay and age", () => {
    const prompt = evalPrompt(posting, new Date("2026-09-24T12:00:00Z"));
    expect(prompt).toContain("Title: Backend Engineer, Payments");
    expect(prompt).toContain("Pay: 150,000–190,000 USD per year");
    expect(prompt).toContain("Posted: 2026-09-10 (14 days ago)");
    expect(prompt).toContain("Today: 2026-09-24");
  });

  it("says unstated rather than inventing pay, and reads a long description only so far", () => {
    const prompt = evalPrompt({
      ...posting,
      salaryMin: null,
      salaryMax: null,
      postedAt: null,
      description: "x".repeat(MAX_DESCRIPTION_CHARS + 500),
    });
    expect(prompt).toContain("Pay: unstated");
    expect(prompt).toContain("Posted: unknown");
    expect(prompt).toContain(`${"x".repeat(MAX_DESCRIPTION_CHARS)}\n[Description cut at`);
    expect(prompt).not.toContain("x".repeat(MAX_DESCRIPTION_CHARS + 1));
  });
});

describe("estimateEvalCost", () => {
  it("prices input from characters and output from the per-evaluation allowance", () => {
    // 16,000 chars ≈ 4,000 tokens + 400 of schema overhead = 4,400 input
    // tokens at $2/M, plus the 1,800-token output allowance at $12/M.
    const estimate = estimateEvalCost(10, {
      inputChars: 16_000,
      modelRef: "openai:gpt-5.6-terra",
    });

    const perItem = (4_400 / 1e6) * 2 + (EVAL_OUTPUT_TOKENS / 1e6) * 12;
    expect(EVAL_OUTPUT_TOKENS).toBe(1_800);
    expect(estimate.model).toBe("openai:gpt-5.6-terra");
    expect(estimate.perItemUsd).toBeCloseTo(perItem, 4);
    expect(estimate.usd).toBeCloseTo(perItem * 10, 4);
  });

  it("is linear in the number of roles, and zero for none", () => {
    const one = estimateEvalCost(1, { modelRef: "openai:gpt-5.6-terra" });
    const fifteen = estimateEvalCost(15, { modelRef: "openai:gpt-5.6-terra" });
    expect(fifteen.usd!).toBeCloseTo(one.usd! * 15, 3);
    expect(estimateEvalCost(0, { modelRef: "openai:gpt-5.6-terra" }).usd).toBe(0);
  });

  it("returns null prices for a model with no price on file", () => {
    expect(estimateEvalCost(3, { modelRef: "acme:mystery-1" })).toEqual({
      usd: null,
      perItemUsd: null,
      model: "acme:mystery-1",
    });
  });

  it("uses the rate in force at the time, for time-banded pricing", () => {
    // A large batch, so rounding to a hundredth of a cent does not blur it.
    const offPeak = estimateEvalCost(1_000, {
      modelRef: "deepseek:deepseek-flash",
      at: new Date("2026-09-24T18:00:00Z"),
    });
    const peak = estimateEvalCost(1_000, {
      modelRef: "deepseek:deepseek-flash",
      at: new Date("2026-09-24T10:00:00Z"),
    });
    expect(offPeak.usd! / peak.usd!).toBeCloseTo(0.5, 3);
  });

  it("prices specific postings from the real prompt, cutting long descriptions", () => {
    const instructions = evalInstructions(candidate).length;
    const estimate = estimateEvalCostFor(candidate, [1_000, 50_000], {
      modelRef: "openai:gpt-5.6-terra",
    });

    const average =
      (instructions + 400 + 1_000 + (instructions + 400 + MAX_DESCRIPTION_CHARS)) / 2;
    const expected = estimateEvalCost(2, {
      inputChars: average,
      modelRef: "openai:gpt-5.6-terra",
    });
    expect(estimate).toEqual(expected);
  });
});

describe("costPhrase", () => {
  it("reads naturally at every size", () => {
    expect(costPhrase(null)).toBeNull();
    expect(costPhrase(0.004)).toBe("under a cent");
    expect(costPhrase(0.4)).toBe("about $0.40");
    expect(costPhrase(1.234)).toBe("about $1.23");
  });
});

describe("runDeepEval", () => {
  it("sends the candidate and posting, parses the schema and drops invented citations", async () => {
    const mock = new MockLanguageModelV4({
      doGenerate: async () => ({
        content: [{ type: "text", text: JSON.stringify(evaluation) }],
        finishReason: { unified: "stop", raw: undefined },
        usage: {
          inputTokens: { total: 10, noCache: 10, cacheRead: undefined, cacheWrite: undefined },
          outputTokens: { total: 20, text: 20, reasoning: undefined },
        },
        warnings: [],
      }),
    });

    const result = await runDeepEval({
      candidate,
      posting,
      overrides: { model: mock },
    });

    expect(result.score).toBe(84);
    expect(result.recommendation).toBe("apply");
    expect(result.matched_requirements[0].fact_ids).toEqual(["work-1", "skill-1"]);

    const sent = JSON.stringify(mock.doGenerateCalls[0].prompt);
    expect(sent).toContain("Backend Engineer, Payments");
    expect(sent).toContain("[work-1]");
  });
});
