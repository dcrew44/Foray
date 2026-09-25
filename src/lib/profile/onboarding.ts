import { getProfile } from "@/lib/db/queries/profile";
import { ResumeSchema, type Resume } from "@/lib/resume/schema";

import {
  EMPTY_INTERVIEW_STATE,
  EMPTY_PREFERENCES,
  InterviewStateSchema,
  interviewProgress,
  PreferencesSchema,
  type InterviewProgress,
  type Preferences,
} from "./interview";
import { parseSettings, type Settings } from "./settings";

/**
 * Where one user stands between signing up and a first search. One answer,
 * read the same way by the home page, the profile page and the agent.
 */

export type OnboardingState = {
  hasProfile: boolean;
  hasResume: boolean;
  resume: Resume | null;
  preferences: Preferences;
  progress: InterviewProgress;
  settings: Settings;
  /** A search can start: resume in, and every required preference known. */
  ready: boolean;
};

export async function getOnboardingState(userId: string): Promise<OnboardingState> {
  const profile = await getProfile(userId);

  const resume = profile ? (ResumeSchema.safeParse(profile.data).data ?? null) : null;
  const hasResume = Boolean(
    resume && resume.work.length + resume.education.length + resume.skills.length > 0,
  );

  const preferences =
    PreferencesSchema.safeParse(profile?.preferences).data ?? EMPTY_PREFERENCES;
  const state =
    InterviewStateSchema.safeParse(profile?.interviewState).data ?? EMPTY_INTERVIEW_STATE;
  const progress = interviewProgress(preferences, state);

  return {
    hasProfile: Boolean(profile),
    hasResume,
    resume,
    preferences,
    progress,
    settings: parseSettings(profile?.settings),
    ready: hasResume && progress.blocking.length === 0,
  };
}
