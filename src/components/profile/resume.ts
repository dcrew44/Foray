import type { components } from "@/lib/api/schema";
import type { Resume, SkillItem } from "@/lib/api/types";

/**
 * The resume as the editor holds it: every list present.
 *
 * Django always sends the whole stored resume (`GET /api/pages/profile` dumps every field), but
 * the generated `Resume` type is the input schema, where a field with a default (`work`,
 * `highlights`, `links`, …) may be left out. `editableResume` fills those in once, so the editor
 * never has to ask. What it sends back (`PUT /api/resume`) is a plain `Resume`.
 */

type S = components["schemas"];
type WithHighlights<T> = T & { highlights: string[] };

export type EditableResume = {
  basics: S["Basics"] & { links: Array<S["Link"]> };
  summary?: string | null;
  work: Array<WithHighlights<S["WorkItem"]>>;
  education: Array<WithHighlights<S["EducationItem"]>>;
  projects: Array<WithHighlights<S["ProjectItem"]>>;
  skills: SkillItem[];
};

export function editableResume(resume: Resume): EditableResume {
  const highlights = <T extends { highlights?: string[] }>(item: T) => ({
    ...item,
    highlights: item.highlights ?? [],
  });
  const basics = resume.basics ?? { name: "" };
  return {
    basics: { ...basics, name: basics.name ?? "", links: basics.links ?? [] },
    summary: resume.summary ?? null,
    work: (resume.work ?? []).map(highlights),
    education: (resume.education ?? []).map(highlights),
    projects: (resume.projects ?? []).map(highlights),
    skills: resume.skills ?? [],
  };
}
