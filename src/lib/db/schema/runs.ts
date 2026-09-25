import {
  bigserial,
  index,
  jsonb,
  pgTable,
  text,
  timestamp,
  uuid,
} from "drizzle-orm/pg-core";

import {
  approvalKind,
  approvalStatus,
  decidedVia,
  eventLevel,
  runKind,
  runMode,
  runStatus,
} from "./enums";
import { user } from "./auth";

/**
 * Background work, its progress, and the decisions it pauses for.
 *
 * One `runs` table covers every run kind: later phases add a member to the
 * `run_kind` enum rather than another table, so the activity feed, the run
 * card and cancellation keep working unchanged.
 */

export const runs = pgTable(
  "runs",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: text("user_id")
      .notNull()
      .references(() => user.id, { onDelete: "cascade" }),

    kind: runKind("kind").notNull(),
    /** Set when a run was spawned by another, e.g. rank_deep under a search. */
    parentRunId: uuid("parent_run_id"),

    mode: runMode("mode").notNull().default("approve_each"),
    status: runStatus("status").notNull().default("queued"),

    /** The run's parameters, including derived criteria worth not recomputing. */
    input: jsonb("input").notNull().default({}),
    /** Live counters the run card reads while the run is in flight. */
    stats: jsonb("stats").notNull().default({}),
    /** The result, once there is one. */
    output: jsonb("output"),
    error: text("error"),

    /** The chat conversation that started this, when one did. */
    conversationId: uuid("conversation_id"),
    pgbossJobId: text("pgboss_job_id"),

    startedAt: timestamp("started_at", { withTimezone: true }),
    finishedAt: timestamp("finished_at", { withTimezone: true }),
    createdAt: timestamp("created_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [
    index("runs_user_created_at_idx").on(t.userId, t.createdAt.desc()),
    index("runs_user_status_idx").on(t.userId, t.status),
    index("runs_parent_run_id_idx").on(t.parentRunId),
  ],
);

/**
 * Append-only progress. `/api/live?since=<id>` polls this while a run is
 * active, which is why the id is a monotonic bigserial rather than a uuid.
 */
export const agentEvents = pgTable(
  "agent_events",
  {
    id: bigserial("id", { mode: "number" }).primaryKey(),
    userId: text("user_id")
      .notNull()
      .references(() => user.id, { onDelete: "cascade" }),
    runId: uuid("run_id").references(() => runs.id, { onDelete: "cascade" }),

    level: eventLevel("level").notNull().default("info"),
    /** Dotted, e.g. `run.started`, `source.fetch.done`, `scoring.progress`. */
    kind: text("kind").notNull(),
    message: text("message").notNull(),
    data: jsonb("data"),

    /** Set on `approval.requested`, so the feed can render the card inline. */
    approvalId: uuid("approval_id"),

    createdAt: timestamp("created_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [
    index("agent_events_user_id_idx").on(t.userId, t.id),
    index("agent_events_run_id_idx").on(t.runId, t.id),
  ],
);

/**
 * A gate a run stopped at.
 *
 * The handler that reaches a gate writes the row — including the
 * `continuation` needed to resume — and returns; the process does not stay
 * alive waiting. Deciding enqueues that continuation. In `autopilot` the same
 * path runs with `decided_via: 'autopilot'`, so there is one code path, not two.
 */
export const approvals = pgTable(
  "approvals",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: text("user_id")
      .notNull()
      .references(() => user.id, { onDelete: "cascade" }),
    runId: uuid("run_id")
      .notNull()
      .references(() => runs.id, { onDelete: "cascade" }),

    kind: approvalKind("kind").notNull(),
    title: text("title").notNull(),
    /** What is being asked: the items, and the cost estimate from pricing.ts. */
    payload: jsonb("payload").notNull().default({}),

    status: approvalStatus("status").notNull().default("pending"),
    /** The answer, including which subset of items was approved. */
    decision: jsonb("decision"),
    decidedVia: decidedVia("decided_via"),
    decidedAt: timestamp("decided_at", { withTimezone: true }),

    /** `{ queue, payload }` — enqueued verbatim when this is decided. */
    continuation: jsonb("continuation").notNull(),
    expiresAt: timestamp("expires_at", { withTimezone: true }),

    createdAt: timestamp("created_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [
    index("approvals_user_status_idx").on(t.userId, t.status),
    index("approvals_run_id_idx").on(t.runId),
  ],
);

export type Run = typeof runs.$inferSelect;
export type NewRun = typeof runs.$inferInsert;
export type AgentEvent = typeof agentEvents.$inferSelect;
export type NewAgentEvent = typeof agentEvents.$inferInsert;
export type Approval = typeof approvals.$inferSelect;
export type NewApproval = typeof approvals.$inferInsert;
