import {
  and,
  eq,
  gte,
  inArray,
  isNull,
  notInArray,
  or,
  sql,
  type SQL,
} from "drizzle-orm";
import { z } from "zod";

import { db } from "@/lib/db";
import { jobMatches, jobs } from "@/lib/db/schema";
import type { LlmContext } from "@/lib/llm/context";
import { generateStructured } from "@/lib/llm/structured";
import type { Preferences } from "@/lib/profile/interview";

import { cleanBrief, mergeBrief, SearchBrief } from "./brief";

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
  /**
   * Regex alternatives for titles that survive an exclusion, e.g. "Senior
   * Software Engineer, New Grad Program" in a new-grad search. Set by
   * `guardSeniority`, never by the model; absent on criteria cached before it.
   */
  title_keep: z.array(z.string()).optional(),
});

export type DerivedCriteria = z.infer<typeof DerivedCriteria>;

const SEARCH_INSTRUCTIONS = `You turn a job search request into a brief and title-matching terms for a database query.

The person has saved preferences for what they generally want. A request may ask for
something narrower or different for this one search, and the request wins wherever it
says something.

First, the brief: only what the request itself states. Anything it does not mention is
null, and the saved preference applies.
- roles: the kinds of role asked for, as short titles ("software engineer", "data
  analyst"). "Developer positions" is roles. Null if the request names none.
- seniority: intern, new_grad, entry, mid, senior or staff_plus when the request states
  a level. "Entry-level", "junior", "early career", "most junior-friendly" and "least
  experience" are entry; "new grad" or "university grad" is new_grad. Null otherwise.
- locations: places the request names. Null otherwise.
- remote: remote, hybrid, onsite or any, only when the request says so.
- companies: company names the request limits the search to ("at Anthropic",
  "Stripe or Plaid"), as written. Null otherwise.
- focus: anything else in the request that should shape the ranking ("lowest
  experience requirements first", "fintech only"), in one short sentence. Null if none.

Then the title terms, for the effective search: the brief where it says something, the
saved preferences everywhere else.
- title_include: the job titles this search would accept, as short lowercase phrases,
  without a level word: "backend engineer", not "senior backend engineer" (many
  companies never put the level in a title). Include the obvious synonyms and word
  orders a company might use ("software engineer, backend"). Err wide: a later scoring
  pass judges fit, and anything excluded here is never seen again.
  For an entry-level or new-grad search, also include the early-career titles roles in
  that discipline carry: new grad, junior, associate, early career, residency,
  apprenticeship and fellowship programs.
- title_exclude: titles that are clearly wrong for this search: the wrong discipline,
  or a seniority it did not ask for. For an entry-level or new-grad search, exclude
  senior, staff, principal, lead, manager and director titles, and never exclude
  early-career titles. For a senior search, exclude intern and new-grad titles. Never
  exclude something merely adjacent.
- keywords: technologies and domain words a good match's description would contain.
  Used for ranking, not filtering.

Return lowercase plain phrases for the title terms, not regex syntax.`;

const DerivedSearch = z.object({
  brief: SearchBrief,
  title_include: z.array(z.string()),
  title_exclude: z.array(z.string()),
  keywords: z.array(z.string()),
});

/**
 * Reads a search request against the saved preferences, once per run: what
 * this search is for (the brief) and the title terms that find it.
 *
 * `hints` are fields the chat agent already resolved in its tool call. They
 * go into the prompt, so the title terms agree with them, and are laid over
 * the model's brief afterwards, so they hold even if the model drops one.
 *
 * Cached into `runs.input` by the caller: a run that resumes after an
 * approval needs the same answer, and paying twice for it is waste.
 */
export async function deriveSearch(input: {
  preferences: Preferences;
  /** The request in the user's words; null for "search with my preferences". */
  request: string | null;
  hints?: Partial<SearchBrief> | null;
  context?: LlmContext;
}): Promise<{ brief: SearchBrief; criteria: DerivedCriteria }> {
  const hints = cleanBrief(input.hints ?? {});
  const resolved = Object.fromEntries(
    Object.entries(hints).filter(([, value]) => value != null),
  );

  const { value } = await generateStructured(
    "score",
    DerivedSearch,
    {
      instructions: SEARCH_INSTRUCTIONS,
      prompt: [
        `Saved preferences:\n${JSON.stringify(input.preferences, null, 2)}`,
        input.request
          ? `This search request:\n${input.request}`
          : "This search request: none. Search with the saved preferences; every brief field is null.",
        Object.keys(resolved).length
          ? `Already resolved for this search (treat these as stated in the request):\n${JSON.stringify(resolved, null, 2)}`
          : null,
      ]
        .filter(Boolean)
        .join("\n\n"),
    },
    input.context ?? {},
  );

  // Models answer "any" for a remote preference nobody stated, which would
  // override a saved "onsite"; "any" only counts when the chat agent set it.
  const read = input.request
    ? { ...value.brief, remote: value.brief.remote === "any" ? null : value.brief.remote }
    : {};
  const brief = mergeBrief(read, hints);
  return {
    brief,
    criteria: guardSeniority(
      {
        title_include: dedupe(value.title_include.map(withoutLevel).map(escapeForRegex)),
        title_exclude: value.title_exclude.map(escapeForRegex).filter(Boolean),
        keywords: value.keywords.filter(Boolean),
      },
      brief.seniority ?? input.preferences.seniority,
      brief.roles ?? input.preferences.target_roles,
      { companies: Boolean(brief.companies) },
    ),
  };
}

/** A word or phrase bounded by non-letters, written to mean the same in Postgres and JavaScript. */
function wordPattern(phrase: string): string {
  return `(^|[^a-z])${phrase.replace(/ /g, "[^a-z]+")}([^a-z]|$)`;
}

/** Early-career title words. A title carrying one is never excluded from a junior search. */
const EARLY_CAREER_WORDS = [
  "intern",
  "internship",
  "new grad",
  "new graduate",
  "graduate program",
  "junior",
  "jr",
  "entry level",
  "associate",
  "early career",
  "university grad",
  "resident",
  "residency",
  "apprentice",
  "apprenticeship",
  "fellow",
  "fellows",
  "fellowship",
  "rotational",
];

const SENIOR_WORDS = [
  "senior",
  "sr",
  "staff",
  "principal",
  "lead",
  "manager",
  "director",
  "head of",
  "vice president",
  "vp",
  "distinguished",
];

/**
 * The title terms a model proposed, made safe for a junior search.
 *
 * Model-written exclusions are literal phrases, and titles are not: "staff
 * software engineer" does not catch "Staff+ Software Engineer" or "Staff + Sr.
 * Software Engineer", which let 45 staff roles into a search for Anthropic's
 * most entry-level ones. The same model excluded "new grad" and "intern" from
 * that search. So for intern, new-grad and entry-level searches, senior words
 * are excluded as whole words (unless the roles asked for contain one, as
 * "associate product manager" contains "manager"), early-career exclusions are
 * dropped, and a title with an early-career word survives any exclusion.
 *
 * A junior search limited to named companies also includes every
 * early-career title those companies post: that set is small, and a
 * program like "Anthropic Fellows Program, ML Systems" is often the most
 * junior way in while matching no role title. Scoring judges the discipline.
 */
export function guardSeniority(
  criteria: DerivedCriteria,
  seniority: string | null | undefined,
  roles?: string[] | null,
  options: { companies?: boolean } = {},
): DerivedCriteria {
  if (seniority !== "intern" && seniority !== "new_grad" && seniority !== "entry") {
    return criteria;
  }

  const early = new RegExp(EARLY_CAREER_WORDS.map(wordPattern).join("|"), "i");
  const wanted = (roles ?? []).join(" ").toLowerCase();
  const senior = SENIOR_WORDS.filter((word) => !new RegExp(wordPattern(word), "i").test(wanted)).map(
    wordPattern,
  );
  const kept = criteria.title_exclude.filter((term) => !early.test(term.replace(/\\/g, "")));

  const earlyTitles = EARLY_CAREER_WORDS.map(wordPattern);

  return {
    ...criteria,
    title_include: options.companies
      ? [...criteria.title_include, ...earlyTitles.filter((term) => !criteria.title_include.includes(term))]
      : criteria.title_include,
    title_exclude: [...kept, ...senior.filter((term) => !kept.includes(term))],
    title_keep: earlyTitles,
  };
}

/**
 * An include term without a leading level word: "senior backend engineer"
 * becomes "backend engineer". Stripe, Ramp and many others never put "Senior"
 * in a title, so a literal senior phrase found none of their roles. The level
 * is judged by the exclusions and by scoring instead.
 */
export function withoutLevel(phrase: string): string {
  return phrase
    .trim()
    // Not "lead": in "lead generation specialist" it is the job, not a level.
    .replace(/^(?:(?:senior|sr\.?|staff|principal|junior|jr\.?|mid[- ]level|entry[- ]level)\s+)+/i, "")
    .trim();
}

function dedupe(terms: string[]): string[] {
  return [...new Set(terms.filter(Boolean))];
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
  /** The effective preferences: saved ones with this search's brief applied. */
  preferences: Preferences;
  criteria: DerivedCriteria;
  /**
   * Limits the search to these companies: registered ones by id, and by name
   * for aggregator postings from companies we do not track.
   */
  companies?: CompanyFilter | null;
  limit?: number;
}): Promise<PrefilterResult> {
  const { userId, preferences, criteria } = input;
  const limit = input.limit ?? 600;

  const conditions: SQL[] = [eq(jobs.isActive, true)];

  const company = companyClause(input.companies);
  if (company) {
    conditions.push(company);
  } else {
    // The age cutoff keeps a broad search to fresh postings. It is skipped
    // when the user names companies: what those boards list is open now, and
    // "what is open at Anthropic" includes roles posted two months ago.
    const since = new Date(Date.now() - MAX_AGE_DAYS * 24 * 60 * 60 * 1000);
    // A posting with no date is kept: careers-page extractions have none, and
    // dropping them would silently exclude an entire source.
    const recent = or(isNull(jobs.postedAt), gte(jobs.postedAt, since));
    if (recent) conditions.push(recent);
  }

  const location = locationClause(preferences);
  if (location) conditions.push(location);

  const title = titleClause(criteria);
  if (title) conditions.push(title);

  // Aggregator duplicates of a posting we hold from its own board.
  conditions.push(isNull(jobs.canonicalJobId));

  // One role, one match. A company posting the same role in four cities, or a
  // recruiter reposting it under new ids, is one thing to the user — the
  // search used to hand back four copies of it. Roles the user already has a
  // match for are skipped here too (dismissed ones stay dismissed), except the
  // very posting they matched, which may be re-selected harmlessly.
  const known = await db
    .select({ jobId: jobMatches.jobId, key: roleKey(jobs) })
    .from(jobMatches)
    .innerJoin(jobs, eq(jobs.id, jobMatches.jobId))
    .where(eq(jobMatches.userId, userId));

  const knownKeys = [...new Set(known.map((row) => row.key))];
  const knownIds = known.map((row) => row.jobId);
  const dismissedIds = new Set(
    (
      await db
        .select({ jobId: jobMatches.jobId })
        .from(jobMatches)
        .where(
          and(
            eq(jobMatches.userId, userId),
            inArray(jobMatches.stage, ["rejected", "archived"]),
          ),
        )
    ).map((row) => row.jobId),
  );

  if (dismissedIds.size > 0) {
    conditions.push(notInArray(jobs.id, [...dismissedIds]));
  }
  if (knownKeys.length > 0) {
    // Drizzle renders an array as a parameter list, which is what `not in` takes.
    const unseenRole = or(
      sql`${roleKey(jobs)} not in ${knownKeys}`,
      inArray(jobs.id, knownIds),
    );
    if (unseenRole) conditions.push(unseenRole);
  }

  // Within the survivors, keep the best copy of each role: the company's own
  // board over the aggregator, then the most recent.
  const deduped = db
    .selectDistinctOn([roleKey(jobs)], {
      id: jobs.id,
      postedAt: jobs.postedAt,
    })
    .from(jobs)
    .where(and(...conditions))
    .orderBy(
      roleKey(jobs),
      sql`case when ${jobs.source} = 'jsearch' then 1 else 0 end`,
      sql`${jobs.postedAt} desc nulls last`,
    )
    .as("deduped");

  const rows = await db
    .select({ id: deduped.id })
    .from(deduped)
    .orderBy(sql`${deduped.postedAt} desc nulls last`)
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

export type CompanyFilter = {
  /** Registered companies, matched on `jobs.company_id`. */
  ids: string[];
  /** Names, matched as a whole word at the start of `jobs.company_name`. */
  names: string[];
};

function companyClause(filter: CompanyFilter | null | undefined): SQL | undefined {
  if (!filter || (filter.ids.length === 0 && filter.names.length === 0)) {
    return undefined;
  }
  const clauses: SQL[] = [];
  if (filter.ids.length > 0) clauses.push(inArray(jobs.companyId, filter.ids));
  for (const name of filter.names) {
    const pattern = companyNamePattern(name);
    if (pattern) clauses.push(sql`${jobs.companyName} ~* ${pattern}`);
  }
  return or(...clauses);
}

/**
 * "Anthropic" matches "Anthropic" and "Anthropic PBC" but not "Anthropics
 * Labs", and "Meta" does not match "Metabase". Postgres spells a word boundary
 * `\y`.
 */
export function companyNamePattern(name: string): string | null {
  const words = name
    .trim()
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean)
    .map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (words.length === 0) return null;
  return `^${words.join("[^a-z0-9]+")}\\y`;
}

/**
 * Location and remote preference as one clause.
 *
 * Remote and location are one question, not two: someone who will take remote
 * work wants remote roles *and* roles in their metro, and asking for both
 * separately would exclude everything.
 */
/** Bay Area place names as whole words, for Postgres (`\\y` is its word boundary). */
export const BAY_AREA_PLACES = `\\y(${[
  "san francisco",
  "sf",
  "bay area",
  "silicon valley",
  "oakland",
  "berkeley",
  "emeryville",
  "san jose",
  "palo alto",
  "mountain view",
  "menlo park",
  "sunnyvale",
  "santa clara",
  "cupertino",
  "redwood city",
  "san mateo",
  "foster city",
  "burlingame",
  "san bruno",
  "san carlos",
  "fremont",
  "milpitas",
  "los gatos",
].join("|")})\\y`;

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
  // The normalizer places one location per posting, and boards write several
  // in one string ("Seattle, San Francisco, New York", "SEA, SF, NY",
  // "San Francisco HQ"), which it leaves without a metro. Stripe and Plaid
  // list most of their Bay Area roles that way, so the raw text is checked
  // against the metro's places too.
  if (metros.has("bay-area")) {
    clauses.push(sql`${jobs.locations}::text ~* ${BAY_AREA_PLACES}`);
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
 * A title must match an include term and no exclude term, except that a title
 * matching a keep term survives the excludes: "Senior Software Engineer, New
 * Grad Program" belongs in a new-grad search even though it says Senior.
 */
function titleClause(criteria: DerivedCriteria): SQL | undefined {
  const patterns = buildTitlePatterns(criteria);
  if (!patterns) return undefined;

  const include = sql`${jobs.title} ~* ${patterns.include}`;
  if (!patterns.exclude) return include;

  const allowed = patterns.keep
    ? or(sql`${jobs.title} !~* ${patterns.exclude}`, sql`${jobs.title} ~* ${patterns.keep}`)
    : sql`${jobs.title} !~* ${patterns.exclude}`;
  return and(include, allowed);
}

/**
 * The regex strings the query uses, built here so they can be tested without a
 * database. Postgres `~*` and JavaScript's case-insensitive RegExp agree on
 * plain alternations of escaped literals, which is all these ever are.
 */
export function buildTitlePatterns(
  criteria: DerivedCriteria,
): { include: string; exclude: string | null; keep: string | null } | null {
  if (criteria.title_include.length === 0) return null;

  return {
    include: criteria.title_include.join("|"),
    exclude:
      criteria.title_exclude.length > 0
        ? criteria.title_exclude.join("|")
        : null,
    keep: criteria.title_keep?.length ? criteria.title_keep.join("|") : null,
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
  if (patterns.keep && new RegExp(patterns.keep, "i").test(title)) return true;
  return !new RegExp(patterns.exclude, "i").test(title);
}

/**
 * What makes two postings "the same role" to a job seeker: the company and the
 * title, normalized, regardless of location or source. Coarser than the
 * ingest dedupe key on purpose — that one keeps locations apart because the
 * mirror should, but a person does not want the same role four times.
 */
export function roleKey(table: typeof jobs) {
  return sql<string>`lower(regexp_replace(${table.companyName}, '[^a-zA-Z0-9]+', '', 'g')) || '|' || btrim(lower(regexp_replace(${table.title}, '[^a-zA-Z0-9+#]+', ' ', 'g')))`;
}
