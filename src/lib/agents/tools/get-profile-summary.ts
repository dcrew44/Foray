import { tool } from "ai";
import { eq } from "drizzle-orm";
import { z } from "zod";

import { db } from "@/lib/db";
import { profiles } from "@/lib/db/schema";

/**
 * The first tool: what do we already know about this user?
 *
 * Tools are built per request with the signed-in user's id closed over, so a
 * tool can never be talked into reading someone else's row — the model has no
 * way to name a different user.
 */
export function getProfileSummary(userId: string) {
  return tool({
    description:
      "How complete the user's profile is, and what they have said they are " +
      "looking for. This reports counts, NOT resume content — to read what " +
      "their resume actually says, use get_resume.",
    inputSchema: z.object({}),
    execute: async () => {
      const [profile] = await db
        .select()
        .from(profiles)
        .where(eq(profiles.userId, userId))
        .limit(1);

      if (!profile) {
        return {
          hasProfile: false,
          note: "No profile yet. The user still needs to upload a resume.",
        };
      }

      const data = (profile.data ?? {}) as Record<string, unknown[]>;
      const preferences = (profile.preferences ?? {}) as Record<string, unknown>;

      return {
        hasProfile: true,
        counts: {
          work: data.work?.length ?? 0,
          education: data.education?.length ?? 0,
          skills: data.skills?.length ?? 0,
          projects: data.projects?.length ?? 0,
        },
        preferences,
        knownPreferences: Object.keys(preferences),
      };
    },
  });
}
