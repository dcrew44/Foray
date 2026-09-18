import { and, eq, inArray, notInArray, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { jobs, type Company } from "@/lib/db/schema";

import type { NormalizedJob } from "./types";

/**
 * Writing fetched postings into the shared mirror.
 *
 * Two rules from DESIGN.md shape this. A posting that has disappeared from its
 * source is kept with `is_active = false` rather than deleted, because the
 * mirror is history — a user who applied last week should still be able to open
 * the job they applied to. And `fetched_at` is cache metadata: it moves on
 * every poll, while `first_seen_at` and `last_seen_at` are facts about the job.
 */

export type IngestResult = {
  inserted: number;
  updated: number;
  unchanged: number;
  deactivated: number;
};

/** Postgres limits a statement's parameters, so large boards go in batches. */
const BATCH_SIZE = 200;

export async function ingestJobs(input: {
  company?: Pick<Company, "id" | "name"> | null;
  jobs: NormalizedJob[];
  /**
   * Mark postings from this company that were not in this fetch as inactive.
   * Only safe for a full board fetch — never for an aggregator search, which
   * only ever sees a slice of what exists.
   */
  deactivateMissing?: boolean;
}): Promise<IngestResult> {
  const result: IngestResult = {
    inserted: 0,
    updated: 0,
    unchanged: 0,
    deactivated: 0,
  };

  const now = new Date();

  for (let offset = 0; offset < input.jobs.length; offset += BATCH_SIZE) {
    const batch = input.jobs.slice(offset, offset + BATCH_SIZE);

    // What we already hold, so an unchanged posting is not rewritten.
    const existing = await db
      .select({ id: jobs.id, contentHash: jobs.contentHash })
      .from(jobs)
      .where(inArray(jobs.id, batch.map((job) => job.id)));

    const known = new Map(existing.map((row) => [row.id, row.contentHash]));

    const changed = batch.filter(
      (job) => known.get(job.id) !== job.contentHash,
    );
    const identical = batch.filter(
      (job) => known.has(job.id) && known.get(job.id) === job.contentHash,
    );

    // Unchanged postings still need their last_seen_at moved forward: that is
    // how we know the job is still open, as distinct from still in the table.
    if (identical.length > 0) {
      await db
        .update(jobs)
        .set({ lastSeenAt: now, fetchedAt: now })
        .where(inArray(jobs.id, identical.map((job) => job.id)));
      result.unchanged += identical.length;
    }

    if (changed.length === 0) continue;

    await db
      .insert(jobs)
      .values(
        changed.map((job) => ({
          id: job.id,
          source: job.source,
          externalId: job.externalId,
          companyId: input.company?.id ?? null,
          companyName: job.companyName,
          title: job.title,
          locations: job.locations,
          locationNorm: job.locationNorm,
          remote: job.remote,
          employmentType: job.employmentType,
          salaryMin: job.salary.min?.toString() ?? null,
          salaryMax: job.salary.max?.toString() ?? null,
          salaryCurrency: job.salary.currency,
          salaryPeriod: job.salary.period,
          descriptionRaw: job.descriptionRaw,
          descriptionText: job.descriptionText,
          url: job.url,
          applyUrl: job.applyUrl,
          postedAt: job.postedAt,
          raw: job.raw,
          contentHash: job.contentHash,
          dedupeKey: job.dedupeKey,
          isActive: true,
          firstSeenAt: now,
          lastSeenAt: now,
          fetchedAt: now,
        })),
      )
      .onConflictDoUpdate({
        target: jobs.id,
        set: {
          companyId: sql`excluded.company_id`,
          companyName: sql`excluded.company_name`,
          title: sql`excluded.title`,
          locations: sql`excluded.locations`,
          locationNorm: sql`excluded.location_norm`,
          remote: sql`excluded.remote`,
          employmentType: sql`excluded.employment_type`,
          salaryMin: sql`excluded.salary_min`,
          salaryMax: sql`excluded.salary_max`,
          salaryCurrency: sql`excluded.salary_currency`,
          salaryPeriod: sql`excluded.salary_period`,
          descriptionRaw: sql`excluded.description_raw`,
          descriptionText: sql`excluded.description_text`,
          url: sql`excluded.url`,
          applyUrl: sql`excluded.apply_url`,
          postedAt: sql`excluded.posted_at`,
          raw: sql`excluded.raw`,
          contentHash: sql`excluded.content_hash`,
          dedupeKey: sql`excluded.dedupe_key`,
          // A posting that came back after vanishing is open again.
          isActive: true,
          lastSeenAt: now,
          fetchedAt: now,
          // first_seen_at is deliberately NOT updated: it is when we first saw
          // the job, not when we last wrote the row.
        },
      });

    const newRows = changed.filter((job) => !known.has(job.id)).length;
    result.inserted += newRows;
    result.updated += changed.length - newRows;
  }

  if (input.deactivateMissing && input.company) {
    const seen = input.jobs.map((job) => job.id);

    const deactivated = await db
      .update(jobs)
      .set({ isActive: false, fetchedAt: now })
      .where(
        and(
          eq(jobs.companyId, input.company.id),
          eq(jobs.isActive, true),
          seen.length > 0 ? notInArray(jobs.id, seen) : undefined,
        ),
      )
      .returning({ id: jobs.id });

    result.deactivated = deactivated.length;
  }

  return result;
}
