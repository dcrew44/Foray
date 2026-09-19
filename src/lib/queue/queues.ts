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
  /** Find and record matching roles for one user. */
  search: "search",
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

export type QueuePayloads = {
  [QUEUES.ping]: PingPayload;
  [QUEUES.search]: SearchPayload;
  [QUEUES.companyDiscover]: CompanyDiscoverPayload;
  [QUEUES.companyRefresh]: CompanyRefreshPayload;
};
