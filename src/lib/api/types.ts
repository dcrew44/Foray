import type { components } from "./schema";

/**
 * The friendly names the frontend uses for Django's response shapes (design §6.7).
 *
 * Every one is generated from Django's OpenAPI schema (`pnpm api:types`), so a backend change
 * that alters a shape breaks `tsc` at each component that reads it. Types only: this module has
 * no runtime code, and client and server components may both import it (unlike
 * `lib/api/server`).
 */

type S = components["schemas"];

// The live channel: `GET /api/live?since=` and `GET /api/runs/{id}`.
export type LiveSnapshot = S["LiveSnapshot"];
export type LiveRun = S["LiveRun"];
export type LiveEvent = S["LiveEvent"];
export type LiveApproval = S["LiveApproval"];
export type WorkerStatus = S["WorkerStatus"];

// Pages and their rows.
export type MatchRow = S["MatchRow"];
export type CompanyRow = S["CompanyRow"];
export type RunSummary = S["RunSummary"];
export type TailoredView = S["TailoredView"];

// The profile: resume, preferences, the interview's slots, and the user's settings.
export type Resume = S["Resume"];
export type SkillItem = S["SkillItem"];
export type Preferences = S["Preferences"];
export type Slot = S["Slot"];
export type Settings = S["Settings"];

// Run outputs and event data. `LiveRun.output` and `LiveEvent.data` are open objects; these are
// the shapes the components that started a run read from its ending event.
/** A `profile_import` run's output: the dropzone reads `usedVision`. */
export type ImportOutput = S["ImportOutput"];
/** A `company_discovery` run's output for one query: `registered`, the row, and the trail. */
export type CompanyAddOutput = S["CompanyAddOutput"];
/** One thing company discovery tried, in order: `{step: "domain", detail: "linear.app"}`. */
export type DiscoveryStep = S["DiscoveryStep"];
/** `worker.ping`'s data: the job that answered Settings' Test, and the worker's pid. */
export type WorkerPingData = S["WorkerPingData"];
