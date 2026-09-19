import { ToolLoopAgent, isStepCount, type InferAgentUIMessage } from "ai";
import { eq } from "drizzle-orm";

import { db } from "@/lib/db";
import { profiles } from "@/lib/db/schema";
import { model } from "@/lib/llm/models";
import {
  EMPTY_INTERVIEW_STATE,
  EMPTY_PREFERENCES,
  InterviewStateSchema,
  PreferencesSchema,
} from "@/lib/profile/interview";

import {
  addCompany,
  discoverCompanies,
  listRegisteredCompanies,
} from "./tools/companies";
import { getProfileSummary } from "./tools/get-profile-summary";
import { getResume } from "./tools/resume";
import {
  cancelRun,
  getJob,
  getRunStatus,
  listJobs,
  startSearchRun,
} from "./tools/search";
import { updateProfilePreferences } from "./tools/update-profile-preferences";
import { systemPrompt } from "./system-prompt";

/**
 * The chat agent, built per request around one user.
 *
 * The instructions are assembled from that user's stored state, so the agent
 * knows what it has already been told. The step cap matters: a chat turn runs
 * inside a Next request, so a loop that will not terminate has to be bounded
 * rather than merely discouraged.
 */
export async function chatAgent(userId: string) {
  const [profile] = await db
    .select()
    .from(profiles)
    .where(eq(profiles.userId, userId))
    .limit(1);

  return new ToolLoopAgent({
    model: model("chat"),
    instructions: systemPrompt({
      hasProfile: Boolean(profile),
      preferences:
        PreferencesSchema.safeParse(profile?.preferences).data ??
        EMPTY_PREFERENCES,
      interviewState:
        InterviewStateSchema.safeParse(profile?.interviewState).data ??
        EMPTY_INTERVIEW_STATE,
    }),
    tools: {
      get_profile_summary: getProfileSummary(userId),
      get_resume: getResume(userId),
      update_profile_preferences: updateProfilePreferences(userId),
      add_company: addCompany(userId),
      discover_companies: discoverCompanies(userId),
      list_companies: listRegisteredCompanies(),
      start_search_run: startSearchRun(userId),
      get_run_status: getRunStatus(userId),
      cancel_run: cancelRun(userId),
      list_jobs: listJobs(userId),
      get_job: getJob(),
    },
    stopWhen: isStepCount(8),
  });
}

/**
 * The UI message type for *this* agent, with its tool names and input/output
 * types baked in. The client infers tool parts from it, so a renamed tool is a
 * type error rather than a silently unrendered part.
 */
export type ForayUIMessage = InferAgentUIMessage<
  Awaited<ReturnType<typeof chatAgent>>
>;
