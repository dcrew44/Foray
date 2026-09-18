import { z } from "zod";

/**
 * What Foray needs to know before it can search on someone's behalf.
 *
 * This is one versioned config rather than questions scattered through a
 * prompt, because the interview is going to grow: the career-counselor mode on
 * the roadmap extends this list rather than replacing it, and a stored
 * `interview_state` written against version 1 has to stay readable when it
 * does. Adding a slot is an edit here plus a version bump, and nothing else.
 */

export const INTERVIEW_VERSION = 1;

export const Seniority = z.enum([
  "intern",
  "new_grad",
  "entry",
  "mid",
  "senior",
  "staff_plus",
]);

export const RemotePreference = z.enum(["remote", "hybrid", "onsite", "any"]);

export const SalaryFloor = z.object({
  amount: z.number(),
  currency: z.string(),
  period: z.enum(["year", "hour"]),
});

/**
 * Stored on `profiles.preferences`.
 *
 * Each field is nullable *and* defaulted. Nullable because "not asked yet" and
 * "asked, no answer" are different states the search run must tell apart.
 * Defaulted because this schema reads rows that were written by earlier
 * versions of itself: without a default, a stored object missing one key fails
 * validation outright and the caller falls back to empty preferences — which
 * silently discards every answer the user gave. Adding a slot in a later
 * version must not erase the answers to the existing ones.
 */
const stored = <T extends z.ZodTypeAny>(schema: T) =>
  schema.nullable().default(null);

export const PreferencesSchema = z.object({
  target_roles: stored(z.array(z.string())),
  seniority: stored(Seniority),
  locations: stored(z.array(z.string())),
  remote_pref: stored(RemotePreference),
  salary_floor: stored(SalaryFloor),
  work_auth: stored(z.string()),
  start_date: stored(z.string()),
  must_haves: stored(z.array(z.string())),
  dealbreakers: stored(z.array(z.string())),
});

export type Preferences = z.infer<typeof PreferencesSchema>;

export const EMPTY_PREFERENCES: Preferences = {
  target_roles: null,
  seniority: null,
  locations: null,
  remote_pref: null,
  salary_floor: null,
  work_auth: null,
  start_date: null,
  must_haves: null,
  dealbreakers: null,
};

export type SlotId =
  | "target_roles"
  | "seniority"
  | "locations"
  | "remote_pref"
  | "salary_floor"
  | "work_auth"
  | "start_date"
  | "constraints";

export type Slot = {
  id: SlotId;
  label: string;
  /** What the agent is trying to find out — not a script to read verbatim. */
  intent: string;
  /**
   * Which preference fields this slot fills. `constraints` covers two, because
   * must-haves and dealbreakers are one conversational question.
   */
  fields: Array<keyof Preferences>;
  /**
   * A search run refuses to start without these (Step 15). The rest sharpen
   * ranking but should never block someone who would rather just look.
   */
  required: boolean;
};

export const SLOTS: Slot[] = [
  {
    id: "target_roles",
    label: "Target roles",
    intent:
      "What kind of role they want, in their own words — job titles or a description of the work.",
    fields: ["target_roles"],
    required: true,
  },
  {
    id: "seniority",
    label: "Seniority",
    intent:
      "What level they are aiming at: internship, new grad, entry, mid, senior, or staff and above.",
    fields: ["seniority"],
    required: true,
  },
  {
    id: "locations",
    label: "Locations",
    intent:
      "Where they want to work — metros, cities or regions. 'Anywhere in the US' is a valid answer.",
    fields: ["locations"],
    required: true,
  },
  {
    id: "remote_pref",
    label: "Remote preference",
    intent: "Remote, hybrid, onsite, or no strong preference.",
    fields: ["remote_pref"],
    required: true,
  },
  {
    id: "work_auth",
    label: "Work authorisation",
    intent:
      "Whether they can work in their target countries without sponsorship, or need it. This changes which postings are worth showing at all.",
    fields: ["work_auth"],
    required: true,
  },
  {
    id: "salary_floor",
    label: "Salary floor",
    intent:
      "The lowest compensation worth their time. They may decline to say, which is fine.",
    fields: ["salary_floor"],
    required: false,
  },
  {
    id: "start_date",
    label: "Start date",
    intent: "When they could start — a date, or 'immediately'.",
    fields: ["start_date"],
    required: false,
  },
  {
    id: "constraints",
    label: "Must-haves and dealbreakers",
    intent:
      "Anything a role must have, and anything that rules one out — company size, industry, on-call, travel, return-to-office.",
    fields: ["must_haves", "dealbreakers"],
    required: false,
  },
];

/** Stored on `profiles.interview_state`. */
export const InterviewStateSchema = z.object({
  version: z.number().default(INTERVIEW_VERSION),
  /** Slots the user explicitly declined to answer, so we stop asking. */
  skipped: z.array(z.string()).default([]),
});

export type InterviewState = z.infer<typeof InterviewStateSchema>;

export const EMPTY_INTERVIEW_STATE: InterviewState = {
  version: INTERVIEW_VERSION,
  skipped: [],
};

/** A slot counts as filled when every field it owns has a value. */
export function isFilled(slot: Slot, preferences: Preferences): boolean {
  return slot.fields.some((field) => {
    const value = preferences[field];
    if (value === null || value === undefined) return false;
    if (Array.isArray(value)) return value.length > 0;
    return true;
  });
}

export type InterviewProgress = {
  filled: Slot[];
  /** Still worth asking about: neither answered nor explicitly declined. */
  outstanding: Slot[];
  /** Required slots still missing — what blocks a search run. */
  blocking: Slot[];
  answered: number;
  total: number;
  complete: boolean;
};

export function interviewProgress(
  preferences: Preferences,
  state: InterviewState,
): InterviewProgress {
  const skipped = new Set(state.skipped);

  const filled = SLOTS.filter((slot) => isFilled(slot, preferences));
  const outstanding = SLOTS.filter(
    (slot) => !isFilled(slot, preferences) && !skipped.has(slot.id),
  );
  const blocking = SLOTS.filter(
    (slot) => slot.required && !isFilled(slot, preferences),
  );

  return {
    filled,
    outstanding,
    blocking,
    // Declining to answer is progress: the checklist should reach the end for
    // someone who does not want to state a salary floor.
    answered: SLOTS.length - outstanding.length,
    total: SLOTS.length,
    complete: outstanding.length === 0,
  };
}

/** Human-readable current answers, for the agent's instructions. */
export function describePreferences(preferences: Preferences): string {
  const lines = SLOTS.flatMap((slot) =>
    slot.fields
      .filter((field) => {
        const value = preferences[field];
        return Array.isArray(value) ? value.length > 0 : value != null;
      })
      .map((field) => `- ${field}: ${JSON.stringify(preferences[field])}`),
  );

  return lines.length ? lines.join("\n") : "- nothing recorded yet";
}
