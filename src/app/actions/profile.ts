"use server";

import { revalidatePath } from "next/cache";

import { requireUser } from "@/lib/auth/session";
import { getProfile, updateProfileData } from "@/lib/db/queries/profile";
import { stampUserEdits } from "@/lib/resume/edits";
import { ResumeSchema } from "@/lib/resume/schema";

/**
 * Saves the profile editor's contents.
 *
 * The incoming resume is validated rather than trusted — it arrives from the
 * browser — and then diffed against what is stored so only the items the user
 * actually changed are re-sourced to `user`. See resume/edits.ts for why that
 * matters to a later LinkedIn merge.
 */
export async function saveProfile(
  next: unknown,
): Promise<{ ok: true } | { ok: false; error: string }> {
  const user = await requireUser();

  const parsed = ResumeSchema.safeParse(next);
  if (!parsed.success) {
    return { ok: false, error: parsed.error.issues[0]?.message ?? "Invalid profile." };
  }

  const existing = await getProfile(user.id);
  if (!existing) {
    return { ok: false, error: "Upload a resume before editing your profile." };
  }

  // The stored side is re-parsed too, so both sides of the diff are normalized
  // the same way before their contents are compared.
  const storedParse = ResumeSchema.safeParse(existing.data);
  const stamped = stampUserEdits(
    parsed.data,
    storedParse.success ? storedParse.data : null,
  );

  await updateProfileData(user.id, stamped);
  revalidatePath("/profile");
  return { ok: true };
}
