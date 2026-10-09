import type { UIMessage } from "ai";

import type { components } from "./schema";

/**
 * The chat's message type (design §6.6), written by hand from the generated schema.
 *
 * Django stores one AI SDK `UIMessage` per row and returns them as they are (§7.4), so the shapes
 * here describe both a streamed reply and `GET /api/conversations/{id}`. Types only: no runtime
 * code, so client and server components may both import this module.
 *
 * Tool inputs and outputs both come from the schema (`backend/apps/chat/tools.py` `INPUT_MODELS`
 * and `OUTPUT_MODELS`, registered by `chat/api.py`). Inputs keep today's snake_case argument
 * names, every field required and null meaning absent (§7.2).
 */

type S = components["schemas"];

// --- pages and conversations ---------------------------------------------------------------------

/** `GET /api/pages/chat`: the starting points a new chat offers. */
export type ChatPage = S["ChatPage"];
/** A starting point. `prefill` ones put their text in the composer for the user to finish. */
export type ChatSuggestion = S["ChatSuggestion"];
/** `GET /api/conversations/{id}`: `{conversation: {id, title}, messages}`. */
export type ConversationOut = S["ConversationOut"];

// --- message metadata ----------------------------------------------------------------------------

/**
 * Set only on the two messages a follow-up writes (§7.7): the notice (role `user`, shown as a
 * divider with `label`) and the agent's reply to it. Every other message has none.
 */
export type ForayMessageMetadata =
  | { kind: "run_update"; runId: string; eventId: number; label: string }
  | { kind: "follow_up"; runId: string; eventId: number };

// --- the tools -----------------------------------------------------------------------------------

/**
 * What any tool returns when it refuses or fails (§7.2's one error wrapper): a throttle, a
 * refused start, the budget, or "That step failed on our side.". It can replace the output of
 * every tool, including those whose own output has no `ok`.
 */
export type ToolFailure = { ok: false; message: string };

type T<I, O> = { input: I; output: O | ToolFailure };

/** The 16 chat tools (§7.2), by name. */
export type ForayTools = {
  get_profile_summary: T<S["GetProfileSummaryInput"], S["GetProfileSummaryOutput"]>;
  get_resume: T<S["GetResumeInput"], S["GetResumeOutput"]>;
  update_profile_preferences: T<S["UpdateProfilePreferencesInput"], S["UpdateProfilePreferencesOutput"]>;
  add_company: T<S["AddCompanyInput"], S["RunStartedToolOutput"]>;
  discover_companies: T<S["DiscoverCompaniesInput"], S["DiscoverCompaniesOutput"]>;
  list_companies: T<S["ListCompaniesInput"], S["ListCompaniesOutput"]>;
  start_search_run: T<S["StartSearchRunInput"], S["StartSearchRunOutput"]>;
  get_run_status: T<S["GetRunStatusInput"], S["GetRunStatusOutput"]>;
  cancel_run: T<S["CancelRunInput"], S["CancelRunOutput"]>;
  list_jobs: T<S["ListJobsInput"], S["ListJobsOutput"]>;
  get_job: T<S["GetJobInput"], S["GetJobOutput"]>;
  list_pending_approvals: T<S["ListPendingApprovalsInput"], S["ListPendingApprovalsOutput"]>;
  answer_approval: T<S["AnswerApprovalInput"], S["AnswerApprovalOutput"]>;
  move_job_stage: T<S["MoveJobStageInput"], S["MoveJobStageOutput"]>;
  evaluate_job: T<S["EvaluateJobInput"], S["MatchRunOutput"]>;
  tailor_resume: T<S["TailorResumeInput"], S["MatchRunOutput"]>;
};

export type ForayToolName = keyof ForayTools;

/** One `list_jobs` result line. */
export type JobCard = S["JobCard"];

/**
 * A message in a Foray chat. Foray streams no data parts. Parts are what Django stored, so a
 * conversation imported from the TypeScript app may hold tool outputs in that app's shapes
 * (`tool-part.tsx` reads every output defensively), and a tool name the model invented arrives
 * as a static `tool-<name>` part with `output-error` (deviations/step-6.md §2).
 */
export type ForayUIMessage = UIMessage<ForayMessageMetadata | undefined, never, ForayTools>;
