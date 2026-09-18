import {
  index,
  integer,
  jsonb,
  numeric,
  pgTable,
  text,
  timestamp,
  uuid,
} from "drizzle-orm/pg-core";

import { llmPurpose } from "./enums";
import { user } from "./auth";
import { runs } from "./runs";

export const conversations = pgTable(
  "conversations",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: text("user_id")
      .notNull()
      .references(() => user.id, { onDelete: "cascade" }),
    title: text("title"),
    createdAt: timestamp("created_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [index("conversations_user_updated_at_idx").on(t.userId, t.updatedAt.desc())],
);

/**
 * One AI SDK `UIMessage` per row, keyed by the SDK's own message id so a
 * resumed stream updates the row it already wrote.
 */
export const messages = pgTable(
  "messages",
  {
    /** The AI SDK message id, not ours. */
    id: text("id").primaryKey(),
    conversationId: uuid("conversation_id")
      .notNull()
      .references(() => conversations.id, { onDelete: "cascade" }),
    userId: text("user_id")
      .notNull()
      .references(() => user.id, { onDelete: "cascade" }),

    role: text("role").notNull(),
    /** `UIMessage.parts` verbatim: text, tool calls, reasoning, files. */
    parts: jsonb("parts").notNull(),
    metadata: jsonb("metadata"),

    createdAt: timestamp("created_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [
    index("messages_conversation_created_at_idx").on(
      t.conversationId,
      t.createdAt,
    ),
  ],
);

/**
 * Every LLM call, with what it cost.
 *
 * This is both the answer to "which model should we use for this purpose" and
 * the seed of SaaS billing, so nothing may call a provider without writing a
 * row here — see DESIGN.md invariant 10.
 */
export const llmCalls = pgTable(
  "llm_calls",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: text("user_id").references(() => user.id, { onDelete: "set null" }),
    runId: uuid("run_id").references(() => runs.id, { onDelete: "set null" }),

    purpose: llmPurpose("purpose").notNull(),
    provider: text("provider").notNull(),
    model: text("model").notNull(),

    inputTokens: integer("input_tokens").notNull().default(0),
    outputTokens: integer("output_tokens").notNull().default(0),
    /** Charged at a lower rate (often near zero) where the vendor supports it. */
    cachedInputTokens: integer("cached_input_tokens").notNull().default(0),
    reasoningTokens: integer("reasoning_tokens").notNull().default(0),

    /** Computed from pricing.ts at call time — rates change, rows should not. */
    costUsd: numeric("cost_usd", { precision: 12, scale: 6 })
      .notNull()
      .default("0"),
    latencyMs: integer("latency_ms"),
    /** Set when the call failed, so failures are costed and counted too. */
    error: text("error"),

    createdAt: timestamp("created_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [
    index("llm_calls_user_created_at_idx").on(t.userId, t.createdAt.desc()),
    index("llm_calls_run_id_idx").on(t.runId),
    index("llm_calls_purpose_model_idx").on(t.purpose, t.model),
  ],
);

export type Conversation = typeof conversations.$inferSelect;
export type NewConversation = typeof conversations.$inferInsert;
export type Message = typeof messages.$inferSelect;
export type NewMessage = typeof messages.$inferInsert;
export type LlmCall = typeof llmCalls.$inferSelect;
export type NewLlmCall = typeof llmCalls.$inferInsert;
