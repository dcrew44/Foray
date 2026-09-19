import { and, eq, gte, inArray, isNull, or, sql, type SQL } from "drizzle-orm";
import { z } from "zod";

import { db } from "@/lib/db";
import { jobMatches, jobs } from "@/lib/db/schema";
import type { LlmContext } from "@/lib/llm/context";
import { generateStructured } from "@/lib/llm/structured";
import type { Preferences } from "@/lib/profile/interview";

/**
 * Narrowing tens of thousands of postings to a few hundred worth paying to read.
 *
 * This is the cheap pass, and it is deliberately dumb: SQL over columns we
 * already normalized. Scoring every posting in the mirror with a model would
 * cost real money per run and take an hour; scoring a few hundred costs cents.
 *
 * The filter errs toward keeping things. A job wrongly excluded here is never
 * seen again by anything downstream, while a job wrongly kept only costs a
 * fraction of a cent to score and reject.
 */

/** Postings older than this are stale enough to be worth skipping. */
const MAX_AGE_DAYS = 45;

const DerivedCriteria = z.object({
  /**
   * Regex alternatives matched against the title. Broad: this is the widest
   * net, and the scoring pass is what judges fit.
   */
  title_include: z.array(z.string()),
  /**
   * Titles to exclude outright — seniority mismatches, unrelated disciplines.
   * Applied only when the title does not also match an include term.
   */
  title_exclude: z.array(z.string()),
  /** Words worth having in the description. Used for ordering, not filtering. */
  keywords: z.array(z.string()),
});

export type DerivedCriteria = z.infer<typeof DerivedCriteria>;

const CRITERIA_INSTRUCTIONS = `You turn a job seeker's stated preferences into title-matching terms for a database query.

- title_include: the job titles this person would actually accept, as short
  lowercase phrases. Include the obvious synonyms a company might use. Err wide:
  a later scoring pass judges fit, and anything excluded here is never seen
  again.
- title_exclude: titles that are clearly wrong for them — the wrong discipline,
  or a seniority they did not ask for. For an entry-level or new-grad search,
  exclude senior/staff/principal/director titles. For a senior search, exclude
  intern and new-grad titles. Never exclude something merely adjacent.
- keywords: technologies and domain words that would appear in a good match's
  description. Used for ranking, not filtering.

Return lowercase plain phrases, not regex syntax.`;

/**
 * Turns preferences into query terms, once per run.
 *
 * Cached into `runs.input` by the caller: the same criteria are needed again if
 * the run resumes after an approval, and paying twice for the same derivation
 * is waste.
 */
export async function deriveCriteria(
  preferences: Preferences,
  context: LlmContext = {},
): Promise<DerivedCriteria> {
  const { value } = await generateStructured(
    "score",
    DerivedCriteria,
    {
      instructions: CRITERIA_INSTRUCTIONS,
      prompt: `Preferences:\n${JSON.stringify(preferences, null, 2)}`,
    },
    context,
  );

  return {
    title_include: value.title_include.map(escapeForRegex).filter(Boolean),
    title_exclude: value.title_exclude.map(escapeForRegex).filter(Boolean),
    keywords: value.keywords.filter(Boolean),
  };
}

/** The model returns plain phrases; they still have to be safe in a regex. */
export function escapeForRegex(phrase: string): string {
  return phrase.trim().toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export type PrefilterResult = {
  jobIds: string[];
  /**
   * What the filter had to work with and what it chose. Deliberately only two
   * numbers: reporting a per-stage funnel would mean a query per stage, and a
   * made-up breakdown is worse than none.
   */
  funnel: { activeInMirror: number; selected: number; cappedAt: number | null };
};

/**
 * Selects the postings worth scoring for one user.
 *
 * Every clause is over a normalized column rather than free text, which is what
 * makes it cheap enough to run over the whole mirror.
 */
export async function prefilterJobs(input: {
  userId: string;
  preferences: Preferences;
  criteria: DerivedCriteria;
  limit?: number;
}): Promise<PrefilterResult> {
  const { userId, preferences, criteria } = input;
  const limit = input.limit ?? 600;

  const since = new Date(Date.now() - MAX_AGE_DAYS * 24 * 60 * 60 * 1000);

  // A posting with no date is kept: careers-page extractions have none, and
  // dropping them would silently exclude an entire source.
  const recent = or(isNull(jobs.postedAt), gte(jobs.postedAt, since));

  const conditions: SQL[] = [eq(jobs.isActive, true)];
  if (recent) conditions.push(recent);

  const location = locationClause(preferences);
  if (location) conditions.push(location);

  const title = titleClause(criteria);
  if (title) conditions.push(title);

  // Anything the user has already dismissed stays dismissed.
  const dismissed = db
    .select({ jobId: jobMatches.jobId })
    .from(jobMatches)
    .where(
      and(
        eq(jobMatches.userId, userId),
        inArray(jobMatches.stage, ["rejected", "archived"]),
      ),
    );

  conditions.push(sql`${jobs.id} not in (${dismissed})`);

  // Aggregator duplicates of a posting we hold from its own board.
  conditions.push(isNull(jobs.canonicalJobId));

  const rows = await db
    .select({ id: jobs.id })
    .from(jobs)
    .where(and(...conditions))
    .orderBy(sql`${jobs.postedAt} desc nulls last`)
    .limit(limit);

  const [counts] = await db
    .select({
      active: sql<number>`count(*) filter (where ${jobs.isActive})::int`,
    })
    .from(jobs);

  return {
    jobIds: rows.map((row) => row.id),
    funnel: {
      activeInMirror: counts?.active ?? 0,
      selected: rows.length,
      // Says so explicitly when the cap, rather than the criteria, decided.
      cappedAt: rows.length === limit ? limit : null,
    },
  };
}

/**
 * Location and remote preference as one clause.
 *
 * Remote and location are one question, not two: someone who will take remote
 * work wants remote roles *and* roles in their metro, and asking for both
 * separately would exclude everything.
 */
function locationClause(preferences: Preferences): SQL | undefined {
  const wantsRemote =
    preferences.remote_pref === "remote" || preferences.remote_pref === "any";

  const metros = new Set<string>();
  for (const location of preferences.locations ?? []) {
    if (/bay area|san francisco|sf|silicon valley|peninsula/i.test(location)) {
      metros.add("bay-area");
    }
  }

  const clauses: SQL[] = [];

  if (metros.size > 0) {
    clauses.push(
      sql`${jobs.locationNorm}->>'metro' in (${sql.join(
        [...metros].map((metro) => sql`${metro}`),
        sql`, `,
      )})`,
    );
  }

  // Free-text fallback for locations outside the gazetteer.
  for (const location of preferences.locations ?? []) {
    const city = location.split(",")[0]?.trim();
    if (city && city.length > 2 && !/bay area|remote|anywhere/i.test(city)) {
      clauses.push(sql`${jobs.locations}::text ilike ${"%" + city + "%"}`);
    }
  }

  if (wantsRemote) clauses.push(eq(jobs.remote, "remote"));
  if (preferences.remote_pref === "hybrid") {
    clauses.push(eq(jobs.remote, "hybrid"));
  }

  if (clauses.length === 0) return undefined;
  return or(...clauses);
}

/**
 * Title matching.
 *
 * An exclusion never overrides an include: "Senior Software Engineer, New Grad
 * Program" should survive a new-grad search even though it says Senior.
 */
function titleClause(criteria: DerivedCriteria): SQL | undefined {
  const patterns = buildTitlePatterns(criteria);
  if (!patterns) return undefined;

  const include = sql`${jobs.title} ~* ${patterns.include}`;
  if (!patterns.exclude) return include;

  return and(include, sql`${jobs.title} !~* ${patterns.exclude}`);
}

/**
 * The regex strings the query uses, built here so they can be tested without a
 * database. Postgres `~*` and JavaScript's case-insensitive RegExp agree on
 * plain alternations of escaped literals, which is all these ever are.
 */
export function buildTitlePatterns(
  criteria: DerivedCriteria,
): { include: string; exclude: string | null } | null {
  if (criteria.title_include.length === 0) return null;

  return {
    include: criteria.title_include.join("|"),
    exclude:
      criteria.title_exclude.length > 0
        ? criteria.title_exclude.join("|")
        : null,
  };
}

/**
 * Whether a title survives the filter. Mirrors the SQL above and exists for
 * tests and for explaining a decision to the user.
 */
export function titleMatches(
  title: string,
  criteria: DerivedCriteria,
): boolean {
  const patterns = buildTitlePatterns(criteria);
  if (!patterns) return true;

  if (!new RegExp(patterns.include, "i").test(title)) return false;
  if (!patterns.exclude) return true;
  return !new RegExp(patterns.exclude, "i").test(title);
}
