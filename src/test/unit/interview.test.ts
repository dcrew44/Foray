import { describe, expect, it } from "vitest";

import {
  EMPTY_INTERVIEW_STATE,
  EMPTY_PREFERENCES,
  InterviewStateSchema,
  PreferencesSchema,
  SLOTS,
  interviewProgress,
} from "@/lib/profile/interview";

describe("PreferencesSchema", () => {
  it("fills in keys a stored row does not have", () => {
    // What an older version of this schema would have written.
    const stored = { target_roles: ["Backend Engineer"], seniority: "senior" };

    const parsed = PreferencesSchema.safeParse(stored);
    expect(parsed.success).toBe(true);
    expect(parsed.data?.target_roles).toEqual(["Backend Engineer"]);
    // Missing keys must not fail the parse, because the caller's fallback on
    // failure is empty preferences — which would discard the answers above.
    expect(parsed.data?.work_auth).toBeNull();
    expect(parsed.data?.salary_floor).toBeNull();
  });

  it("parses a completely empty row", () => {
    expect(PreferencesSchema.safeParse({}).data).toEqual(EMPTY_PREFERENCES);
  });

  it("still rejects a value of the wrong type", () => {
    // Tolerance is about absent keys, not about accepting nonsense.
    expect(PreferencesSchema.safeParse({ seniority: "emperor" }).success).toBe(
      false,
    );
    expect(PreferencesSchema.safeParse({ target_roles: "engineer" }).success).toBe(
      false,
    );
  });
});

describe("interviewProgress", () => {
  it("counts nothing as answered for a fresh user", () => {
    const progress = interviewProgress(
      EMPTY_PREFERENCES,
      EMPTY_INTERVIEW_STATE,
    );
    expect(progress.answered).toBe(0);
    expect(progress.total).toBe(SLOTS.length);
    expect(progress.complete).toBe(false);
    expect(progress.blocking.length).toBeGreaterThan(0);
  });

  it("treats a declined slot as answered, so the checklist can finish", () => {
    const progress = interviewProgress(EMPTY_PREFERENCES, {
      version: 1,
      skipped: ["salary_floor"],
    });
    expect(progress.answered).toBe(1);
    expect(progress.outstanding.map((s) => s.id)).not.toContain("salary_floor");
  });

  it("does not let a declined slot unblock a required one", () => {
    // Declining is allowed; declining your way into a search is not.
    const progress = interviewProgress(EMPTY_PREFERENCES, {
      version: 1,
      skipped: ["work_auth"],
    });
    expect(progress.outstanding.map((s) => s.id)).not.toContain("work_auth");
    expect(progress.blocking.map((s) => s.id)).toContain("work_auth");
  });

  it("treats an empty array as unanswered", () => {
    const progress = interviewProgress(
      { ...EMPTY_PREFERENCES, target_roles: [] },
      EMPTY_INTERVIEW_STATE,
    );
    expect(progress.blocking.map((s) => s.id)).toContain("target_roles");
  });

  it("counts the constraints slot when either field is given", () => {
    const progress = interviewProgress(
      { ...EMPTY_PREFERENCES, dealbreakers: ["no on-call"] },
      EMPTY_INTERVIEW_STATE,
    );
    expect(progress.filled.map((s) => s.id)).toContain("constraints");
  });

  it("is complete once every slot is answered or declined", () => {
    const answered = {
      ...EMPTY_PREFERENCES,
      target_roles: ["Backend Engineer"],
      seniority: "senior" as const,
      locations: ["Bay Area"],
      remote_pref: "any" as const,
      work_auth: "US citizen",
      start_date: "six weeks",
      must_haves: ["ownership"],
    };
    const progress = interviewProgress(answered, {
      version: 1,
      skipped: ["salary_floor"],
    });
    expect(progress.complete).toBe(true);
    expect(progress.blocking).toHaveLength(0);
  });
});

describe("InterviewStateSchema", () => {
  it("defaults an empty stored state to the current version", () => {
    expect(InterviewStateSchema.parse({})).toEqual(EMPTY_INTERVIEW_STATE);
  });
});
