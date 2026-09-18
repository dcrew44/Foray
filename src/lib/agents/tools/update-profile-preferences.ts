import { tool } from "ai";
import { eq } from "drizzle-orm";
import { z } from "zod";

import { db } from "@/lib/db";
import { profiles } from "@/lib/db/schema";
import {
  EMPTY_INTERVIEW_STATE,
  EMPTY_PREFERENCES,
  InterviewStateSchema,
  PreferencesSchema,
  RemotePreference,
  SalaryFloor,
  Seniority,
  SLOTS,
  interviewProgress,
  type Preferences,
} from "@/lib/profile/interview";

/**
 * How the interview writes its answers down.
 *
 * Every field is nullable and none is optional: OpenAI's strict tool schemas
 * require every property to be present, and `null` here has a real meaning —
 * "leave what is already stored alone". That lets the agent record two answers
 * from one message without clobbering the other six slots.
 */
const Input = z.object({
  target_roles: z.array(z.string()).nullable(),
  seniority: Seniority.nullable(),
  locations: z.array(z.string()).nullable(),
  remote_pref: RemotePreference.nullable(),
  salary_floor: SalaryFloor.nullable(),
  work_auth: z.string().nullable(),
  start_date: z.string().nullable(),
  must_haves: z.array(z.string()).nullable(),
  dealbreakers: z.array(z.string()).nullable(),
  /**
   * Slots the user declined. Recorded so the agent stops asking — being
   * re-asked a question you already refused is the fastest way to make an
   * onboarding interview feel like a form.
   */
  declined_slots: z.array(z.enum(SLOTS.map((s) => s.id) as [string, ...string[]])).nullable(),
});

export function updateProfilePreferences(userId: string) {
  return tool({
    description:
      "Record what the user has told you about what they are looking for. " +
      "Pass only the fields they just answered; leave the rest null. Use " +
      "declined_slots for anything they explicitly do not want to answer.",
    inputSchema: Input,
    execute: async (input) => {
      const [existing] = await db
        .select()
        .from(profiles)
        .where(eq(profiles.userId, userId))
        .limit(1);

      if (!existing) {
        return {
          ok: false,
          error:
            "No profile yet — the user needs to upload a resume before preferences can be saved.",
        };
      }

      const current =
        PreferencesSchema.safeParse(existing.preferences).data ??
        EMPTY_PREFERENCES;
      const state =
        InterviewStateSchema.safeParse(existing.interviewState).data ??
        EMPTY_INTERVIEW_STATE;

      // null means "not answered in this turn", so stored answers survive a
      // turn that only fills one or two slots.
      const answered = Object.fromEntries(
        (Object.keys(EMPTY_PREFERENCES) as Array<keyof Preferences>)
          .filter((key) => input[key] !== null && input[key] !== undefined)
          .map((key) => [key, input[key]]),
      );
      const merged: Preferences = { ...current, ...answered };

      const nextState = {
        ...state,
        skipped: [
          ...new Set([...state.skipped, ...(input.declined_slots ?? [])]),
        ],
      };

      await db
        .update(profiles)
        .set({
          preferences: merged,
          interviewState: nextState,
          updatedAt: new Date(),
        })
        .where(eq(profiles.userId, userId));

      const progress = interviewProgress(merged, nextState);

      return {
        ok: true,
        saved: merged,
        answered: progress.answered,
        total: progress.total,
        stillNeeded: progress.outstanding.map((slot) => slot.label),
        blockingSearch: progress.blocking.map((slot) => slot.label),
      };
    },
  });
}
