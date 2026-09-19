import { tool } from "ai";
import { z } from "zod";

import { getBaseResume } from "@/lib/db/queries/profile";
import { ResumeSchema, type Resume } from "@/lib/resume/schema";

/**
 * Reading the user's actual resume.
 *
 * Without this the agent knows only that a profile exists and how many entries
 * it has — enough to say "you have three roles on file" and nothing else. It
 * could not answer "what's on my resume?", could not say whether a posting
 * matches the user's background, and could not draft anything grounded in it,
 * while the scoring pass was reading the whole thing. Counts are not a profile.
 *
 * Every item carries its id, because tailoring (Step 20) cites the base fact
 * each changed bullet rests on, and the agent needs to be able to name them.
 */

const SECTIONS = [
  "basics",
  "summary",
  "work",
  "education",
  "projects",
  "skills",
] as const;

/** Compact, readable rendering — a resume is prose, not a JSON blob. */
function render(resume: Resume, sections: Set<string>): string {
  const parts: string[] = [];

  if (sections.has("basics")) {
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

  if (sections.has("summary") && resume.summary) {
    parts.push(`SUMMARY\n${resume.summary}`);
  }

  if (sections.has("work") && resume.work.length) {
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

  if (sections.has("education") && resume.education.length) {
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

  if (sections.has("projects") && resume.projects.length) {
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

  if (sections.has("skills") && resume.skills.length) {
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

export function getResume(userId: string) {
  return tool({
    description:
      "Read the user's actual resume — their roles, bullets, education, " +
      "projects and skills as written. Call this before saying anything about " +
      "their background, judging whether a job fits them, or drafting " +
      "anything grounded in their experience. get_profile_summary only " +
      "reports how much is on file, not what it says.",
    inputSchema: z.object({
      sections: z
        .array(z.enum(SECTIONS))
        .nullable()
        .describe(
          "Limit to these sections. Null reads the whole resume, which is " +
            "usually what you want.",
        ),
    }),
    execute: async ({ sections }) => {
      const stored = await getBaseResume(userId);
      if (!stored) {
        return {
          ok: false,
          message:
            "No resume on file. The user needs to upload one on the profile page.",
        };
      }

      const parsed = ResumeSchema.safeParse(stored.data);
      if (!parsed.success) {
        return {
          ok: false,
          message: "The stored resume could not be read; it may need re-uploading.",
        };
      }

      const wanted = new Set<string>(sections?.length ? sections : SECTIONS);

      return {
        ok: true,
        version: stored.version,
        // Rendered rather than raw JSON: the model reads this, and a resume is
        // prose. Ids are kept inline so it can cite specific facts.
        resume: render(parsed.data, wanted),
      };
    },
  });
}
