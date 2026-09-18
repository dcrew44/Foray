import { and, eq, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { apiUsage } from "@/lib/db/schema";

/** The UTC month a quota window belongs to. */
export function currentPeriod(at: Date = new Date()): string {
  return `${at.getUTCFullYear()}-${String(at.getUTCMonth() + 1).padStart(2, "0")}`;
}

/**
 * Counts a request against this month's quota and returns the new total.
 *
 * The id is `provider:period`, so the counter is a single upsert with no read
 * first — two concurrent fetchers cannot both read 199 and both decide they
 * have room.
 */
export async function countRequest(
  provider: string,
  count = 1,
  at: Date = new Date(),
): Promise<number> {
  const period = currentPeriod(at);

  const [row] = await db
    .insert(apiUsage)
    .values({ id: `${provider}:${period}`, provider, period, requests: count })
    .onConflictDoUpdate({
      target: apiUsage.id,
      set: {
        requests: sql`${apiUsage.requests} + ${count}`,
        updatedAt: new Date(),
      },
    })
    .returning({ requests: apiUsage.requests });

  return row.requests;
}

export async function requestsThisPeriod(
  provider: string,
  at: Date = new Date(),
): Promise<number> {
  const [row] = await db
    .select({ requests: apiUsage.requests })
    .from(apiUsage)
    .where(
      and(
        eq(apiUsage.provider, provider),
        eq(apiUsage.period, currentPeriod(at)),
      ),
    )
    .limit(1);

  return row?.requests ?? 0;
}
