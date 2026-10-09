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

/**
 * One thing company discovery tried, in order: `{step: "domain", detail: "linear.app"}`. It
 * lives in a `company_discovery` run's `output.trail`, and run output is an open object in the
 * schema (`LiveRun.output`), so this one is written by hand from `DiscoveryStep` in
 * `backend/apps/runs/catalogue.py`.
 */
export type DiscoveryStep = { step: string; detail: string };
