/**
 * Event names shared by the server and the chat page. No imports, so a client
 * component can use them without pulling the database into its bundle.
 */

/**
 * The moments a run started from chat reports back in that chat: it paused
 * for a decision, it finished, or it failed.
 */
export const FOLLOW_UP_KINDS: ReadonlySet<string> = new Set([
  "approval.requested",
  "run.completed",
  "run.failed",
  "tailor.done",
  "tailor.failed",
]);

/**
 * Emitted once a follow-up has been written (or skipped), with
 * `{ conversationId, runId, messageId?, label?, skipped?, failed? }`.
 */
export const FOLLOW_UP_EVENT = "chat.followup";
