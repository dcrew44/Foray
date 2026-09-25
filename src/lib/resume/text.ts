import type { Resume } from "./schema";

/**
 * A resume as prose, for a model to read.
 *
 * Models read this rather than the JSON: a resume is prose, and the JSON's
 * nesting and provenance fields cost tokens without helping comprehension.
 * Every item keeps its id inline in brackets — `[id] Position — Company` — so
 * whatever reads it (the chat agent, the deep evaluation, the tailor) can cite
 * the exact base fact a claim rests on.
 */

export const RESUME_SECTIONS = [
  "basics",
  "summary",
  "work",
  "education",
  "projects",
  "skills",
] as const;

/** Compact, readable rendering — a resume is prose, not a JSON blob. */
export function resumeToText(
  resume: Resume,
  sections: Iterable<string> = RESUME_SECTIONS,
): string {
  const wanted = new Set(sections);
  const parts: string[] = [];

  if (wanted.has("basics")) {
    const basics = resume.basics;
    parts.push(
      [
        `Name: ${basics.name || "(unknown)"}`,
        basics.headline && `Headline: ${basics.headline}`,
        basics.location && `Location: ${basics.location}`,
        basics.email && `Email: ${basics.email}`,
        basics.links.length &&
          `Links: ${basics.links.map((link) => `${link.label} ${link.url}`).join(", ")}`,
      ]
        .filter(Boolean)
        .join("\n"),
    );
  }

  if (wanted.has("summary") && resume.summary) {
    parts.push(`SUMMARY\n${resume.summary}`);
  }

  if (wanted.has("work") && resume.work.length) {
    parts.push(
      "EXPERIENCE\n" +
        resume.work
          .map((role) =>
            [
              `[${role.id}] ${role.position} — ${role.company}` +
                (role.location ? ` (${role.location})` : ""),
              `  ${role.startDate ?? "?"} – ${role.endDate ?? "present"}` +
                ` · source: ${role.source}`,
              role.summary && `  ${role.summary}`,
              ...role.highlights.map((highlight) => `  • ${highlight}`),
            ]
              .filter(Boolean)
              .join("\n"),
          )
          .join("\n\n"),
    );
  }

  if (wanted.has("education") && resume.education.length) {
    parts.push(
      "EDUCATION\n" +
        resume.education
          .map(
            (item) =>
              `[${item.id}] ${item.studyType ?? ""} ${item.area ?? ""} — ${item.institution}` +
              ` (${item.startDate ?? "?"} – ${item.endDate ?? "?"})` +
              (item.score ? ` · ${item.score}` : ""),
          )
          .join("\n"),
    );
  }

  if (wanted.has("projects") && resume.projects.length) {
    parts.push(
      "PROJECTS\n" +
        resume.projects
          .map((project) =>
            [
              `[${project.id}] ${project.name}` +
                (project.url ? ` — ${project.url}` : ""),
              project.description && `  ${project.description}`,
              ...project.highlights.map((highlight) => `  • ${highlight}`),
            ]
              .filter(Boolean)
              .join("\n"),
          )
          .join("\n\n"),
    );
  }

  if (wanted.has("skills") && resume.skills.length) {
    const grouped = new Map<string, string[]>();
    for (const skill of resume.skills) {
      const key = skill.category ?? "Other";
      grouped.set(key, [...(grouped.get(key) ?? []), skill.name]);
    }
    parts.push(
      "SKILLS\n" +
        [...grouped]
          .map(([category, names]) => `${category}: ${names.join(", ")}`)
          .join("\n"),
    );
  }

  return parts.join("\n\n");
}
