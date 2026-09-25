/**
 * Every queue name in one place, with the shape of what each carries.
 *
 * Queue names are strings on the wire, so a typo would be a job that is
 * accepted and never worked. Both sides import these constants.
 */

export const QUEUES = {
  /** Liveness check: proves the app can enqueue and the worker can emit. */
  ping: "ping",
  /** Resolve a company name or URL to a verified board and register it. */
  companyDiscover: "company.discover",
  /** Re-fetch registered boards into the shared job mirror. */
  companyRefresh: "company.refresh",
  /** Find, record and fast-score matching roles for one user. */
  search: "search",
  /**
   * Continuation of a search run after its deep-evaluation gate: evaluate the
   * approved matches with the strong model, then propose a shortlist.
   */
  searchEvaluate: "search.evaluate",
  /** Continuation after the shortlist gate: move the approved matches. */
  searchShortlist: "search.shortlist",
  /** Deep-evaluate specific matches on demand (chat or the job page). */
  matchEvaluate: "match.evaluate",
  /** Tailor the base resume to one match, verify it, and render a PDF. */
  resumeTailor: "resume.tailor",
  /** Render a stored resume (base or tailored) to PDF. */
  resumeRender: "resume.render",
  /**
   * Have the chat agent report back in the conversation that started a run,
   * when the run pauses for a decision, finishes or fails.
   */
  chatFollowUp: "chat.follow_up",
} as const;

export type QueueName = (typeof QUEUES)[keyof typeof QUEUES];

export type PingPayload = {
  userId: string;
  note?: string;
};

export type CompanyDiscoverPayload = {
  userId: string;
  /** Company names or URLs. Each is resolved independently. */
  names: string[];
  runId?: string | null;
};

export type CompanyRefreshPayload = {
  /** Set when a person asked, so the activity feed can report back. */
  userId?: string | null;
  runId?: string | null;
  /** Refresh every active board rather than only the stale ones. */
  all?: boolean;
};

export type SearchPayload = {
  userId: string;
  runId: string;
};

/** Enqueued verbatim as an approval's continuation — see lib/runs/approvals. */
export type SearchContinuationPayload = {
  userId: string;
  runId: string;
  approvalId: string;
};

export type MatchEvaluatePayload = {
  userId: string;
  /** A `rank_deep` run created by the caller, so progress has somewhere to go. */
  runId: string;
  matchIds: string[];
};

export type ResumeTailorPayload = {
  userId: string;
  /** A `tailor` run created by the caller. */
  runId: string;
  matchId: string;
};

export type ResumeRenderPayload = {
  userId: string;
  /** A `resume_render` run created by the caller. */
  runId: string;
  resumeId: string;
};

export type ChatFollowUpPayload = {
  userId: string;
  runId: string;
  /** The `agent_events` row that marked the moment: the run paused, finished or failed. */
  eventId: number;
};

export type QueuePayloads = {
  [QUEUES.ping]: PingPayload;
  [QUEUES.search]: SearchPayload;
  [QUEUES.searchEvaluate]: SearchContinuationPayload;
  [QUEUES.searchShortlist]: SearchContinuationPayload;
  [QUEUES.matchEvaluate]: MatchEvaluatePayload;
  [QUEUES.resumeTailor]: ResumeTailorPayload;
  [QUEUES.resumeRender]: ResumeRenderPayload;
  [QUEUES.companyDiscover]: CompanyDiscoverPayload;
  [QUEUES.companyRefresh]: CompanyRefreshPayload;
  [QUEUES.chatFollowUp]: ChatFollowUpPayload;
};
