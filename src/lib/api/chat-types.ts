import type { UIMessage } from "ai";

import type { components } from "./schema";

/**
 * The chat's message type (design §6.6), written by hand from the generated schema.
 *
 * Django stores one AI SDK `UIMessage` per row and returns them as they are (§7.4), so the shapes
 * here describe both a streamed reply and `GET /api/conversations/{id}`. Types only: no runtime
 * code, so client and server components may both import this module.
 *
 * Tool **outputs** come from the schema (`backend/apps/chat/tools.py` `OUTPUT_MODELS`). Tool
 * **inputs** are written by hand below until step 8E registers the input models for OpenAPI
 * (deviations/step-8D.md §1).
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

// --- tool inputs (hand-written; replace with S["…Input"] once 8E registers them) ---------------
//
// TODO(8E): these mirror the input models in `backend/apps/chat/tools.py` (today's snake_case
// argument names; every field required, null meaning absent). Once `chat/api.py` registers
// `INPUT_MODELS` and the types are regenerated, each becomes `S["<Name>Input"]`.

type Section = "basics" | "summary" | "work" | "education" | "projects" | "skills";

/** `GetProfileSummaryInput`, `ListPendingApprovalsInput`: no arguments. */
export type NoInput = Record<string, never>;

export type GetResumeInput = { sections: Section[] | null };

export type SalaryFloorInput = { amount: number; currency: string; period: "year" | "hour" };

export type UpdateProfilePreferencesInput = {
  target_roles: string[] | null;
  seniority: S["Seniority"] | null;
  locations: string[] | null;
  remote_pref: S["RemotePreference"] | null;
  salary_floor: SalaryFloorInput | null;
  work_auth: string | null;
  start_date: string | null;
  must_haves: string[] | null;
  dealbreakers: string[] | null;
  declined_slots: S["SlotId"][] | null;
};

export type AddCompanyInput = { name_or_url: string };

export type DiscoverCompaniesInput = { description: string; count: number };

export type ListCompaniesInput = { limit: number | null };

export type StartSearchRunInput = {
  prompt: string;
  companies: string[] | null;
  roles: string[] | null;
  seniority: S["Seniority"] | null;
  locations: string[] | null;
  remote: S["RemotePreference"] | null;
  focus: string | null;
  mode: S["AutonomyMode"] | null;
};

export type GetRunStatusInput = { run_id: string | null };

export type CancelRunInput = { run_id: string };

export type ListJobsInput = {
  run_id: string | null;
  limit: number | null;
  stage: S["Stage"] | null;
  min_score: number | null;
  query: string | null;
};

/** `get_job`, `evaluate_job`, `tailor_resume`: a match id or a job id. */
export type JobIdInput = { id: string };

export type AnswerApprovalInput = {
  approval_id: string;
  approve: boolean;
  item_ids: string[] | null;
};

export type MoveJobStageInput = { id: string; stage: S["Stage"]; note: string | null };

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
  get_profile_summary: T<NoInput, S["GetProfileSummaryOutput"]>;
  get_resume: T<GetResumeInput, S["GetResumeOutput"]>;
  update_profile_preferences: T<UpdateProfilePreferencesInput, S["UpdateProfilePreferencesOutput"]>;
  add_company: T<AddCompanyInput, S["RunStartedToolOutput"]>;
  discover_companies: T<DiscoverCompaniesInput, S["DiscoverCompaniesOutput"]>;
  list_companies: T<ListCompaniesInput, S["ListCompaniesOutput"]>;
  start_search_run: T<StartSearchRunInput, S["StartSearchRunOutput"]>;
  get_run_status: T<GetRunStatusInput, S["GetRunStatusOutput"]>;
  cancel_run: T<CancelRunInput, S["CancelRunOutput"]>;
  list_jobs: T<ListJobsInput, S["ListJobsOutput"]>;
  get_job: T<JobIdInput, S["GetJobOutput"]>;
  list_pending_approvals: T<NoInput, S["ListPendingApprovalsOutput"]>;
  answer_approval: T<AnswerApprovalInput, S["AnswerApprovalOutput"]>;
  move_job_stage: T<MoveJobStageInput, S["MoveJobStageOutput"]>;
  evaluate_job: T<JobIdInput, S["MatchRunOutput"]>;
  tailor_resume: T<JobIdInput, S["MatchRunOutput"]>;
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
