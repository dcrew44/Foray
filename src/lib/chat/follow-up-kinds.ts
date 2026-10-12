/**
 * Event names shared by the server and the chat page. No imports, so a client
 * component can use them without pulling the database into its bundle.
 */

/**
 * The moments a run started from chat reports back in that chat: it paused
 * for a decision, it finished, or it failed. Every run kind ends with
 * `run.completed`, `run.failed` or `run.cancelled`, so these three cover all
 * of them. The same set as `FOLLOW_UP_KINDS` in
 * `backend/apps/runs/catalogue.py` (checked by
 * `tests/chat/test_follow_up_db.py::test_follow_up_kinds_match_frontend`).
 */
export const FOLLOW_UP_KINDS: ReadonlySet<string> = new Set([
  "approval.requested",
  "run.completed",
  "run.failed",
]);

/**
 * Emitted once a follow-up has been written (or skipped), with
 * `{ conversationId, runId, messageId?, label?, skipped?, reason?, failed? }`.
 */
export const FOLLOW_UP_EVENT = "chat.followup";
