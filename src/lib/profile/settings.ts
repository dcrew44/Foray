import { eq } from "drizzle-orm";
import { z } from "zod";

import { db } from "@/lib/db";
import { profiles } from "@/lib/db/schema";

/**
 * How the agents behave for one user.
 *
 * Stored on `profiles.settings`. Every field is defaulted so a row written
 * before a field existed still parses to something usable — the same rule the
 * preferences schema follows.
 */

export const AutonomyMode = z.enum(["approve_each", "autopilot"]);
export type AutonomyMode = z.infer<typeof AutonomyMode>;

export const SettingsSchema = z.object({
  /**
   * What a search does when the user does not say. `approve_each` stops before
   * spending on deep evaluation and before shortlisting; `autopilot` does both.
   */
  defaultMode: AutonomyMode.default("approve_each"),
  /** Fast scores below this are archived with a note rather than shown. */
  scoreThreshold: z.number().int().min(0).max(100).default(55),
  /** How many of the best fast-scored matches get the strong-model review. */
  deepEvalTopN: z.number().int().min(0).max(60).default(15),
});

export type Settings = z.infer<typeof SettingsSchema>;

export const DEFAULT_SETTINGS: Settings = SettingsSchema.parse({});

export function parseSettings(value: unknown): Settings {
  return SettingsSchema.safeParse(value ?? {}).data ?? DEFAULT_SETTINGS;
}

export async function getSettings(userId: string): Promise<Settings> {
  const [row] = await db
    .select({ settings: profiles.settings })
    .from(profiles)
    .where(eq(profiles.userId, userId))
    .limit(1);
  return parseSettings(row?.settings);
}

/**
 * Merges a partial update. Returns null when the user has no profile yet —
 * settings live on the profile row, which is created by the first resume
 * upload.
 */
export async function updateSettings(
  userId: string,
  patch: Partial<Settings>,
): Promise<Settings | null> {
  const current = await getSettings(userId);
  const next = SettingsSchema.parse({ ...current, ...patch });

  const [row] = await db
    .update(profiles)
    .set({ settings: next, updatedAt: new Date() })
    .where(eq(profiles.userId, userId))
    .returning({ id: profiles.id });

  return row ? next : null;
}
