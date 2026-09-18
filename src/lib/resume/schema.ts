import { z } from "zod";

/**
 * The canonical resume shape — JSON-Resume-like, but with provenance.
 *
 * Every item carries `id` and `source`. That is what makes a later import
 * safe: merging a LinkedIn export (Step 9) must never overwrite something the
 * user typed, and tailoring (Step 20) has to cite the id of the base fact each
 * changed bullet rests on. Without per-item provenance neither is possible.
 */

/** Where an item came from. `user` always wins a merge. */
export const ItemSource = z.enum(["resume", "linkedin_pdf", "user"]);
export type ItemSource = z.infer<typeof ItemSource>;

/**
 * Dates stay as the resume wrote them: "2024-06", "June 2024", "Present".
 * Normalizing at extraction time would invent precision the document does not
 * have, and a tailored resume must reproduce dates verbatim.
 */
const DateString = z.string().min(1).max(40);

const withProvenance = {
  /** Stable within a resume; regenerated only when an item is created. */
  id: z.string(),
  source: ItemSource.default("resume"),
};

export const Basics = z.object({
  name: z.string().default(""),
  headline: z.string().nullish(),
  email: z.string().nullish(),
  phone: z.string().nullish(),
  location: z.string().nullish(),
  links: z
    .array(
      z.object({
        ...withProvenance,
        label: z.string(),
        url: z.string(),
      }),
    )
    .default([]),
});

export const WorkItem = z.object({
  ...withProvenance,
  company: z.string(),
  position: z.string(),
  location: z.string().nullish(),
  startDate: DateString.nullish(),
  endDate: DateString.nullish(),
  summary: z.string().nullish(),
  /** One bullet per achievement, exactly as written. */
  highlights: z.array(z.string()).default([]),
});

export const EducationItem = z.object({
  ...withProvenance,
  institution: z.string(),
  area: z.string().nullish(),
  studyType: z.string().nullish(),
  startDate: DateString.nullish(),
  endDate: DateString.nullish(),
  score: z.string().nullish(),
  highlights: z.array(z.string()).default([]),
});

export const ProjectItem = z.object({
  ...withProvenance,
  name: z.string(),
  description: z.string().nullish(),
  url: z.string().nullish(),
  startDate: DateString.nullish(),
  endDate: DateString.nullish(),
  highlights: z.array(z.string()).default([]),
});

export const SkillItem = z.object({
  ...withProvenance,
  name: z.string(),
  /** The resume's own grouping, e.g. "Languages". Never inferred. */
  category: z.string().nullish(),
});

export const ResumeSchema = z.object({
  basics: Basics.default({ name: "", links: [] }),
  summary: z.string().nullish(),
  work: z.array(WorkItem).default([]),
  education: z.array(EducationItem).default([]),
  projects: z.array(ProjectItem).default([]),
  skills: z.array(SkillItem).default([]),
});

export type Resume = z.infer<typeof ResumeSchema>;
export type WorkItem = z.infer<typeof WorkItem>;
export type EducationItem = z.infer<typeof EducationItem>;
export type ProjectItem = z.infer<typeof ProjectItem>;
export type SkillItem = z.infer<typeof SkillItem>;

/**
 * What the model is asked to produce.
 *
 * Written out by hand rather than derived from the storage schema, because the
 * two have genuinely different constraints. OpenAI's strict structured-output
 * mode requires *every* property to appear in the schema's `required` array:
 * an optional field — which is what Zod's `.optional()`, `.nullish()` and
 * `.default()` all produce — is rejected outright with an invalid_json_schema
 * error. So absence is expressed as `.nullable()` here and nothing is optional.
 *
 * `id` and `source` are absent for a different reason: they are ours to assign.
 * Asking a model to invent stable ids invites it to reuse or collide them, and
 * the extraction prompt has no business knowing about provenance.
 */

const NullableDate = DateString.nullable();

const ExtractedLink = z.object({
  label: z.string(),
  url: z.string(),
});

const ExtractedWork = z.object({
  company: z.string(),
  position: z.string(),
  location: z.string().nullable(),
  startDate: NullableDate,
  endDate: NullableDate,
  summary: z.string().nullable(),
  highlights: z.array(z.string()),
});

const ExtractedEducation = z.object({
  institution: z.string(),
  area: z.string().nullable(),
  studyType: z.string().nullable(),
  startDate: NullableDate,
  endDate: NullableDate,
  score: z.string().nullable(),
  highlights: z.array(z.string()),
});

const ExtractedProject = z.object({
  name: z.string(),
  description: z.string().nullable(),
  url: z.string().nullable(),
  startDate: NullableDate,
  endDate: NullableDate,
  highlights: z.array(z.string()),
});

const ExtractedSkill = z.object({
  name: z.string(),
  category: z.string().nullable(),
});

export const ExtractedResumeSchema = z.object({
  basics: z.object({
    name: z.string(),
    headline: z.string().nullable(),
    email: z.string().nullable(),
    phone: z.string().nullable(),
    location: z.string().nullable(),
    links: z.array(ExtractedLink),
  }),
  summary: z.string().nullable(),
  work: z.array(ExtractedWork),
  education: z.array(ExtractedEducation),
  projects: z.array(ExtractedProject),
  skills: z.array(ExtractedSkill),
});

export type ExtractedResume = z.infer<typeof ExtractedResumeSchema>;

/** Assigns ids and provenance to a freshly extracted resume. */
export function withIds(
  extracted: ExtractedResume,
  source: ItemSource,
  newId: () => string = () => crypto.randomUUID(),
): Resume {
  const stamp = <T extends object>(item: T) => ({
    ...item,
    id: newId(),
    source,
  });

  return ResumeSchema.parse({
    basics: {
      ...extracted.basics,
      links: (extracted.basics.links ?? []).map(stamp),
    },
    summary: extracted.summary,
    work: extracted.work.map(stamp),
    education: extracted.education.map(stamp),
    projects: extracted.projects.map(stamp),
    skills: extracted.skills.map(stamp),
  });
}

/** Every factual item, flattened — what the tailoring verifier checks against. */
export function factIds(resume: Resume): string[] {
  return [
    ...resume.basics.links,
    ...resume.work,
    ...resume.education,
    ...resume.projects,
    ...resume.skills,
  ].map((item) => item.id);
}
