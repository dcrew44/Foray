import { describe, expect, it, vi } from "vitest";

import { ResumeSchema, type Resume } from "@/lib/resume/schema";
import type { TailorChange } from "@/lib/resume/tailor";
import {
  checkRules,
  collectClaims,
  extractLinks,
  extractMetrics,
  extractTerms,
  finalizeVerification,
  reconcileChanges,
  reviseTailored,
  splitSentences,
  verifyTailored,
  type Judge,
} from "@/lib/resume/verify";

/**
 * The verifier is the product's guard against the one unforgivable failure: a
 * tailored resume that claims something the user never did. These tests pin
 * each rule against the fabrication it exists to catch, and pin the opposite
 * too — a faithful tailoring must pass untouched, or the guard just reverts
 * every useful edit.
 */

const KESTREL = "w-kestrel";
const NORTHGATE = "w-northgate";
const INTERN = "w-intern";

function baseResume(): Resume {
  return ResumeSchema.parse({
    basics: {
      name: "Dana Reyes",
      email: "dana@example.com",
      phone: "(555) 0134-2087",
      location: "Sacramento, CA",
      links: [{ id: "l1", url: "github.com/dana", label: "github.com/dana" }],
    },
    summary:
      "Backend-leaning engineer with four years of experience building Node.js and Python services. Comfortable owning a feature from schema design through rollout.",
    work: [
      {
        id: KESTREL,
        company: "Kestrel Logistics Group",
        position: "Software Engineer II",
        location: "Sacramento, CA",
        startDate: "March 2023",
        endDate: "Present",
        highlights: [
          "Rebuilt the shipment-tracking ingestion service in TypeScript on Node.js, cutting median event processing latency from 840 ms to 190 ms and reducing dropped events by 94%.",
          "Designed the partitioning strategy for a 40-million-row PostgreSQL events table, which held query times under 200 ms as volume tripled.",
          "Mentored two junior engineers through their first on-call rotations.",
        ],
      },
      {
        id: NORTHGATE,
        company: "Northgate Analytics",
        position: "Software Engineer I",
        location: "Remote",
        startDate: "July 2021",
        endDate: "February 2023",
        highlights: [
          "Built the customer-facing reporting API (Python, FastAPI, PostgreSQL) used by roughly 1,200 daily active accounts.",
          "Cut the nightly aggregation job from 3 hours 10 minutes to 26 minutes with set-based SQL.",
        ],
      },
      {
        id: INTERN,
        company: "Willowmere Civic Tech",
        position: "Software Engineering Intern",
        startDate: "May 2020",
        endDate: "August 2020",
        highlights: ["Wrote the first automated test suite for the intake application, reaching 71% line coverage."],
      },
    ],
    education: [
      {
        id: "e1",
        institution: "Bellwater State University",
        area: "Computer Science",
        studyType: "B.S.",
        startDate: "2017",
        endDate: "2021",
        score: "GPA 3.6",
        highlights: ["Minor in Statistics."],
      },
    ],
    projects: [
      {
        id: "p1",
        name: "Tideline",
        url: "github.com/dana/tideline",
        description:
          "An open-source CLI that replays PostgreSQL write-ahead log segments into a local test database. Roughly 900 stars.",
      },
    ],
    skills: [
      { id: "s1", name: "TypeScript", category: "Languages" },
      { id: "s2", name: "Python", category: "Languages" },
      { id: "s3", name: "PostgreSQL", category: "Data" },
      { id: "s4", name: "Kafka", category: "Data" },
      { id: "s5", name: "Docker", category: "Infrastructure" },
    ],
  });
}

/** A copy to tailor. */
function draft(): Resume {
  return structuredClone(baseResume());
}

const reasons = (findings: Array<{ reason: string }>) => findings.map((f) => f.reason).join(" | ");

describe("checkRules: a faithful tailoring passes", () => {
  it("accepts reordering, selection and a reordered skill subset", () => {
    const base = baseResume();
    const tailored = draft();
    tailored.work[0].highlights.reverse();
    tailored.work[1].highlights = [tailored.work[1].highlights[1]];
    tailored.work = tailored.work.slice(0, 2); // dropped the internship
    tailored.skills = [tailored.skills[3], tailored.skills[0]];
    tailored.projects = [];

    expect(checkRules(base, tailored)).toEqual([]);
    expect(collectClaims(base, tailored).every((claim) => claim.verbatim)).toBe(true);
  });

  it("accepts a rephrase that keeps every number and name from the same role", () => {
    const base = baseResume();
    const tailored = draft();
    tailored.work[0].highlights[0] =
      "Cut median event processing latency from 840 ms to 190 ms and dropped events by 94% by rebuilding the shipment-tracking ingestion service in TypeScript/Node.js.";
    tailored.work[1].highlights[0] =
      "Built a customer-facing reporting API with Python, FastAPI and Postgres, serving ~1.2k daily active accounts.";

    expect(checkRules(base, tailored)).toEqual([]);
  });

  it("treats written and numeric forms of the same quantity as the same", () => {
    const base = baseResume();
    const tailored = draft();
    tailored.work[0].highlights[2] = "Mentored 2 junior engineers through their first on-call rotations.";
    tailored.work[0].highlights[1] =
      "Partitioned a 40M-row PostgreSQL events table, keeping query times under 200 ms as volume grew 3x.";
    tailored.work[1].highlights[1] = "Cut the nightly aggregation job from 3h 10m to 26m with set-based SQL.";

    expect(reasons(checkRules(base, tailored))).toBe("");
  });

  it("lets the summary draw on anything in the resume, skills included", () => {
    const base = baseResume();
    const tailored = draft();
    tailored.summary =
      "Backend engineer with four years of experience across TypeScript, Python and PostgreSQL. Particular strength in making data pipelines faster.";

    expect(checkRules(base, tailored)).toEqual([]);
  });
});

describe("checkRules: fabrication is caught", () => {
  it("catches an injected skill", () => {
    const tailored = draft();
    tailored.skills.push({ id: "s-new", source: "resume", name: "Kubernetes", category: "Infrastructure" });

    const findings = checkRules(baseResume(), tailored);
    expect(findings).toHaveLength(1);
    expect(findings[0]).toMatchObject({ section: "skills", text: "Kubernetes", source: "rule" });
  });

  it("matches skills ignoring case and spacing, so a recased skill is not an invention", () => {
    const tailored = draft();
    tailored.skills[0] = { ...tailored.skills[0], name: "  typescript " };
    expect(checkRules(baseResume(), tailored)).toEqual([]);
  });

  it("catches an injected employer", () => {
    const tailored = draft();
    tailored.work.unshift({
      id: "w-google",
      source: "resume",
      company: "Google",
      position: "Senior Software Engineer",
      startDate: "2024",
      endDate: "Present",
      highlights: ["Led the search infrastructure team."],
    });

    const findings = checkRules(baseResume(), tailored);
    expect(findings).toHaveLength(1);
    expect(findings[0]).toMatchObject({ section: "work", item_id: "w-google", source: "rule" });
    expect(findings[0].target).toEqual({ kind: "item", section: "work", itemId: "w-google" });
  });

  it("catches a changed date, title or company", () => {
    const tailored = draft();
    tailored.work[0].startDate = "January 2023";
    tailored.work[1].position = "Senior Software Engineer";
    tailored.work[2].company = "Willowmere";

    const findings = checkRules(baseResume(), tailored);
    expect(findings.map((f) => f.target)).toEqual([
      { kind: "field", section: "work", itemId: KESTREL, field: "startDate" },
      { kind: "field", section: "work", itemId: NORTHGATE, field: "position" },
      { kind: "field", section: "work", itemId: INTERN, field: "company" },
    ]);
    expect(findings[0].reason).toContain('from "March 2023" to "January 2023"');
  });

  it("allows leaving out a location or GPA, but not changing one", () => {
    const base = baseResume();
    const omitted = draft();
    omitted.work[0].location = null;
    omitted.education[0].score = null;
    expect(checkRules(base, omitted)).toEqual([]);

    const changed = draft();
    changed.education[0].score = "GPA 3.9";
    expect(checkRules(base, changed)[0]?.target).toMatchObject({ kind: "field", field: "score" });
  });

  it("catches an invented metric even when the digits appear elsewhere", () => {
    // The base says "40-million-row" — a 40, but not 40%.
    const tailored = draft();
    tailored.work[0].highlights[1] =
      "Designed the partitioning strategy for a PostgreSQL events table, which increased revenue 40%.";

    const findings = checkRules(baseResume(), tailored);
    expect(findings).toHaveLength(1);
    expect(findings[0].reason).toContain("40%");
    expect(findings[0].target).toEqual({ kind: "text", key: `work:${KESTREL}:highlight:1` });
  });

  it("catches a number computed from the originals", () => {
    const tailored = draft();
    tailored.work[0].highlights[0] =
      "Made the shipment-tracking ingestion service 77% faster by rebuilding it in TypeScript on Node.js.";
    expect(reasons(checkRules(baseResume(), tailored))).toContain("77%");
  });

  it("catches a metric moved from one role to another", () => {
    const tailored = draft();
    tailored.work[1].highlights[1] =
      "Cut the nightly aggregation job by 94% with set-based SQL.";
    const findings = checkRules(baseResume(), tailored);
    expect(findings).toHaveLength(1);
    expect(findings[0].item_id).toBe(NORTHGATE);
    expect(findings[0].reason).toContain("this role");
  });

  it("catches a technology the role never mentioned, even one listed as a skill", () => {
    const tailored = draft();
    tailored.work[0].highlights[2] =
      "Mentored two junior engineers through their first on-call rotations on Kubernetes and Kafka.";
    const findings = checkRules(baseResume(), tailored);
    expect(findings).toHaveLength(1);
    expect(findings[0].reason).toMatch(/Kubernetes and Kafka/);
  });

  it("does not mistake a capitalised first word for a technology", () => {
    const tailored = draft();
    tailored.work[0].highlights[2] = "Coached two junior engineers through their first on-call rotations.";
    expect(checkRules(baseResume(), tailored)).toEqual([]);
  });

  it("catches a link or email that is not on the resume, and allows one that is", () => {
    const base = baseResume();
    const invented = draft();
    invented.projects[0].description =
      "An open-source CLI that replays PostgreSQL write-ahead log segments. Demo at tideline-demo.io.";
    expect(reasons(checkRules(base, invented))).toContain("tideline-demo.io");

    const real = draft();
    real.projects[0].description =
      "An open-source CLI (github.com/dana/tideline) that replays PostgreSQL write-ahead log segments into a local test database.";
    expect(checkRules(base, real)).toEqual([]);
  });

  it("catches an invented fact in the summary against the whole resume", () => {
    const tailored = draft();
    tailored.summary = "Backend engineer with 7 years of experience. Deep expertise in Rust.";
    const findings = checkRules(baseResume(), tailored);
    expect(findings.map((f) => f.target)).toEqual([
      { kind: "text", key: "summary:sentence:0" },
      { kind: "text", key: "summary:sentence:1" },
    ]);
  });

  it("catches changed basics", () => {
    const tailored = draft();
    tailored.basics.email = "dana@other.com";
    expect(checkRules(baseResume(), tailored)[0]?.target).toEqual({ kind: "basics", field: "email" });
  });
});

describe("extractors", () => {
  const keys = (text: string) => extractMetrics(text).map((m) => m.keys);

  it("reads numbers with their units", () => {
    expect(keys("reduced errors by 40%")).toEqual([["pct:40"]]);
    expect(keys("40 percent")).toEqual([["pct:40"]]);
    expect(keys("3x faster")).toEqual([["x:3"]]);
    expect(keys("$2M ARR")).toEqual([["n:2000000"]]);
    expect(keys("10k users")).toEqual([["n:10000"]]);
    expect(keys("1,200 accounts")).toEqual([["n:1200"]]);
    expect(keys("a 40-million-row table")).toEqual([["n:40000000"]]);
    expect(keys("in 2019")).toEqual([["n:2019"]]);
    expect(keys("12 engineers")).toEqual([["n:12"]]);
    expect(keys("840 ms")).toEqual([["n:840"]]);
  });

  it("reads number words and multipliers", () => {
    expect(keys("twenty-five services")).toEqual([["n:25"]]);
    expect(keys("six incidents")).toEqual([["n:6"]]);
    expect(keys("volume tripled")).toEqual([["x:3"]]);
    expect(keys("one of the first")).toEqual([]);
  });

  it("does not read names as numbers", () => {
    expect(keys("S3, EC2 and Q3 planning")).toEqual([]);
    expect(extractTerms("S3, EC2 and Q3 planning")).toEqual(["S3", "EC2", "Q3"]);
  });

  it("finds named things but not ordinary capitalised first words", () => {
    expect(extractTerms("Built a service in TypeScript/Node.js with Kafka and C++.")).toEqual([
      "TypeScript",
      "Node.js",
      "Kafka",
      "C++",
    ]);
    expect(extractTerms("Led the team's on-call rotation.")).toEqual([]);
  });

  it("finds links and emails", () => {
    expect(extractLinks("See https://www.Example.com/x/ or mail a@b.co")).toEqual([
      "a@b.co",
      "example.com/x",
    ]);
    expect(extractLinks("Built with Node.js and Next.js")).toEqual([]);
  });

  it("splits sentences without breaking abbreviations", () => {
    expect(splitSentences("B.S. in Computer Science. Served in the U.S. Army. Done!")).toEqual([
      "B.S. in Computer Science.",
      "Served in the U.S. Army.",
      "Done!",
    ]);
  });
});

describe("reviseTailored", () => {
  it("reverts fields, drops invented items and skills, and restores the original bullet", () => {
    const base = baseResume();
    const tailored = draft();
    tailored.work[0].startDate = "January 2023";
    tailored.work.push({
      id: "w-fake",
      source: "resume",
      company: "Stripe",
      position: "Staff Engineer",
      highlights: ["Scaled payments."],
    });
    tailored.skills.push({ id: "s-x", source: "resume", name: "Rust", category: null });
    tailored.work[0].highlights[1] =
      "Designed the partitioning strategy for a PostgreSQL events table, which increased revenue 40%.";

    const findings = checkRules(base, tailored);
    const { resume, revisions } = reviseTailored(base, tailored, findings);

    expect(resume.work.map((w) => w.id)).toEqual([KESTREL, NORTHGATE, INTERN]);
    expect(resume.work[0].startDate).toBe("March 2023");
    expect(resume.skills.map((s) => s.name)).not.toContain("Rust");
    expect(resume.work[0].highlights[1]).toBe(base.work[0].highlights[1]);
    expect(revisions.map((r) => r.action).sort()).toEqual(["dropped", "dropped", "replaced", "reverted"]);
    expect(checkRules(base, resume)).toEqual([]);
  });

  it("drops a flagged bullet that resembles nothing, rather than guessing", () => {
    const base = baseResume();
    const tailored = draft();
    tailored.work[0].highlights.push("Shipped a Kubernetes operator for GPU scheduling.");

    const { resume, revisions } = reviseTailored(base, tailored, checkRules(base, tailored));
    expect(resume.work[0].highlights).toEqual(base.work[0].highlights);
    expect(revisions[0].action).toBe("dropped");
  });

  it("never duplicates an original bullet that is already on the resume", () => {
    const base = baseResume();
    const tailored = draft();
    // Two rewrites of the same original: one kept verbatim, one embellished.
    tailored.work[0].highlights = [
      base.work[0].highlights[2],
      "Mentored two junior engineers through their first on-call rotations and led hiring for 5 roles.",
    ];

    const { resume } = reviseTailored(base, tailored, checkRules(base, tailored));
    expect(resume.work[0].highlights).toEqual([base.work[0].highlights[2]]);
  });

  it("drops only the unsupported summary sentence, and falls back to the base summary if none survive", () => {
    const base = baseResume();
    const partly = draft();
    partly.summary = "Backend engineer with four years of experience. Deep expertise in Rust.";
    const one = reviseTailored(base, partly, checkRules(base, partly));
    expect(one.resume.summary).toBe("Backend engineer with four years of experience.");

    const wholly = draft();
    wholly.summary = "Engineer with 9 years of experience. Deep expertise in Rust.";
    const all = reviseTailored(base, wholly, checkRules(base, wholly));
    expect(all.resume.summary).toBe(base.summary);
    expect(checkRules(base, all.resume)).toEqual([]);
  });
});

describe("verifyTailored with a judge", () => {
  const embellished =
    "Led the team that rebuilt the shipment-tracking ingestion service in TypeScript on Node.js, cutting latency from 840 ms to 190 ms.";

  it("asks the judge only about rewritten lines that passed the rules", async () => {
    const base = baseResume();
    const tailored = draft();
    tailored.work[0].highlights[0] = embellished; // passes the rules, fails the judge
    tailored.work[0].highlights[1] = "Partitioned a table, raising revenue 40%."; // fails the rules

    const judge = vi.fn<Judge>(async ({ claims }) => {
      return new Map(
        claims.map((claim) => [
          claim.key,
          claim.text.startsWith("Led")
            ? { supported: false, reason: "The original does not say they led a team." }
            : { supported: true, reason: null },
        ]),
      );
    });

    const result = await verifyTailored(base, tailored, [], {}, { judge });
    expect(judge).toHaveBeenCalledTimes(1);
    expect(judge.mock.calls[0][0].claims.map((c) => c.text)).toEqual([embellished]);
    expect(result.modelChecked).toBe(1);
    expect(result.findings.map((f) => f.source)).toEqual(["rule", "model"]);

    const { resume, revisions } = reviseTailored(base, tailored, result.findings);
    expect(resume.work[0].highlights.slice(0, 2)).toEqual(base.work[0].highlights.slice(0, 2));
    const verification = finalizeVerification({ base, final: resume, result, revisions });
    expect(verification).toMatchObject({ passed: true, revised: true, unsupported: [], modelError: null });
    expect(verification.revisions).toHaveLength(2);
  });

  it("does not call the judge when nothing was rewritten", async () => {
    const judge = vi.fn<Judge>();
    const tailored = draft();
    tailored.work[0].highlights.reverse();
    const result = await verifyTailored(baseResume(), tailored, [], {}, { judge });
    expect(judge).not.toHaveBeenCalled();
    expect(result.findings).toEqual([]);
  });

  it("treats a line the judge skipped as unverified", async () => {
    const tailored = draft();
    tailored.work[0].highlights[0] = embellished;
    const result = await verifyTailored(baseResume(), tailored, [], {}, {
      judge: async () => new Map(),
    });
    expect(result.findings).toHaveLength(1);
    expect(result.findings[0].source).toBe("model");
  });

  it("reverts every rewrite when the judge cannot run", async () => {
    const base = baseResume();
    const tailored = draft();
    tailored.work[0].highlights[0] = embellished;
    tailored.summary = "Backend engineer with four years of experience building Node.js services.";

    const result = await verifyTailored(base, tailored, [], {}, {
      judge: async () => {
        throw new Error("provider unavailable");
      },
    });
    expect(result.modelError).toBe("provider unavailable");

    const { resume, revisions } = reviseTailored(base, tailored, result.findings);
    expect(resume.work[0].highlights[0]).toBe(base.work[0].highlights[0]);
    expect(resume.summary).toBe(base.summary);
    const verification = finalizeVerification({ base, final: resume, result, revisions });
    expect(verification.passed).toBe(true);
    expect(verification.modelError).toBe("provider unavailable");
  });
});

describe("reconcileChanges", () => {
  it("removes entries for reverted lines and logs rewrites the tailor forgot", () => {
    const base = baseResume();
    const final = draft();
    final.work[1].highlights[0] =
      "Built a customer-facing reporting API with Python and FastAPI for roughly 1,200 daily active accounts.";

    const changes: TailorChange[] = [
      {
        section: "work",
        item_id: KESTREL,
        before: base.work[0].highlights[0],
        after: "Led the rebuild of the ingestion service.",
        reason: "Leadership",
        evidence_fact_ids: [KESTREL],
      },
      {
        section: "work",
        item_id: "w-invented",
        before: null,
        after: "Scaled payments.",
        reason: "Payments",
        evidence_fact_ids: [],
      },
      {
        section: "skills",
        item_id: null,
        before: null,
        after: "Led with TypeScript and PostgreSQL",
        reason: "Matches the stack",
        evidence_fact_ids: ["s1", "s3", "not-an-id"],
      },
    ];

    const out = reconcileChanges({
      base,
      final,
      changes,
      revisions: [
        {
          section: "work",
          item_id: KESTREL,
          text: "Led the rebuild of the ingestion service.",
          reason: "x",
          source: "model",
          action: "replaced",
          replacement: base.work[0].highlights[0],
        },
      ],
    });

    expect(out).toHaveLength(2);
    expect(out[0]).toMatchObject({ section: "skills", evidence_fact_ids: ["s1", "s3"] });
    expect(out[1]).toMatchObject({
      section: "work",
      item_id: NORTHGATE,
      before: base.work[1].highlights[0],
      after: final.work[1].highlights[0],
    });
  });
});
