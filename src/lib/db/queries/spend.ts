import { and, desc, eq, gte, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { llmCalls } from "@/lib/db/schema";

/**
 * What the agents have cost this user, read from the ledger. `userId` first.
 */

export function monthStart(at: Date = new Date()): Date {
  return new Date(Date.UTC(at.getUTCFullYear(), at.getUTCMonth(), 1));
}

export async function spendSince(userId: string, since: Date) {
  const [row] = await db
    .select({
      usd: sql<number>`coalesce(sum(${llmCalls.costUsd}), 0)::float8`,
      calls: sql<number>`count(*)::int`,
      inputTokens: sql<number>`coalesce(sum(${llmCalls.inputTokens} + ${llmCalls.cachedInputTokens}), 0)::float8`,
      outputTokens: sql<number>`coalesce(sum(${llmCalls.outputTokens}), 0)::float8`,
    })
    .from(llmCalls)
    .where(and(eq(llmCalls.userId, userId), gte(llmCalls.createdAt, since)));
  return row ?? { usd: 0, calls: 0, inputTokens: 0, outputTokens: 0 };
}

export async function spendByPurpose(userId: string, since: Date) {
  return db
    .select({
      purpose: llmCalls.purpose,
      usd: sql<number>`coalesce(sum(${llmCalls.costUsd}), 0)::float8`,
      calls: sql<number>`count(*)::int`,
    })
    .from(llmCalls)
    .where(and(eq(llmCalls.userId, userId), gte(llmCalls.createdAt, since)))
    .groupBy(llmCalls.purpose)
    .orderBy(desc(sql`sum(${llmCalls.costUsd})`));
}

export async function spendByModel(userId: string, since: Date) {
  return db
    .select({
      provider: llmCalls.provider,
      model: llmCalls.model,
      usd: sql<number>`coalesce(sum(${llmCalls.costUsd}), 0)::float8`,
      calls: sql<number>`count(*)::int`,
      failures: sql<number>`count(*) filter (where ${llmCalls.error} is not null)::int`,
      avgLatencyMs: sql<number>`coalesce(avg(${llmCalls.latencyMs}), 0)::float8`,
    })
    .from(llmCalls)
    .where(and(eq(llmCalls.userId, userId), gte(llmCalls.createdAt, since)))
    .groupBy(llmCalls.provider, llmCalls.model)
    .orderBy(desc(sql`sum(${llmCalls.costUsd})`));
}

/** Daily spend for a small chart: one row per UTC day that had any calls. */
export async function spendByDay(userId: string, since: Date) {
  return db
    .select({
      day: sql<string>`to_char(date_trunc('day', ${llmCalls.createdAt} at time zone 'UTC'), 'YYYY-MM-DD')`,
      usd: sql<number>`coalesce(sum(${llmCalls.costUsd}), 0)::float8`,
    })
    .from(llmCalls)
    .where(and(eq(llmCalls.userId, userId), gte(llmCalls.createdAt, since)))
    .groupBy(sql`1`)
    .orderBy(sql`1`);
}
