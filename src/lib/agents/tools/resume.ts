import { tool } from "ai";
import { z } from "zod";

import { getBaseResume } from "@/lib/db/queries/profile";
import { ResumeSchema } from "@/lib/resume/schema";
import { RESUME_SECTIONS, resumeToText } from "@/lib/resume/text";

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
        .array(z.enum(RESUME_SECTIONS))
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

      const wanted = sections?.length ? sections : RESUME_SECTIONS;

      return {
        ok: true,
        version: stored.version,
        // Rendered rather than raw JSON: the model reads this, and a resume is
        // prose. Ids are kept inline so it can cite specific facts.
        resume: resumeToText(parsed.data, wanted),
      };
    },
  });
}
