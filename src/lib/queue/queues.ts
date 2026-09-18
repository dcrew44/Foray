/**
 * Every queue name in one place, with the shape of what each carries.
 *
 * Queue names are strings on the wire, so a typo would be a job that is
 * accepted and never worked. Both sides import these constants.
 */

export const QUEUES = {
  /** Liveness check: proves the app can enqueue and the worker can emit. */
  ping: "ping",
} as const;

export type QueueName = (typeof QUEUES)[keyof typeof QUEUES];

export type PingPayload = {
  userId: string;
  note?: string;
};

export type QueuePayloads = {
  [QUEUES.ping]: PingPayload;
};
