import {
  index,
  integer,
  jsonb,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";

import { documentKind, renderEngine, resumeKind } from "./enums";
import { user } from "./auth";
import { jobMatches } from "./jobs";

/**
 * Who the user is, what they uploaded, and every resume we hold for them.
 */

export const profiles = pgTable(
  "profiles",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: text("user_id")
      .notNull()
      .unique()
      .references(() => user.id, { onDelete: "cascade" }),

    /**
     * The canonical profile: basics, summary, skills, work, education,
     * projects, links. Every item carries `id` and `source` (resume |
     * linkedin_pdf | user) so a later import never silently overwrites
     * something the user typed.
     */
    data: jsonb("data").notNull().default({}),

    /**
     * What they are looking for: target_roles, seniority, locations,
     * remote_pref, salary_floor, work_auth, start_date, must_haves,
     * dealbreakers. The search run refuses to start until the essential
     * fields here are known.
     */
    preferences: jsonb("preferences").notNull().default({}),

    /** Versioned interview slots — which have been asked, which answered. */
    interviewState: jsonb("interview_state").notNull().default({}),

    /**
     * How the agents behave for this user: default autonomy, the fast-score
     * archive threshold, how many matches get a deep evaluation. See
     * lib/profile/settings.ts for the shape and defaults.
     */
    settings: jsonb("settings").notNull().default({}),

    /**
     * Reserved for the career-counselor roadmap: strengths, skill gaps,
     * base-resume advice aggregated across deep evals. Unused in Phase 1.
     */
    assessment: jsonb("assessment"),

    createdAt: timestamp("created_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
);

/** Every stored file: what the user uploaded and what we rendered. */
export const documents = pgTable(
  "documents",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: text("user_id")
      .notNull()
      .references(() => user.id, { onDelete: "cascade" }),

    kind: documentKind("kind").notNull(),
    filename: text("filename").notNull(),
    mime: text("mime").notNull(),
    sizeBytes: integer("size_bytes").notNull(),
    /** Opaque to callers — the `Storage` interface resolves it. */
    storagePath: text("storage_path").notNull(),
    sha256: text("sha256").notNull(),
    /** Text pulled out of the file, before any LLM sees it. */
    textExtracted: text("text_extracted"),

    createdAt: timestamp("created_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [
    index("documents_user_kind_idx").on(t.userId, t.kind),
    // The same upload twice is the same document.
    uniqueIndex("documents_user_sha256_idx").on(t.userId, t.sha256),
  ],
);

export const resumes = pgTable(
  "resumes",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: text("user_id")
      .notNull()
      .references(() => user.id, { onDelete: "cascade" }),

    kind: resumeKind("kind").notNull(),
    /** Set on tailored resumes: the match this was written for. */
    jobMatchId: uuid("job_match_id").references(() => jobMatches.id, {
      onDelete: "cascade",
    }),
    version: integer("version").notNull().default(1),

    /** The canonical Resume schema (JSON-Resume-like). */
    data: jsonb("data").notNull(),

    renderedDocumentId: uuid("rendered_document_id").references(
      () => documents.id,
      { onDelete: "set null" },
    ),
    renderEngine: renderEngine("render_engine"),

    /**
     * What tailoring changed: section, before, after, reason, and the
     * evidence_fact_ids in the base resume that support it. The audit trail
     * that makes a tailored claim checkable.
     */
    changes: jsonb("changes"),
    /** The verifier's report: unsupported claims found, revisions applied. */
    verification: jsonb("verification"),

    createdAt: timestamp("created_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .defaultNow()
      .notNull(),
  },
  (t) => [
    index("resumes_user_kind_idx").on(t.userId, t.kind),
    index("resumes_job_match_id_idx").on(t.jobMatchId),
  ],
);

export type Profile = typeof profiles.$inferSelect;
export type NewProfile = typeof profiles.$inferInsert;
export type Document = typeof documents.$inferSelect;
export type NewDocument = typeof documents.$inferInsert;
export type Resume = typeof resumes.$inferSelect;
export type NewResume = typeof resumes.$inferInsert;
