import { z } from "zod";

import type { Job, JobMatch } from "@/lib/db/schema";
import type { LlmContext } from "@/lib/llm/context";
import { generateStructured } from "@/lib/llm/structured";
import type { Preferences } from "@/lib/profile/interview";
import { htmlToText } from "@/lib/sources/clean";

import { ResumeSchema, type Resume } from "./schema";
import { resumeToText } from "./text";

/**
 * Tailoring: the base resume, edited for one posting.
 *
 * The model is an editor here, never an author. It may choose, reorder and
 * reword what the base resume already says; it may not add a fact. That rule
 * is stated plainly in the instructions, but instructions are not a guarantee
 * — lib/resume/verify.ts checks the output against the base facts and reverts
 * anything unsupported before a tailored resume is saved.
 *
 * This module only produces the draft: the prompt, the model call, and the
 * mapping of the model's output back onto base items by id.
 */

export const TAILOR_SECTIONS = [
  "summary",
  "work",
  "projects",
  "education",
  "skills",
] as const;

export type TailorSection = (typeof TAILOR_SECTIONS)[number];

/**
 * One edit, as stored in `resumes.changes`: the audit trail that makes a
 * tailored claim checkable against the base facts it rests on.
 */
export type TailorChange = {
  section: TailorSection;
  /** The base item the edit belongs to; null for the summary and skills. */
  item_id: string | null;
  /** The base text, or null when nothing was replaced. */
  before: string | null;
  /** The tailored text, or null when something was dropped. */
  after: string | null;
  reason: string;
  /** Base item ids the new text rests on. */
  evidence_fact_ids: string[];
};

/** How much of a posting the tailor reads. */
const MAX_POSTING_CHARS = 10_000;

/**
 * What the model returns.
 *
 * Same shape as the extraction schema, with two differences. Every item
 * carries the `id` of the base item it came from, because a tailored item can
 * only be an edited base item. And basics are absent: they are copied from the
 * base verbatim, so asking the model to echo them would only create a chance
 * to change a phone number. Skills are names only for the same reason — the
 * grouping is the resume's own and is copied from the base.
 *
 * Every property is required with absence as null (OpenAI strict mode).
 */
const TailoredWork = z.object({
  id: z.string(),
  company: z.string(),
  position: z.string(),
  location: z.string().nullable(),
  startDate: z.string().nullable(),
  endDate: z.string().nullable(),
  summary: z.string().nullable(),
  highlights: z.array(z.string()),
});

const TailoredEducation = z.object({
  id: z.string(),
  institution: z.string(),
  area: z.string().nullable(),
  studyType: z.string().nullable(),
  startDate: z.string().nullable(),
  endDate: z.string().nullable(),
  score: z.string().nullable(),
  highlights: z.array(z.string()),
});

const TailoredProject = z.object({
  id: z.string(),
  name: z.string(),
  description: z.string().nullable(),
  url: z.string().nullable(),
  startDate: z.string().nullable(),
  endDate: z.string().nullable(),
  highlights: z.array(z.string()),
});

const Change = z.object({
  section: z.enum(TAILOR_SECTIONS),
  item_id: z.string().nullable(),
  before: z.string().nullable(),
  after: z.string().nullable(),
  reason: z.string(),
  evidence_fact_ids: z.array(z.string()),
});

export const TailorOutputSchema = z.object({
  summary: z.string().nullable(),
  work: z.array(TailoredWork),
  education: z.array(TailoredEducation),
  projects: z.array(TailoredProject),
  /** Names of base skills, most relevant first. */
  skills: z.array(z.string()),
  changes: z.array(Change),
});

export type TailorOutput = z.infer<typeof TailorOutputSchema>;

const INSTRUCTIONS = `You tailor a candidate's resume to one job posting. You are an editor, not an author: every statement on the tailored resume must already be true on the original resume below.

What you should do — a tailored resume that reads exactly like the original has not been tailored:
- Select. Keep the roles, projects, bullets and skills that matter most for this posting. Drop weak or irrelevant bullets rather than padding. Keep the most recent role and every education entry.
- Reorder bullets within each role, and reorder projects and skills, so the most relevant to this posting come first. Keep roles and education in their original order.
- Rephrase bullets so each leads with what this posting cares about — the outcome, the kind of system, or the technology it asks for — using the posting's vocabulary only where it means exactly the same thing as the original ("Postgres" for "PostgreSQL" is fine; "Kubernetes" for "Docker" is not; "APIs" for "services" is not). Rewording that only moves emphasis is exactly the job; a bullet that already fits well can stay as it is.
- Rewrite the summary for this posting, using only facts stated somewhere in the resume. Do not name the target company.

Every rewritten line is checked against the original item it sits under, and anything unsupported is reverted, so a rewrite that adds a fact gains nothing.

What you must never do:
- Add an employer, job title, date, degree, school, certification, skill, tool, technology, metric or number that is not in the original. That includes numbers computed from existing ones: "from 840 ms to 190 ms" must not become "77% faster" or "4x faster".
- Move a fact between items. A bullet may only use facts from the role, project or education entry it sits under.
- Claim more ownership, scope or seniority than the original shows: "contributed to" does not become "led", "helped build" does not become "architected".
- Claim anything listed under Gaps — those are things the candidate does not have.

Copy exactly, character for character, from the Exact fields block:
- the id of every item you keep;
- company, position, location, startDate and endDate for roles; institution, area, studyType, score, startDate and endDate for education; name, url, startDate and endDate for projects — null where the block has null;
- skill names, exactly as listed. Return a subset of them, most relevant to this posting first.
Keep an education entry's highlights and a role's summary as they are unless rewording one makes it more relevant; the same rules apply.

Length: the tailored resume should fit on one page. Roughly: at most 4–5 bullets for the most relevant role, 2–3 for older or less relevant roles, 1–3 for a project, and a summary of two or three sentences. Shorter and sharper beats complete.

changes: one entry per edit — each rewritten bullet (before = the original bullet, after = your version), each dropped bullet or item (after = null), the summary rewrite, and one entry describing the skills selection (section "skills", item_id null). reason says in a few words why the edit helps for this posting. evidence_fact_ids lists the [id]s of the original items the new text rests on. Do not log items you left untouched.`;

/** What a deep evaluation (or, failing that, the fast score) says about this match. */
export type EvaluationNotes = {
  matched: string[];
  talkingPoints: string[];
  gaps: string[];
};

/**
 * Reads the evaluations leniently: they are written by another pass, and a
 * shape change there should cost the tailor its hints, not fail the run.
 */
export function evaluationNotes(
  match: Pick<JobMatch, "deepEval" | "fastEval"> | null,
): EvaluationNotes {
  const deep = asRecord(match?.deepEval);
  const fast = asRecord(match?.fastEval);

  const matched = list(deep.matched_requirements, (item) => {
    if (typeof item === "string") return item;
    const record = asRecord(item);
    const requirement = str(record.requirement);
    const evidence = str(record.evidence);
    return requirement && evidence
      ? `${requirement} — shown by: ${evidence}`
      : requirement || evidence;
  });

  const gaps = [
    ...list(deep.gaps, (item) => {
      if (typeof item === "string") return item;
      const record = asRecord(item);
      const gap = str(record.gap);
      const note = str(record.note);
      return gap && note ? `${gap} (${note})` : gap;
    }),
    ...list(fast.hard_requirement_misses, (item) => str(item)),
  ];

  return {
    matched,
    talkingPoints: list(deep.talking_points, (item) => str(item)),
    gaps: [...new Set(gaps)].slice(0, 12),
  };
}

export type PostingInput = Pick<
  Job,
  "title" | "companyName" | "locations" | "remote" | "descriptionText" | "descriptionRaw"
>;

/**
 * The exact values the model must copy. `resumeToText` is written for reading
 * and renders a missing date as "?" or "present"; a tailor copying from it
 * would faithfully reproduce the placeholder. This block has the real values,
 * plus the education highlights and project dates the prose leaves out.
 */
function exactFields(base: Resume) {
  return {
    work: base.work.map((item) => ({
      id: item.id,
      company: item.company,
      position: item.position,
      location: item.location ?? null,
      startDate: item.startDate ?? null,
      endDate: item.endDate ?? null,
    })),
    education: base.education.map((item) => ({
      id: item.id,
      institution: item.institution,
      area: item.area ?? null,
      studyType: item.studyType ?? null,
      score: item.score ?? null,
      startDate: item.startDate ?? null,
      endDate: item.endDate ?? null,
      highlights: item.highlights,
    })),
    projects: base.projects.map((item) => ({
      id: item.id,
      name: item.name,
      url: item.url ?? null,
      startDate: item.startDate ?? null,
      endDate: item.endDate ?? null,
    })),
    skills: base.skills.map((skill) => skill.name),
  };
}

export function postingText(job: PostingInput): string {
  const description =
    job.descriptionText?.trim() ||
    (job.descriptionRaw ? htmlToText(job.descriptionRaw) : "");
  return description.slice(0, MAX_POSTING_CHARS) || "(no description)";
}

/**
 * Instructions and the candidate's facts first, the posting last: the stable
 * prefix is what a provider's prompt cache can reuse when the same user
 * tailors for several jobs.
 */
export function buildTailorPrompt(input: {
  base: Resume;
  job: PostingInput;
  notes?: EvaluationNotes;
  preferences?: Preferences | null;
}): { instructions: string; prompt: string } {
  const { base, job, notes, preferences } = input;

  const instructions = `${INSTRUCTIONS}

<resume>
${resumeToText(base)}
</resume>

<exact_fields>
${JSON.stringify(exactFields(base), null, 1)}
</exact_fields>`;

  const goals = [
    preferences?.target_roles?.length
      ? `Target roles: ${preferences.target_roles.join(", ")}`
      : null,
    preferences?.seniority ? `Seniority: ${preferences.seniority}` : null,
  ].filter(Boolean);

  const bullets = (items: string[]) => items.map((item) => `- ${item}`).join("\n");

  const evaluation = notes
    ? [
        notes.matched.length
          ? `What the posting asks for that the candidate has:\n${bullets(notes.matched)}`
          : null,
        notes.talkingPoints.length
          ? `Worth emphasising:\n${bullets(notes.talkingPoints)}`
          : null,
        notes.gaps.length
          ? `Gaps — the candidate does NOT have these; never claim or imply them:\n${bullets(notes.gaps)}`
          : null,
      ].filter(Boolean)
    : [];

  const prompt = [
    `Tailor the resume for this posting.`,
    `<posting>
Title: ${job.title}
Company: ${job.companyName}
Location: ${job.locations.join(" | ") || "unstated"} (${job.remote})

${postingText(job)}
</posting>`,
    evaluation.length ? `<evaluation>\n${evaluation.join("\n\n")}\n</evaluation>` : null,
    goals.length ? `<candidate_goals>\n${goals.join("\n")}\n</candidate_goals>` : null,
  ]
    .filter(Boolean)
    .join("\n\n");

  return { instructions, prompt };
}

/** Something the mapping refused to carry into the tailored resume. */
export type MappingDrop = {
  section: TailorSection;
  item_id: string | null;
  text: string;
  reason: string;
};

export type MappedTailoring = {
  resume: Resume;
  changes: TailorChange[];
  dropped: MappingDrop[];
};

/**
 * Maps the model's output onto the storage shape, anchored to the base.
 *
 * - Basics come from the base verbatim.
 * - An item is only kept if its id is a base item's id; `source` is the base
 *   item's. An invented item is dropped here and reported.
 * - Skills are matched to base skills by name; the id, provenance and category
 *   come from the base. An unknown skill is dropped and reported.
 * - Roles and education keep the base order, whatever order the model used.
 *
 * Everything else — the fields the model was told to copy, the bullets it
 * rewrote — is kept as the model wrote it, for the verifier to judge.
 */
export function mapTailoredOutput(base: Resume, output: TailorOutput): MappedTailoring {
  const dropped: MappingDrop[] = [];

  const clean = (values: string[]) =>
    values.map((value) => value.trim()).filter((value) => value.length > 0);
  const nullable = (value: string | null) => (value?.trim() ? value.trim() : null);
  // The storage schema caps dates at 40 characters. Anything longer cannot be
  // a verbatim copy of a base date, so it is replaced by the base value here
  // rather than failing the whole run at parse time.
  const date = (value: string | null, original: string | null | undefined) => {
    const trimmed = nullable(value);
    return trimmed && trimmed.length > 40 ? (original ?? null) : trimmed;
  };

  function anchor<B extends { id: string; source: Resume["work"][number]["source"] }, T extends { id: string }>(
    section: "work" | "education" | "projects",
    baseItems: B[],
    items: T[],
    describe: (item: T) => string,
    keepBaseOrder: boolean,
  ): Array<{ base: B; item: T }> {
    const byId = new Map(baseItems.map((item, index) => [item.id, { item, index }]));
    const seen = new Set<string>();
    const kept: Array<{ base: B; item: T; index: number }> = [];

    for (const item of items) {
      const id = item.id.trim();
      const match = byId.get(id);
      if (!match) {
        dropped.push({
          section,
          item_id: id || null,
          text: describe(item),
          reason: `Not ${section === "work" ? "a role" : section === "education" ? "an education entry" : "a project"} on your resume, so it was removed.`,
        });
        continue;
      }
      if (seen.has(id)) continue;
      seen.add(id);
      kept.push({ base: match.item, item, index: match.index });
    }

    if (keepBaseOrder) kept.sort((a, b) => a.index - b.index);
    return kept;
  }

  const work = anchor(
    "work",
    base.work,
    output.work,
    (item) => [item.position, item.company].filter(Boolean).join(" — "),
    true,
  ).map(({ base: original, item }) => ({
    id: original.id,
    source: original.source,
    company: item.company.trim(),
    position: item.position.trim(),
    location: nullable(item.location),
    startDate: date(item.startDate, original.startDate),
    endDate: date(item.endDate, original.endDate),
    summary: nullable(item.summary),
    highlights: clean(item.highlights),
  }));

  const education = anchor(
    "education",
    base.education,
    output.education,
    (item) => item.institution,
    true,
  ).map(({ base: original, item }) => ({
    id: original.id,
    source: original.source,
    institution: item.institution.trim(),
    area: nullable(item.area),
    studyType: nullable(item.studyType),
    startDate: date(item.startDate, original.startDate),
    endDate: date(item.endDate, original.endDate),
    score: nullable(item.score),
    highlights: clean(item.highlights),
  }));

  const projects = anchor(
    "projects",
    base.projects,
    output.projects,
    (item) => item.name,
    false,
  ).map(({ base: original, item }) => ({
    id: original.id,
    source: original.source,
    name: item.name.trim(),
    description: nullable(item.description),
    url: nullable(item.url),
    startDate: date(item.startDate, original.startDate),
    endDate: date(item.endDate, original.endDate),
    highlights: clean(item.highlights),
  }));

  const skillsByName = new Map(base.skills.map((skill) => [skillKey(skill.name), skill]));
  const skillSeen = new Set<string>();
  const skills: Resume["skills"] = [];
  for (const name of output.skills) {
    const key = skillKey(name);
    if (!key || skillSeen.has(key)) continue;
    const original = skillsByName.get(key);
    if (!original) {
      dropped.push({
        section: "skills",
        item_id: null,
        text: name.trim(),
        reason: `${name.trim()} is not a skill on your resume, so it was removed.`,
      });
      continue;
    }
    skillSeen.add(key);
    skills.push(structuredClone(original));
  }

  const resume = ResumeSchema.parse({
    basics: structuredClone(base.basics),
    summary: nullable(output.summary),
    work,
    education,
    projects,
    skills,
  });

  return {
    resume,
    changes: output.changes.map((change) => ({
      section: change.section,
      item_id: nullable(change.item_id),
      before: nullable(change.before),
      after: nullable(change.after),
      reason: change.reason.trim(),
      evidence_fact_ids: clean(change.evidence_fact_ids),
    })),
    dropped,
  };
}

/** Case- and whitespace-insensitive skill identity. */
export function skillKey(name: string): string {
  return name.normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim();
}

export type TailorDraft = MappedTailoring & { retried: boolean };

export async function tailorResume(input: {
  base: Resume;
  job: PostingInput;
  match?: Pick<JobMatch, "deepEval" | "fastEval"> | null;
  preferences?: Preferences | null;
  context?: LlmContext;
}): Promise<TailorDraft> {
  const { instructions, prompt } = buildTailorPrompt({
    base: input.base,
    job: input.job,
    notes: evaluationNotes(input.match ?? null),
    preferences: input.preferences,
  });

  const { value, retried } = await generateStructured<TailorOutput>(
    "tailor",
    TailorOutputSchema,
    { instructions, prompt },
    input.context ?? {},
  );

  return { ...mapTailoredOutput(input.base, value), retried };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function str(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function list(value: unknown, read: (item: unknown) => string, max = 10): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(read)
    .filter((item) => item.length > 0)
    .slice(0, max)
    .map((item) => (item.length > 300 ? `${item.slice(0, 297)}…` : item));
}
