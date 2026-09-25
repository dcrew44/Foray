"use server";

import { revalidatePath } from "next/cache";

import { requireUser } from "@/lib/auth/session";
import { SettingsSchema, updateSettings, type Settings } from "@/lib/profile/settings";

/**
 * Saves the "How Foray works for you" settings. Partial: only the fields sent
 * change, and each is validated against the same schema the agents read.
 */
export async function saveSettingsAction(
  patch: Partial<Settings>,
): Promise<{ ok: true; message: string; settings: Settings } | { ok: false; message: string }> {
  const user = await requireUser();

  const parsed = SettingsSchema.partial().strict().safeParse(patch);
  if (!parsed.success) {
    return { ok: false, message: "Those settings are out of range. Check the values and try again." };
  }

  // Drop keys the schema filled with defaults but the caller did not send,
  // so a partial save never resets a field to its default by accident.
  const sent = Object.fromEntries(
    Object.entries(parsed.data).filter(([key]) => key in patch),
  ) as Partial<Settings>;

  const settings = await updateSettings(user.id, sent);
  if (!settings) {
    return {
      ok: false,
      message: "Upload your resume first. Settings are saved on the profile it creates.",
    };
  }

  revalidatePath("/settings");
  return { ok: true, message: "Settings saved", settings };
}
