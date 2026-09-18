import { sql } from "drizzle-orm";
import {
  boolean,
  index,
  integer,
  jsonb,
  numeric,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";

import {
  companySourceType,
  companyStatus,
  discoveredVia,
  jobSource,
  matchStage,
  remoteKind,
} from "./enums";
import { user } from "./auth";

/**
 * The shared mirror.
 *
 * `companies` and `jobs` are not per-user: one fetch serves everyone, and a
 * posting that disappears from its source is kept with `is_active = false` so
 * history survives. Everything downstream of this file is per-user and carries
 * `user_id`.
 */

export const companies = pgTable(
  "companies",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    name: text("name").notNull(),
    /** Bare registrable domain, e.g. `stripe.com`. */
    domain: text("domain"),
    /** Our own slug; also the middle segment of every job id. */
    slug: text("slug").notNull().unique(),

    sourceType: companySourceType("source_type").notNull(),
    /** The board identifier at the ATS, e.g. `stripe` for Greenhouse. */
    atsSlug: text("ats_slug"),
    careersUrls: text("careers_urls").array().notNull().default(sql`'{}'`),

    tags: text("tags").array().notNull().default(sql`'{}'`),
    status: companyStatus("status").notNull().default("active"),

    lastFetchedAt: timestamp("last_fetched_at", { withTimezone: true }),
    lastFetchStatus: text("last_fetch_status"),
    lastFetchJobCount: integer("last_fetch_job_count"),
    /** Set when a careers page needs JS we cannot run until Phase 2. */
    needsBrowser: boolean("needs_browser").notNull().default(false),

    discoveredVia: discoveredVia("discovered_via").notNull(),
    /** Who asked for it, when a user added it by hand. Not an owner. */
    addedByUserId: text("added_by_user_id").references(() => user.id, {
      onDelete: "set null",
    }),

    createdAt: timestamp("created_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [
    // One registration per board. Partial, because `careers_page` and `none`
    // companies have no ats_slug and would otherwise all collide on NULL.
    uniqueIndex("companies_source_ats_slug_idx")
      .on(t.sourceType, t.atsSlug)
      .where(sql`${t.atsSlug} is not null`),
    index("companies_status_idx").on(t.status),
    index("companies_last_fetched_at_idx").on(t.lastFetchedAt),
  ],
);

export const jobs = pgTable(
  "jobs",
  {
    /** `source:company_slug:external_id` — see DESIGN.md invariant 2. */
    id: text("id").primaryKey(),
    source: jobSource("source").notNull(),
    /** The id at the source, before it is folded into our composite id. */
    externalId: text("external_id").notNull(),

    companyId: uuid("company_id").references(() => companies.id, {
      onDelete: "set null",
    }),
    /** Denormalized: aggregator rows arrive before the company is registered. */
    companyName: text("company_name").notNull(),

    title: text("title").notNull(),
    locations: text("locations").array().notNull().default(sql`'{}'`),
    /** Normalized location: city, region, country, metro, raw. */
    locationNorm: jsonb("location_norm"),
    remote: remoteKind("remote").notNull().default("unknown"),
    employmentType: text("employment_type"),

    salaryMin: numeric("salary_min"),
    salaryMax: numeric("salary_max"),
    salaryCurrency: text("salary_currency"),
    /** year | month | hour — as the source states it, never converted. */
    salaryPeriod: text("salary_period"),

    /** Exactly as fetched. Never rewritten — see DESIGN.md invariant 4. */
    descriptionRaw: text("description_raw"),
    /** HTML stripped and known boilerplate removed. Derived, disposable. */
    descriptionText: text("description_text"),

    url: text("url").notNull(),
    applyUrl: text("apply_url"),
    postedAt: timestamp("posted_at", { withTimezone: true }),

    /** The source's own payload, kept so a parser fix can be replayed. */
    raw: jsonb("raw"),
    /** Hash of the content we care about, for change detection. */
    contentHash: text("content_hash"),
    /** sha1(norm(company)|norm(title)|norm(primary_location)). */
    dedupeKey: text("dedupe_key"),
    /** Points at the ATS posting when this row is an aggregator duplicate. */
    canonicalJobId: text("canonical_job_id"),

    isActive: boolean("is_active").notNull().default(true),
    firstSeenAt: timestamp("first_seen_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
    lastSeenAt: timestamp("last_seen_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
    /** Cache metadata, not a job fact — see DESIGN.md invariant 6. */
    fetchedAt: timestamp("fetched_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [
    index("jobs_company_id_idx").on(t.companyId),
    index("jobs_dedupe_key_idx").on(t.dedupeKey),
    index("jobs_canonical_job_id_idx").on(t.canonicalJobId),
    // The search prefilter's shape: active postings, newest first.
    index("jobs_active_posted_at_idx")
      .on(t.postedAt.desc())
      .where(sql`${t.isActive}`),
    index("jobs_fts_idx").using(
      "gin",
      sql`to_tsvector('english', ${t.title} || ' ' || coalesce(${t.descriptionText}, ''))`,
    ),
  ],
);

/**
 * A user's relationship to one canonical job: where it sits on their board,
 * what the two scoring passes concluded, and which tailored resume they have.
 */
export const jobMatches = pgTable(
  "job_matches",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: text("user_id")
      .notNull()
      .references(() => user.id, { onDelete: "cascade" }),
    jobId: text("job_id")
      .notNull()
      .references(() => jobs.id, { onDelete: "cascade" }),

    stage: matchStage("stage").notNull().default("found"),
    stageChangedAt: timestamp("stage_changed_at", { withTimezone: true })
      .defaultNow()
      .notNull(),

    /** Cheap-tier pass over everything that survived the SQL prefilter. */
    fastScore: integer("fast_score"),
    fastEval: jsonb("fast_eval"),
    /** Strong-tier pass over the top N, with evidence quoted from the posting. */
    deepScore: integer("deep_score"),
    deepEval: jsonb("deep_eval"),

    userRating: integer("user_rating"),
    notes: text("notes"),

    tailoredResumeId: uuid("tailored_resume_id"),

    createdAt: timestamp("created_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [
    uniqueIndex("job_matches_user_job_idx").on(t.userId, t.jobId),
    index("job_matches_user_stage_idx").on(t.userId, t.stage),
    index("job_matches_user_fast_score_idx").on(t.userId, t.fastScore.desc()),
  ],
);

export type Company = typeof companies.$inferSelect;
export type NewCompany = typeof companies.$inferInsert;
export type Job = typeof jobs.$inferSelect;
export type NewJob = typeof jobs.$inferInsert;
export type JobMatch = typeof jobMatches.$inferSelect;
export type NewJobMatch = typeof jobMatches.$inferInsert;
