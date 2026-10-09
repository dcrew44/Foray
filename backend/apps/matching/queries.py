"""A user's matches: the jobs Foray found for them, where each sits on their board, and what the
two scoring passes concluded (design §9.4, §5.3; a port of `src/lib/db/queries/matches.ts`).

Every function takes `user_id` first and is sync (async code runs them through `db()`): a match
is the per-user half of a shared posting, and nothing here can read another user's. Reads go
through `JobMatch.objects.for_user`; the few writes that need `RETURNING` or a conditional
`CASE` are raw SQL with `user_id` in every `WHERE`.

`MatchRow` is what lists, boards, run cards and chat cards need about one match; it is also the
`GET /api/matches` row (camelCase on the wire).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from django.db import connection, transaction
from django.db.models import F, Q, QuerySet
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce, Length
from django.utils import timezone

from apps.core.router import ApiModel
from apps.jobs.models import Job
from apps.matching.models import JobMatch, Stage

__all__ = [
    "ALL_STAGES",
    "CLOSED_STAGES",
    "OPEN_STAGES",
    "DeepEvalCandidate",
    "MatchRow",
    "Recommendation",
    "append_match_note",
    "as_uuid",
    "count_by_stage",
    "count_new_since",
    "ensure_match",
    "get_match_by_job",
    "get_match_rows",
    "is_shortlist_candidate",
    "latest_matched_at",
    "list_matches",
    "list_shortlist_candidates",
    "matches_needing_evaluation",
    "move_matches_from_stage",
    "resolve_match",
    "resolve_or_create_match",
    "save_deep_eval",
    "save_fast_score",
    "score_bands_for_jobs",
    "select_for_deep_eval",
    "set_match_stage",
    "top_match_ids_for_jobs",
    "unscored_job_ids",
    "update_match",
    "upsert_matches",
    "valid_uuids",
]

OPEN_STAGES: tuple[str, ...] = ("found", "shortlisted", "applied", "interviewing", "offer")
CLOSED_STAGES: tuple[str, ...] = ("rejected", "archived")
ALL_STAGES: tuple[str, ...] = (*OPEN_STAGES, *CLOSED_STAGES)

Recommendation = Literal["strong", "apply", "maybe", "skip"]
RECOMMENDATIONS: tuple[str, ...] = ("strong", "apply", "maybe", "skip")
StageName = Literal[
    "found", "shortlisted", "applied", "interviewing", "offer", "rejected", "archived"
]
RemoteName = Literal["remote", "hybrid", "onsite", "unknown"]

AUTO_ARCHIVE_NOTE = "Auto-archived: scored {score}, below the {threshold} threshold."
SKIP_ARCHIVE_NOTE = "Archived: the deep review recommended skipping this one."
UPSERT_BATCH = 500


def as_uuid(value: Any) -> uuid.UUID | None:
    """An id from a caller as a UUID; None when malformed (callers answer "not found")."""
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def valid_uuids(ids: Iterable[Any]) -> list[str]:
    """The ids that are UUIDs, canonical and de-duplicated, in input order."""
    found: dict[str, None] = {}
    for value in ids:
        parsed = as_uuid(value)
        if parsed is not None:
            found[str(parsed)] = None
    return list(found)


# --- rows ----------------------------------------------------------------------------------------


class MatchRow(ApiModel):
    match_id: uuid.UUID
    job_id: str
    stage: StageName
    stage_changed_at: datetime
    fast_score: int | None
    deep_score: int | None
    score: int | None  # the deep score when there is one, else the fast score: what the UI ranks
    recommendation: Recommendation | None  # the deep evaluation's verdict, when one exists
    headline: str | None  # one line on why it fits: the deep summary, else the first fast reason
    title: str
    company: str
    locations: list[str]
    remote: RemoteName
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_period: str | None
    posted_at: datetime | None
    url: str
    apply_url: str | None
    is_active: bool
    tailored_resume_id: uuid.UUID | None
    user_rating: int | None
    created_at: datetime


def _rows(user_id: str) -> QuerySet[JobMatch]:
    return (
        JobMatch.objects.for_user(user_id)
        .select_related("job")
        .annotate(
            score=Coalesce("deep_score", "fast_score"),
            recommendation=KT("deep_eval__recommendation"),
            headline=Coalesce(KT("deep_eval__summary"), KT("fast_eval__fit_reasons__0")),
        )
    )


def _row(match: Any) -> MatchRow:
    job: Job = match.job
    recommendation = match.recommendation if match.recommendation in RECOMMENDATIONS else None
    return MatchRow(
        match_id=match.id,
        job_id=job.id,
        stage=match.stage,
        stage_changed_at=match.stage_changed_at,
        fast_score=match.fast_score,
        deep_score=match.deep_score,
        score=match.score,
        recommendation=recommendation,
        headline=match.headline,
        title=job.title,
        company=job.company_name,
        locations=list(job.locations),
        remote=job.remote,  # pyright: ignore[reportArgumentType] (a CHECKed choice)
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        salary_currency=job.salary_currency,
        salary_period=job.salary_period,
        posted_at=job.posted_at,
        url=job.url,
        apply_url=job.apply_url,
        is_active=job.is_active,
        tailored_resume_id=match.tailored_resume_id,
        user_rating=match.user_rating,
        created_at=match.created_at,
    )


_BEST_FIRST = (F("score").desc(nulls_last=True), F("job__posted_at").desc(nulls_last=True), "id")


def list_matches(
    user_id: str,
    *,
    stages: Sequence[str] | None = None,
    min_score: int | None = None,
    query: str | None = None,
    evaluated_only: bool = False,
    job_ids: Sequence[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[MatchRow]:
    """Matches with their job, best first (deep score, else fast score, then the newest)."""
    rows = _rows(user_id)
    if stages:
        rows = rows.filter(stage__in=list(stages))
    if min_score is not None:
        rows = rows.filter(score__gte=min_score)
    if evaluated_only:
        rows = rows.filter(deep_eval__isnull=False)
    if job_ids is not None:
        if not job_ids:
            return []
        rows = rows.filter(job_id__in=list(job_ids))
    text = (query or "").strip()
    if text:
        rows = rows.filter(Q(job__title__icontains=text) | Q(job__company_name__icontains=text))
    return [_row(match) for match in rows.order_by(*_BEST_FIRST)[offset : offset + limit]]


def get_match_rows(user_id: str, match_ids: Sequence[Any]) -> list[MatchRow]:
    """These matches (malformed and foreign ids ignored), best first."""
    ids = valid_uuids(match_ids)
    if not ids:
        return []
    rows = _rows(user_id).filter(id__in=ids).order_by(*_BEST_FIRST)
    return [_row(match) for match in rows]


def get_match_by_job(user_id: str, job_id: str) -> JobMatch | None:
    """The user's match for a posting, if Foray has matched them to it."""
    return JobMatch.objects.for_user(user_id).filter(job_id=job_id).first()


def resolve_match(user_id: str, match_or_job_id: str) -> JobMatch | None:
    """Accepts a match id or a job id: the chat agent sees both, and asking it to keep them
    apart is a failure mode rather than a feature."""
    match_id = as_uuid(match_or_job_id)
    if match_id is not None:
        found = JobMatch.objects.for_user(user_id).filter(id=match_id).first()
        if found is not None:
            return found
    return get_match_by_job(user_id, match_or_job_id)


def ensure_match(user_id: str, job_id: str) -> JobMatch:
    """The user's match for a posting, created in `found` if it does not exist (a posting the
    user found themselves rather than one a search surfaced)."""
    existing = get_match_by_job(user_id, job_id)
    if existing is not None:
        return existing
    upsert_matches(user_id, [job_id])
    found = get_match_by_job(user_id, job_id)
    assert found is not None
    return found


def resolve_or_create_match(user_id: str, match_or_job_id: str) -> JobMatch | None:
    """`resolve_match`, creating the match when the id is a posting the user has no match for
    yet. None when it is neither."""
    existing = resolve_match(user_id, match_or_job_id)
    if existing is not None:
        return existing
    if Job.objects.filter(id=match_or_job_id).exists():
        return ensure_match(user_id, match_or_job_id)
    return None


def count_by_stage(user_id: str) -> dict[str, int]:
    counts = dict.fromkeys(ALL_STAGES, 0)
    rows = JobMatch.objects.for_user(user_id).values_list("stage").order_by()
    for (stage,) in rows:
        counts[stage] = counts.get(stage, 0) + 1
    return counts


def count_new_since(user_id: str, since: datetime) -> dict[str, int]:
    """New open matches since a moment (the dashboard's "since you were last here")."""
    rows = (
        JobMatch.objects.for_user(user_id)
        .filter(created_at__gte=since, stage__in=OPEN_STAGES)
        .annotate(score=Coalesce("deep_score", "fast_score"))
    )
    return {"total": rows.count(), "strong": rows.filter(score__gte=80).count()}


def latest_matched_at(user_id: str) -> datetime | None:
    return (
        JobMatch.objects.for_user(user_id)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )


# --- writes from the UI and chat -----------------------------------------------------------------


def set_match_stage(user_id: str, match_id: Any, stage: str) -> bool:
    """Moves a match to a stage; False when the match is not this user's. Moving to the stage it
    is already in is a no-op that keeps its timestamp."""
    parsed = as_uuid(match_id)
    if parsed is None or stage not in Stage.values:
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE job_matches SET stage = %s, stage_changed_at = CASE WHEN stage = %s "
            "THEN stage_changed_at ELSE now() END, updated_at = now() "
            "WHERE user_id = %s AND id = %s",
            [stage, stage, user_id, str(parsed)],
        )
        return cursor.rowcount > 0


_UNSET: Any = object()


def update_match(
    user_id: str, match_id: Any, *, notes: Any = _UNSET, user_rating: Any = _UNSET
) -> bool:
    """Sets only the fields given. False when the match is not this user's."""
    parsed = as_uuid(match_id)
    if parsed is None:
        return False
    fields: dict[str, Any] = {"updated_at": timezone.now()}
    if notes is not _UNSET:
        fields["notes"] = notes
    if user_rating is not _UNSET:
        fields["user_rating"] = user_rating
    return JobMatch.objects.for_user(user_id).filter(id=parsed).update(**fields) > 0


def append_match_note(user_id: str, match_id: Any, note: str) -> bool:
    """Appends to a match's notes rather than replacing them: a note from chat must not erase
    what the user typed on the job page."""
    text = note.strip()
    parsed = as_uuid(match_id)
    if not text or parsed is None:
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE job_matches SET notes = CASE WHEN coalesce(notes, '') = '' THEN %s "
            "ELSE notes || E'\\n\\n' || %s END, updated_at = now() "
            "WHERE user_id = %s AND id = %s",
            [text, text, user_id, str(parsed)],
        )
        return cursor.rowcount > 0


# --- the search pipeline -------------------------------------------------------------------------


def upsert_matches(user_id: str, job_ids: Sequence[str]) -> int:
    """Records postings as `found` matches and returns how many were actually inserted.

    Existing matches are left exactly as they are: a job the user has already shortlisted or
    applied to must not be dragged back to `found` by a later search seeing it again. The count
    comes from `RETURNING`, so a concurrent insert is not counted twice (bug #44).
    """
    created = 0
    unique = list(dict.fromkeys(job_ids))
    for offset in range(0, len(unique), UPSERT_BATCH):
        batch = unique[offset : offset + UPSERT_BATCH]
        ids = [str(uuid.uuid7()) for _ in batch]
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO job_matches "
                "(id, user_id, job_id, stage, stage_changed_at, created_at, updated_at) "
                "SELECT new.id, %s, new.job_id, 'found', now(), now(), now() "
                "FROM unnest(%s::uuid[], %s::text[]) AS new(id, job_id) "
                "ON CONFLICT (user_id, job_id) DO NOTHING RETURNING id",
                [user_id, ids, batch],
            )
            created += len(cursor.fetchall())
    return created


def unscored_job_ids(user_id: str, job_ids: Sequence[str]) -> list[str]:
    """Of these postings, the ones whose match has no fast score yet, in input order: re-scoring
    an unchanged posting buys nothing and is the easiest way to double a run's bill."""
    if not job_ids:
        return []
    pending = set(
        JobMatch.objects.for_user(user_id)
        .filter(job_id__in=list(job_ids), fast_score__isnull=True)
        .values_list("job_id", flat=True)
    )
    return [job_id for job_id in dict.fromkeys(job_ids) if job_id in pending]


_SAVE_FAST = """
WITH old AS (
    SELECT id, stage FROM job_matches WHERE user_id = %(user)s AND job_id = %(job)s FOR UPDATE
)
UPDATE job_matches m SET
    fast_score = %(score)s,
    fast_eval = %(eval)s::jsonb,
    stage = CASE WHEN %(below)s AND old.stage = 'found' THEN 'archived' ELSE m.stage END,
    stage_changed_at = CASE WHEN %(below)s AND old.stage = 'found' THEN now()
                            ELSE m.stage_changed_at END,
    notes = CASE WHEN %(below)s AND old.stage = 'found'
                 THEN concat_ws(E'\\n\\n', nullif(m.notes, ''), %(note)s::text) ELSE m.notes END,
    updated_at = now()
FROM old WHERE m.id = old.id
RETURNING %(below)s AND old.stage = 'found'
"""


def save_fast_score(
    user_id: str, job_id: str, evaluation: dict[str, Any], *, threshold: int
) -> tuple[bool, bool]:
    """Stores a fast score on the user's match for a posting, and archives the match when the
    score is below the threshold **and** the match is still in `found`: a job the user already
    acted on is never dragged back to archived by a cheap model. The score is stored whatever
    the stage (bug #40), and the archive note is appended, not written over the user's notes
    (bug #39). Returns `(saved, archived)`."""
    score = int(evaluation["score"])
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            _SAVE_FAST,
            {
                "user": user_id,
                "job": job_id,
                "score": score,
                "eval": json.dumps(evaluation, ensure_ascii=False),
                "below": score < threshold,
                "note": AUTO_ARCHIVE_NOTE.format(score=score, threshold=threshold),
            },
        )
        row = cursor.fetchone()
    return (row is not None, bool(row and row[0]))


def save_deep_eval(user_id: str, match_id: Any, evaluation: dict[str, Any]) -> bool:
    """Stores a deep evaluation. False when the match is not this user's.

    A "skip" verdict archives the match, as a low triage score does, but only an untouched
    (`found`) match moves; the note is appended to the user's notes.
    """
    parsed = as_uuid(match_id)
    if parsed is None:
        return False
    skip = evaluation.get("recommendation") == "skip"
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE job_matches SET deep_score = %(score)s, deep_eval = %(eval)s::jsonb, "
            "stage = CASE WHEN %(skip)s AND stage = 'found' THEN 'archived' ELSE stage END, "
            "stage_changed_at = CASE WHEN %(skip)s AND stage = 'found' THEN now() "
            "ELSE stage_changed_at END, "
            "notes = CASE WHEN %(skip)s AND stage = 'found' "
            "THEN concat_ws(E'\\n\\n', nullif(notes, ''), %(note)s::text) ELSE notes END, "
            "updated_at = now() WHERE user_id = %(user)s AND id = %(id)s",
            {
                "score": int(evaluation["score"]),
                "eval": json.dumps(evaluation, ensure_ascii=False),
                "skip": skip,
                "note": SKIP_ARCHIVE_NOTE,
                "user": user_id,
                "id": str(parsed),
            },
        )
        return cursor.rowcount > 0


@dataclass(frozen=True, slots=True)
class DeepEvalCandidate:
    """A match the deep evaluation could run on, with what its cost estimate needs."""

    match_id: uuid.UUID
    job_id: str
    title: str
    company: str
    fast_score: int | None
    description_chars: int  # the cleaned description's length: the variable part of the prompt


def select_for_deep_eval(
    user_id: str, *, job_ids: Sequence[str], min_fast_score: int, limit: int
) -> list[DeepEvalCandidate]:
    """The matches among these postings (a search run's own candidates) worth the strong model's
    time: open (found or shortlisted), fast-scored at or above the threshold, never
    deep-evaluated, on a posting that is still live. Best fast score first."""
    if limit <= 0 or not job_ids:
        return []
    rows = (
        JobMatch.objects.for_user(user_id)
        .filter(
            job_id__in=list(job_ids),
            stage__in=("found", "shortlisted"),
            fast_score__gte=min_fast_score,
            deep_eval__isnull=True,
            job__is_active=True,
        )
        .annotate(description_chars=Coalesce(Length("job__description_text"), 0))
        .select_related("job")
        .order_by(F("fast_score").desc(), F("job__posted_at").desc(nulls_last=True), "id")[:limit]
    )
    return [
        DeepEvalCandidate(
            match_id=row.id,
            job_id=row.job_id,
            title=row.job.title,
            company=row.job.company_name,
            fast_score=row.fast_score,
            description_chars=int(row.description_chars),  # pyright: ignore[reportAttributeAccessIssue]
        )
        for row in rows
    ]


def matches_needing_evaluation(user_id: str, match_ids: Sequence[Any]) -> list[str]:
    """Of these matches, the ones an evaluation should still be paid for: this user's, not yet
    deep-evaluated, and not rejected or archived since they were picked. Input order kept."""
    ids = valid_uuids(match_ids)
    if not ids:
        return []
    keep = {
        str(found)
        for found in JobMatch.objects.for_user(user_id)
        .filter(id__in=ids, deep_eval__isnull=True, stage__in=OPEN_STAGES)
        .values_list("id", flat=True)
    }
    return [match_id for match_id in ids if match_id in keep]


def is_shortlist_candidate(row: MatchRow) -> bool:
    """The shortlist rule: deep-evaluated as worth applying to, and still sitting in `found`; a
    match the user has already moved is theirs, not ours."""
    return row.stage == "found" and row.recommendation in ("strong", "apply")


def list_shortlist_candidates(user_id: str, match_ids: Sequence[Any]) -> list[MatchRow]:
    return [row for row in get_match_rows(user_id, match_ids) if is_shortlist_candidate(row)]


def move_matches_from_stage(
    user_id: str, match_ids: Sequence[Any], from_stage: str, to_stage: str
) -> list[str]:
    """Moves matches to `to_stage`, but only those still in `from_stage`, in one statement, so a
    match the user moved in the meantime stays where they put it. Returns the ids that moved, in
    input order."""
    ids = valid_uuids(match_ids)
    if not ids:
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE job_matches SET stage = %s, stage_changed_at = now(), updated_at = now() "
            "WHERE user_id = %s AND id = ANY(%s::uuid[]) AND stage = %s RETURNING id",
            [to_stage, user_id, ids, from_stage],
        )
        moved = {str(row[0]) for row in cursor.fetchall()}
    return [match_id for match_id in ids if match_id in moved]


def top_match_ids_for_jobs(user_id: str, job_ids: Sequence[str], limit: int = 5) -> list[str]:
    """The best open matches among a set of postings, deep score preferred: a search run's
    headline results."""
    if not job_ids or limit <= 0:
        return []
    rows = (
        JobMatch.objects.for_user(user_id)
        .filter(job_id__in=list(job_ids), stage__in=OPEN_STAGES)
        .annotate(score=Coalesce("deep_score", "fast_score"))
        .filter(score__isnull=False)
        .order_by(F("score").desc(), F("updated_at").desc(), "id")
        .values_list("id", flat=True)[:limit]
    )
    return [str(match_id) for match_id in rows]


def score_bands_for_jobs(user_id: str, job_ids: Sequence[str]) -> tuple[int, int]:
    """How a set of postings scored, for a run's summary: `(strong, good)` among the open
    matches, where strong is a score of 80 or more and good 65 to 79."""
    if not job_ids:
        return 0, 0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FILTER (WHERE coalesce(deep_score, fast_score) >= 80), "
            "count(*) FILTER (WHERE coalesce(deep_score, fast_score) >= 65 "
            "AND coalesce(deep_score, fast_score) < 80) "
            "FROM job_matches WHERE user_id = %s AND job_id = ANY(%s::text[]) "
            "AND stage = ANY(%s::text[])",
            [user_id, list(job_ids), list(OPEN_STAGES)],
        )
        row = cursor.fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)
