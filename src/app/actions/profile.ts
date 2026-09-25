"use server";

import { eq } from "drizzle-orm";
import { revalidatePath } from "next/cache";

import { requireUser } from "@/lib/auth/session";
import { db } from "@/lib/db";
import { getProfile, saveEditedResume } from "@/lib/db/queries/profile";
import { profiles } from "@/lib/db/schema";
import {
  EMPTY_INTERVIEW_STATE,
  InterviewStateSchema,
  isFilled,
  PreferencesSchema,
  SLOTS,
  type Preferences,
} from "@/lib/profile/interview";
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

  await saveEditedResume(user.id, stamped);
  revalidatePath("/profile");
  return { ok: true };
}

/**
 * Saves the preferences form — the same answers the chat interview records,
 * written whole rather than merged, because the form shows every field and an
 * emptied one means "clear this".
 *
 * A slot the user now fills is no longer "declined", so the interview state
 * stays truthful for the agent that reads it.
 */
export async function savePreferencesAction(
  input: Preferences,
): Promise<{ ok: true; message: string } | { ok: false; message: string }> {
  const user = await requireUser();

  const parsed = PreferencesSchema.safeParse(input);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    const slot = SLOTS.find((s) => s.fields.includes(issue?.path[0] as keyof Preferences));
    return {
      ok: false,
      message: slot
        ? `${slot.label} could not be saved: ${issue?.message ?? "the value is not valid"}.`
        : "Those preferences could not be saved. Check the fields and try again.",
    };
  }

  const existing = await getProfile(user.id);
  if (!existing) {
    return {
      ok: false,
      message: "Upload your resume first. Your preferences are saved on the profile it creates.",
    };
  }

  const preferences = normalize(parsed.data);
  const state =
    InterviewStateSchema.safeParse(existing.interviewState).data ?? EMPTY_INTERVIEW_STATE;
  const skipped = state.skipped.filter((id) => {
    const slot = SLOTS.find((s) => s.id === id);
    return !slot || !isFilled(slot, preferences);
  });

  await db
    .update(profiles)
    .set({
      preferences,
      interviewState: { ...state, skipped },
      updatedAt: new Date(),
    })
    .where(eq(profiles.userId, user.id));

  revalidatePath("/profile");
  revalidatePath("/home");
  return { ok: true, message: "Preferences saved" };
}

/** Trimmed, de-duplicated, and empty answers stored as null ("not known"). */
function normalize(preferences: Preferences): Preferences {
  const list = (items: string[] | null) => {
    if (!items) return null;
    const seen = new Set<string>();
    const cleaned = items
      .map((item) => item.trim().slice(0, 200))
      .filter((item) => {
        const key = item.toLowerCase();
        if (!item || seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .slice(0, 40);
    return cleaned.length ? cleaned : null;
  };
  const text = (value: string | null) => value?.trim().slice(0, 500) || null;
  const salary = preferences.salary_floor;

  return {
    target_roles: list(preferences.target_roles),
    seniority: preferences.seniority,
    locations: list(preferences.locations),
    remote_pref: preferences.remote_pref,
    salary_floor:
      salary && Number.isFinite(salary.amount) && salary.amount > 0
        ? { amount: salary.amount, currency: salary.currency.trim() || "USD", period: salary.period }
        : null,
    work_auth: text(preferences.work_auth),
    start_date: text(preferences.start_date),
    must_haves: list(preferences.must_haves),
    dealbreakers: list(preferences.dealbreakers),
  };
}
