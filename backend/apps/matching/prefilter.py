"""Narrowing tens of thousands of postings to a few hundred worth paying to read (design §9.4;
`map/llm.md` §11; a port of `src/lib/ranking/prefilter.ts`).

This is the cheap pass, and it is deliberately dumb: SQL over columns already normalized.
Scoring every posting in the mirror with a model would cost real money per run and take an
hour; scoring a few hundred costs cents. The filter errs toward keeping things: a job wrongly
excluded here is never seen again by anything downstream, while a job wrongly kept only costs a
fraction of a cent to score and reject.

- `derive_search` reads a request against the saved preferences, once per run (purpose
  `score`): the brief and the title terms. The caller caches both in `runs.input`.
- `prefilter_jobs` is raw SQL: Postgres `~*` with `\\y`, the `DISTINCT ON` role key, the 600
  cap and the 45-day window. It runs inside a transaction that first sets a 20 s
  `statement_timeout`, because it executes model-written regexes.

The JavaScript regexes are mirrored with ASCII semantics (`re.ASCII`): JS without the `u` flag
matches `\\b`, `\\w` and case-insensitivity on ASCII only.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.db import connection, transaction
from pydantic import BaseModel, ConfigDict

from apps.accounts.schemas import Preferences
from apps.core.choices import JsonModel
from apps.core.jsonio import js_json_indent
from apps.llm.structured import structured
from apps.matching.brief import SearchBrief, clean_brief, merge_brief
from apps.resumes.text import JS_SPACE, js_trim

__all__ = [
    "BAY_AREA_PLACES",
    "DEFAULT_LIMIT",
    "MAX_AGE_DAYS",
    "SEARCH_INSTRUCTIONS",
    "CompanyFilter",
    "DerivedCriteria",
    "DerivedSearch",
    "PrefilterResult",
    "build_title_patterns",
    "company_name_pattern",
    "derive_search",
    "derive_search_prompt",
    "escape_for_regex",
    "finish_derived",
    "guard_seniority",
    "prefilter_jobs",
    "title_matches",
    "without_level",
    "word_pattern",
]

# Postings older than this are stale enough to be worth skipping.
MAX_AGE_DAYS = 45
DEFAULT_LIMIT = 600
STATEMENT_TIMEOUT = "20s"


class DerivedCriteria(JsonModel):
    # Regex alternatives matched against the title. Broad: this is the widest net, and the
    # scoring pass is what judges fit.
    title_include: list[str]
    # Titles to exclude outright: seniority mismatches, unrelated disciplines. Applied only
    # when the title does not also match a keep term.
    title_exclude: list[str]
    # Words worth having in the description. Used for ordering, not filtering.
    keywords: list[str]
    # Regex alternatives for titles that survive an exclusion ("Senior Software Engineer, New
    # Grad Program" in a new-grad search). Set by `guard_seniority`, never by the model; absent
    # on criteria cached before it existed.
    title_keep: list[str] | None = None

    def dump(self, **kwargs: Any) -> dict[str, Any]:
        return super().dump(exclude_none=True, **kwargs)


class DerivedSearch(BaseModel):
    # The model's answer. No docstring: it would become the schema's description.
    model_config = ConfigDict(extra="ignore")

    brief: SearchBrief
    title_include: list[str]
    title_exclude: list[str]
    keywords: list[str]


SEARCH_INSTRUCTIONS = """You turn a job search request into a brief and title-matching terms for a database query.

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

Return lowercase plain phrases for the title terms, not regex syntax."""  # noqa: E501 (prompt text, pinned by the §8.10 goldens)


# --- deriving the search -------------------------------------------------------------------------


def js_value(value: Any) -> Any:
    """A JSON value as JavaScript holds it: an integral float is an int (`150000`, not
    `150000.0`)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: js_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [js_value(inner) for inner in value]
    return value


def derive_search_prompt(
    preferences: Preferences, request: str | None, hints: SearchBrief | None = None
) -> str:
    """The per-run half of `derive_search`. `hints` are the brief fields the chat agent already
    resolved; they go into the prompt so the title terms agree with them."""
    resolved = {
        key: value
        for key, value in clean_brief(hints if hints is not None else {}).dump().items()
        if value is not None
    }
    parts = [
        f"Saved preferences:\n{js_json_indent(js_value(preferences.dump()))}",
        f"This search request:\n{request}"
        if request
        else "This search request: none. Search with the saved preferences; every brief field "
        "is null.",
    ]
    if resolved:
        parts.append(
            "Already resolved for this search (treat these as stated in the request):\n"
            f"{js_json_indent(resolved)}"
        )
    return "\n\n".join(parts)


def finish_derived(
    value: DerivedSearch,
    *,
    preferences: Preferences,
    request: str | None,
    hints: SearchBrief | None,
) -> tuple[SearchBrief, DerivedCriteria]:
    """The model's answer made into this run's brief and criteria (no model call)."""
    # Models answer "any" for a remote preference nobody stated, which would override a saved
    # "onsite"; "any" only counts when the chat agent set it. Without a request the model's
    # brief is discarded: every field is "as saved".
    read: dict[str, Any] = {}
    if request:
        read = value.brief.model_dump()
        if read.get("remote") == "any":
            read["remote"] = None
    brief = merge_brief(read, hints)
    include = [escape_for_regex(without_level(term)) for term in value.title_include]
    criteria = DerivedCriteria(
        title_include=list(dict.fromkeys(term for term in include if term)),
        title_exclude=[t for t in (escape_for_regex(term) for term in value.title_exclude) if t],
        keywords=[keyword for keyword in value.keywords if keyword],
    )
    criteria = guard_seniority(
        criteria,
        brief.seniority or preferences.seniority,
        brief.roles if brief.roles is not None else preferences.target_roles,
        companies=bool(brief.companies),
    )
    return brief, criteria


async def derive_search(
    preferences: Preferences, request: str | None, hints: SearchBrief | None = None
) -> tuple[SearchBrief, DerivedCriteria]:
    """Reads a search request against the saved preferences: what this search is for (the
    brief) and the title terms that find it. The hints are laid over the model's brief
    afterwards, so they hold even if the model drops one. Attribution comes from the run's
    `llm_context`."""
    value = await structured(
        "score",
        DerivedSearch,
        instructions=SEARCH_INSTRUCTIONS,
        prompt=derive_search_prompt(preferences, request, hints),
    )
    return finish_derived(value, preferences=preferences, request=request, hints=hints)


# --- title terms ---------------------------------------------------------------------------------

JS_FLAGS = re.IGNORECASE | re.ASCII


def word_pattern(phrase: str) -> str:
    """A word or phrase bounded by non-letters, written to mean the same in Postgres and
    JavaScript (and Python, case-insensitively)."""
    return f"(^|[^a-z]){phrase.replace(' ', '[^a-z]+')}([^a-z]|$)"


# Early-career title words. A title carrying one is never excluded from a junior search.
EARLY_CAREER_WORDS = (
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
)

SENIOR_WORDS = (
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
)


def guard_seniority(
    criteria: DerivedCriteria,
    seniority: str | None,
    roles: list[str] | None = None,
    *,
    companies: bool = False,
) -> DerivedCriteria:
    """The title terms a model proposed, made safe for a junior search.

    Model-written exclusions are literal phrases, and titles are not: "staff software engineer"
    does not catch "Staff+ Software Engineer". So for intern, new-grad and entry-level searches,
    senior words are excluded as whole words (unless the roles asked for contain one, as
    "associate product manager" contains "manager"), early-career exclusions are dropped, and a
    title with an early-career word survives any exclusion. A junior search limited to named
    companies also includes every early-career title those companies post.
    """
    if seniority not in ("intern", "new_grad", "entry"):
        return criteria

    early = re.compile("|".join(word_pattern(word) for word in EARLY_CAREER_WORDS), JS_FLAGS)
    wanted = " ".join(roles or []).lower()
    senior = [
        word_pattern(word)
        for word in SENIOR_WORDS
        if not re.search(word_pattern(word), wanted, JS_FLAGS)
    ]
    kept = [term for term in criteria.title_exclude if not early.search(term.replace("\\", ""))]
    early_titles = [word_pattern(word) for word in EARLY_CAREER_WORDS]

    include = criteria.title_include
    if companies:
        include = [*include, *(term for term in early_titles if term not in include)]
    return DerivedCriteria(
        title_include=include,
        title_exclude=[*kept, *(term for term in senior if term not in kept)],
        keywords=criteria.keywords,
        title_keep=early_titles,
    )


_LEVEL_WORDS = r"senior|sr\.?|staff|principal|junior|jr\.?|mid[- ]level|entry[- ]level"
_LEADING_LEVEL = re.compile(rf"^(?:(?:{_LEVEL_WORDS})[{JS_SPACE}]+)+", JS_FLAGS)


def without_level(phrase: str) -> str:
    """An include term without a leading level word: "senior backend engineer" becomes "backend
    engineer". Many companies never put "Senior" in a title. Not "lead": in "lead generation
    specialist" it is the job, not a level."""
    return js_trim(_LEADING_LEVEL.sub("", js_trim(phrase)))


_REGEX_SPECIAL = re.compile(r"[.*+?^${}()|[\]\\]")


def escape_for_regex(phrase: str) -> str:
    """The model returns plain phrases; they still have to be safe in a regex."""
    return _REGEX_SPECIAL.sub(lambda match: "\\" + match.group(0), js_trim(phrase).lower())


@dataclass(frozen=True, slots=True)
class TitlePatterns:
    include: str
    exclude: str | None
    keep: str | None


def build_title_patterns(criteria: DerivedCriteria) -> TitlePatterns | None:
    """The regex strings the query uses, built here so they can be tested without a database.
    Postgres `~*` and a case-insensitive JavaScript RegExp agree on plain alternations of escaped
    literals, which is all these ever are."""
    if not criteria.title_include:
        return None
    return TitlePatterns(
        include="|".join(criteria.title_include),
        exclude="|".join(criteria.title_exclude) if criteria.title_exclude else None,
        keep="|".join(criteria.title_keep) if criteria.title_keep else None,
    )


def title_matches(title: str, criteria: DerivedCriteria) -> bool:
    """Whether a title survives the filter. Mirrors the SQL; for tests and for explaining a
    decision."""
    patterns = build_title_patterns(criteria)
    if patterns is None:
        return True
    if not re.search(patterns.include, title, JS_FLAGS):
        return False
    if patterns.exclude is None:
        return True
    if patterns.keep is not None and re.search(patterns.keep, title, JS_FLAGS):
        return True
    return not re.search(patterns.exclude, title, JS_FLAGS)


def company_name_pattern(name: str) -> str | None:
    """ "Anthropic" matches "Anthropic" and "Anthropic PBC" but not "Anthropics Labs", and
    "Meta" does not match "Metabase". Postgres spells a word boundary `\\y`."""
    words = [word for word in re.split(r"[^a-z0-9]+", js_trim(name).lower()) if word]
    if not words:
        return None
    return f"^{'[^a-z0-9]+'.join(words)}\\y"


# Bay Area place names as whole words, for Postgres (`\y` is its word boundary).
BAY_AREA_PLACES = (
    "\\y("
    + "|".join(
        (
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
        )
    )
    + ")\\y"
)

_BAY_AREA_HINT = re.compile(r"bay area|san francisco|sf|silicon valley|peninsula", JS_FLAGS)
_NOT_A_CITY = re.compile(r"bay area|remote|anywhere", JS_FLAGS)

# What makes two postings "the same role" to a job seeker: the company and the title,
# normalized, regardless of location or source. Coarser than the ingest dedupe key on purpose.
ROLE_KEY = (
    "lower(regexp_replace({t}.company_name, '[^a-zA-Z0-9]+', '', 'g')) || '|' || "
    "btrim(lower(regexp_replace({t}.title, '[^a-zA-Z0-9+#]+', ' ', 'g')))"
)


# --- the query -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompanyFilter:
    ids: list[uuid.UUID] = field(default_factory=list)  # registered companies (`company_id`)
    # Names, matched as whole words at the start of `company_name` (aggregator postings from
    # companies we do not track).
    names: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PrefilterResult:
    job_ids: list[str]
    # What the filter had to work with and what it chose. Deliberately only these numbers: a
    # per-stage funnel would mean a query per stage.
    active_in_mirror: int
    selected: int
    capped_at: int | None  # says so when the cap, rather than the criteria, decided

    def funnel(self) -> dict[str, Any]:
        return {
            "activeInMirror": self.active_in_mirror,
            "selected": self.selected,
            "cappedAt": self.capped_at,
        }


def _company_clause(companies: CompanyFilter | None, params: list[Any]) -> str | None:
    if companies is None or (not companies.ids and not companies.names):
        return None
    clauses: list[str] = []
    if companies.ids:
        clauses.append("j.company_id = ANY(%s::uuid[])")
        params.append([str(company_id) for company_id in companies.ids])
    for name in companies.names:
        pattern = company_name_pattern(name)
        if pattern:
            clauses.append("j.company_name ~* %s")
            params.append(pattern)
    return f"({' OR '.join(clauses)})" if clauses else None


def _location_clause(preferences: Preferences, params: list[Any]) -> str | None:
    """Location and remote preference as one clause. Remote and location are one question:
    someone who will take remote work wants remote roles *and* roles in their metro."""
    wants_remote = preferences.remote_pref in ("remote", "any")
    locations = preferences.locations or []
    metros = sorted({"bay-area" for location in locations if _BAY_AREA_HINT.search(location)})

    clauses: list[str] = []
    if metros:
        clauses.append("j.location_norm ->> 'metro' = ANY(%s::text[])")
        params.append(metros)
    # The normalizer places one location per posting, and boards write several in one string
    # ("Seattle, San Francisco, New York"), which it leaves without a metro. The raw text is
    # checked against the metro's places too.
    if "bay-area" in metros:
        clauses.append("j.locations::text ~* %s")
        params.append(BAY_AREA_PLACES)
    # Free-text fallback for locations outside the gazetteer.
    for location in locations:
        city = js_trim(location.split(",")[0])
        if len(city) > 2 and not _NOT_A_CITY.search(city):
            clauses.append("j.locations::text ILIKE %s")
            params.append(f"%{city}%")
    if wants_remote:
        clauses.append("j.remote = 'remote'")
    if preferences.remote_pref == "hybrid":
        clauses.append("j.remote = 'hybrid'")
    return f"({' OR '.join(clauses)})" if clauses else None


def _title_clause(criteria: DerivedCriteria, params: list[Any]) -> str | None:
    """A title must match an include term and no exclude term, except that a title matching a
    keep term survives the excludes."""
    patterns = build_title_patterns(criteria)
    if patterns is None:
        return None
    params.append(patterns.include)
    if patterns.exclude is None:
        return "j.title ~* %s"
    params.append(patterns.exclude)
    if patterns.keep is None:
        return "(j.title ~* %s AND j.title !~* %s)"
    params.append(patterns.keep)
    return "(j.title ~* %s AND (j.title !~* %s OR j.title ~* %s))"


def prefilter_jobs(
    user_id: str,
    preferences: Preferences,
    criteria: DerivedCriteria,
    companies: CompanyFilter | None = None,
    limit: int = DEFAULT_LIMIT,
) -> PrefilterResult:
    """Selects the postings worth scoring for one user. Sync (run it through `db()`).

    `preferences` are the effective ones (saved, with this search's brief applied). Every clause
    is over a normalized column rather than free text, which is what makes it cheap enough to run
    over the whole mirror.
    """
    params: list[Any] = []
    conditions = ["j.is_active"]

    company = _company_clause(companies, params)
    if company is not None:
        conditions.append(company)
    else:
        # The age cutoff keeps a broad search to fresh postings. It is skipped when the user
        # names companies: what those boards list is open now. A posting with no date is kept:
        # careers-page extractions have none.
        conditions.append(
            f"(j.posted_at IS NULL OR j.posted_at >= now() - interval '{MAX_AGE_DAYS} days')"
        )

    location = _location_clause(preferences, params)
    if location is not None:
        conditions.append(location)

    title = _title_clause(criteria, params)
    if title is not None:
        conditions.append(title)

    # One role, one match. A company posting the same role in four cities is one thing to the
    # user. Roles the user already has a match for are skipped (dismissed ones stay dismissed),
    # except the very posting they matched, which may be re-selected harmlessly.
    conditions.append(
        "j.id NOT IN (SELECT dm.job_id FROM job_matches dm "
        "WHERE dm.user_id = %s AND dm.stage IN ('rejected', 'archived'))"
    )
    params.append(user_id)
    conditions.append(
        f"({ROLE_KEY.format(t='j')} NOT IN (SELECT {ROLE_KEY.format(t='kj')} FROM job_matches km "
        "JOIN jobs kj ON kj.id = km.job_id WHERE km.user_id = %s) "
        "OR j.id IN (SELECT km2.job_id FROM job_matches km2 WHERE km2.user_id = %s))"
    )
    params.extend([user_id, user_id])

    # Within the survivors, keep the best copy of each role: the company's own board over the
    # aggregator, then the most recent.
    sql = (
        "SELECT deduped.id FROM ("
        f"SELECT DISTINCT ON ({ROLE_KEY.format(t='j')}) j.id, j.posted_at FROM jobs j "
        f"WHERE {' AND '.join(conditions)} "
        f"ORDER BY {ROLE_KEY.format(t='j')}, CASE WHEN j.source = 'jsearch' THEN 1 ELSE 0 END, "
        "j.posted_at DESC NULLS LAST"
        ") deduped ORDER BY deduped.posted_at DESC NULLS LAST LIMIT %s"
    )
    params.append(limit)

    with transaction.atomic(), connection.cursor() as cursor:
        # Model-written regexes run here; a pathological one must not hold a connection.
        cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        cursor.execute(sql, params)
        job_ids = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT count(*) FROM jobs WHERE is_active")
        row = cursor.fetchone()
    active = int(row[0]) if row else 0
    return PrefilterResult(
        job_ids=job_ids,
        active_in_mirror=active,
        selected=len(job_ids),
        capped_at=limit if len(job_ids) == limit else None,
    )
