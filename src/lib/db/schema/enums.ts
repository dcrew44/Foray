import { pgEnum } from "drizzle-orm/pg-core";

/**
 * Enums shared across tables. Each is a real Postgres enum so a typo is a
 * write error rather than a row nobody notices.
 */

/** Where a posting came from. `web` is the LLM careers-page extractor. */
export const jobSource = pgEnum("job_source", [
  "greenhouse",
  "lever",
  "ashby",
  "jsearch",
  "web",
]);

/** How we fetch a company's postings. `none` = registered but unfetchable. */
export const companySourceType = pgEnum("company_source_type", [
  "greenhouse",
  "lever",
  "ashby",
  "careers_page",
  "none",
]);

export const companyStatus = pgEnum("company_status", [
  "active",
  "invalid",
  "paused",
]);

export const discoveredVia = pgEnum("discovered_via", [
  "seed",
  "agent",
  "jsearch",
  "user",
]);

export const remoteKind = pgEnum("remote_kind", [
  "remote",
  "hybrid",
  "onsite",
  "unknown",
]);

/** The pipeline board's columns. */
export const matchStage = pgEnum("match_stage", [
  "found",
  "shortlisted",
  "applied",
  "interviewing",
  "offer",
  "rejected",
  "archived",
]);

/**
 * Every kind of background run lives in one `runs` table. Later phases add
 * members here rather than new tables: `apply`, `send_email`, `browser_connect`.
 */
export const runKind = pgEnum("run_kind", [
  "search",
  "rank_deep",
  "tailor",
  "resume_render",
  "company_discovery",
  "company_refresh",
  "profile_import",
]);

/** Per-run autonomy. `approve_each` is the default; `autopilot` auto-approves. */
export const runMode = pgEnum("run_mode", ["approve_each", "autopilot"]);

export const runStatus = pgEnum("run_status", [
  "queued",
  "running",
  "awaiting_approval",
  "completed",
  "failed",
  "cancelled",
]);

export const eventLevel = pgEnum("event_level", [
  "debug",
  "info",
  "warn",
  "error",
]);

export const approvalKind = pgEnum("approval_kind", [
  "deep_eval_batch",
  "shortlist_proposal",
  "tailor",
  "company_add",
]);

export const approvalStatus = pgEnum("approval_status", [
  "pending",
  "approved",
  "rejected",
  "expired",
  "auto_approved",
]);

export const decidedVia = pgEnum("decided_via", ["ui", "chat", "autopilot"]);

/** What a stored file is. Uploads and renders share one table. */
export const documentKind = pgEnum("document_kind", [
  "resume_upload",
  "linkedin_pdf",
  "resume_pdf",
]);

export const resumeKind = pgEnum("resume_kind", ["base", "tailored"]);

/** Which renderer produced a PDF, so HTML and LaTeX output stay comparable. */
export const renderEngine = pgEnum("render_engine", ["html", "latex"]);

/**
 * What an LLM call was for. Call sites ask for a purpose and `models.ts` maps
 * it to a provider and model from env — no model names in application code.
 */
export const llmPurpose = pgEnum("llm_purpose", [
  "chat",
  "extract",
  "score",
  "evaluate",
  "tailor",
  "verify",
  "merge",
]);
