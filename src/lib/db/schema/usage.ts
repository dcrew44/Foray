import { index, integer, pgTable, text, timestamp } from "drizzle-orm/pg-core";

/**
 * Third-party API request counters, by calendar month.
 *
 * JSearch's free tier is 200 requests a month and each result page bills one,
 * so a search run that pages freely can exhaust a month in an afternoon. This
 * is the number the run budget checks before spending another request, and the
 * one the settings page reports. LLM spend has its own ledger (`llm_calls`)
 * because it is priced per token rather than per call.
 */
export const apiUsage = pgTable(
  "api_usage",
  {
    id: text("id").primaryKey(),
    /** e.g. `jsearch`. */
    provider: text("provider").notNull(),
    /** `YYYY-MM`, in UTC — the window the provider's quota resets on. */
    period: text("period").notNull(),
    requests: integer("requests").notNull().default(0),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [index("api_usage_provider_period_idx").on(t.provider, t.period)],
);

export type ApiUsage = typeof apiUsage.$inferSelect;
