"""The cheap scoring pass (design §9.4; `map/llm.md` §12; a port of
`src/lib/ranking/fast-score.ts`).

It runs over every posting the prefilter kept, hundreds per run, so the unit cost is what
matters. Three things keep it down: a small output schema, a capped description, and a prompt
whose expensive half (the rubric and the candidate) is identical across every call in a run, so
providers that cache prompt prefixes charge for it once. Its job is triage, not judgement:
anything needing real reasoning about a posting belongs in the deep evaluation.

Changes from TypeScript (§9.4 "Fixed"): the score is always stored, even on a match the user
already moved (only the archive is gated on `found`), so it is not re-scored and re-paid every
run; the auto-archive note is appended to the user's notes. A budget refusal is not a per-posting
failure: it ends the pass, so the run fails with the budget's sentence.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from apps.accounts.schemas import Preferences
from apps.core.db import db
from apps.core.jsonio import js_json, js_locale, js_num
from apps.jobs.models import Job
from apps.llm.budget import BudgetExceeded
from apps.llm.structured import structured
from apps.matching.prefilter import js_value
from apps.matching.queries import save_fast_score, unscored_job_ids
from apps.matching.schemas import FastScore
from apps.resumes.schema import Resume

__all__ = [
    "BATCH",
    "CONCURRENCY",
    "DEFAULT_SCORE_THRESHOLD",
    "MAX_DESCRIPTION_CHARS",
    "RUBRIC",
    "ScorePosting",
    "ScoreTotals",
    "candidate_block",
    "load_postings",
    "posting_prompt",
    "score_instructions",
    "score_matches",
]

log = logging.getLogger("foray.matching")

# How much of a posting the cheap pass reads: the deep review's cap, which shows 99.5% of
# engineering postings whole. At 4,000 the requirements were often what got cut (#34).
MAX_DESCRIPTION_CHARS = 12_000
# Below this, a match is archived rather than shown.
DEFAULT_SCORE_THRESHOLD = 55
# Concurrent scoring calls. Each takes about two seconds regardless, so this is what decides how
# long a run feels (16 postings: 6.1 s at 8-way, 3.0 s at 16-way). The per-provider limiter is
# the real ceiling above it.
CONCURRENCY = 16
# Batched so a Stop is noticed within seconds rather than at the end.
BATCH = 40

RUBRIC = """You triage job postings for one candidate. Score 0–100.

The scale, which is not a feeling:
- 85–100: squarely what they asked for. Right level, right domain, right
  location, and their background is a direct match.
- 70–84: a good fit with a gap they could close, or a slightly different domain.
- 55–69: plausible. Adjacent domain, or the level is a little off, or the
  posting is vague enough that it might be right.
- 30–54: probably wrong — wrong level, wrong discipline, or a hard requirement
  they do not meet.
- 0–29: clearly not this person.

Rules:
- Judge against what the posting actually says. If it does not state something,
  that is not a miss — it is unknown.
- hard_requirement_misses is only for requirements the posting states and the
  candidate demonstrably does not meet. A "nice to have" is never a miss, and
  neither is a technology they have not listed.
- seniority is the posting's level, not the candidate's.
- fit_reasons: at most three, short, concrete. No restating the job title.
- Be decisive. A middling score for everything is useless triage."""


def candidate_block(preferences: Preferences, focus: str | None, resume: Resume | None) -> str:
    """The half of the prompt that never changes within a run."""
    work = resume.work if resume is not None else []
    experience = "\n".join(
        f"- {role.position} at {role.company} "
        f"({role.start_date if role.start_date is not None else '?'}–"
        f"{role.end_date if role.end_date is not None else 'present'})"
        + ("\n  " + "\n  ".join(role.highlights[:3]) if role.highlights else "")
        for role in work[:6]
    )
    education = "\n".join(
        f"- {item.study_type or ''} {item.area or ''} at {item.institution} "
        f"({item.end_date if item.end_date is not None else '?'})"
        for item in (resume.education if resume is not None else [])
    )
    skills = ", ".join(skill.name for skill in (resume.skills if resume is not None else []))
    this_search = f"\nThis search: {focus}" if focus else ""
    return (
        "<candidate>\n"
        f"Wants: {js_json(js_value(preferences.dump()))}{this_search}\n\n"
        "Experience:\n"
        f"{experience or '(none on file)'}\n\n"
        "Education:\n"
        f"{education or '(none on file)'}\n\n"
        f"Skills: {skills or '(none on file)'}\n"
        "</candidate>"
    )


def score_instructions(preferences: Preferences, focus: str | None, resume: Resume | None) -> str:
    # Rubric and candidate first, posting last: the stable prefix is what a provider's prompt
    # cache can reuse across the run.
    return f"{RUBRIC}\n\n{candidate_block(preferences, focus, resume)}"


@dataclass(frozen=True, slots=True)
class ScorePosting:
    id: str
    title: str
    company: str
    locations: list[str]
    remote: str
    salary_min: float | None
    salary_max: float | None
    description: str | None


def _pay(posting: ScorePosting) -> str:
    # Printed whenever the minimum is known; a missing maximum prints the minimum alone (TS
    # printed `150000–null`, §8.10's declared difference).
    if posting.salary_min is None:
        return "unstated"
    if posting.salary_max is None:
        return js_num(posting.salary_min)
    return f"{js_num(posting.salary_min)}–{js_num(posting.salary_max)}"


def posting_prompt(posting: ScorePosting) -> str:
    location = " | ".join(posting.locations) or "unstated"
    description = (
        posting.description[:MAX_DESCRIPTION_CHARS]
        if posting.description is not None
        else "(no description)"
    )
    # Said as the deep review says it, so a requirement past the cut reads as unseen rather than
    # as one the posting doesn't make.
    cut = (
        f"\n[Description cut at {js_locale(MAX_DESCRIPTION_CHARS)} characters.]"
        if posting.description is not None and len(posting.description) > MAX_DESCRIPTION_CHARS
        else ""
    )
    return (
        "<posting>\n"
        f"Title: {posting.title}\n"
        f"Company: {posting.company}\n"
        f"Location: {location} ({posting.remote})\n"
        f"Pay: {_pay(posting)}\n\n"
        f"{description}{cut}\n"
        "</posting>"
    )


def load_postings(job_ids: Sequence[str]) -> list[ScorePosting]:
    """The postings to score, in the order given. Sync."""
    rows = {
        job.id: job
        for job in Job.objects.filter(id__in=list(job_ids)).only(
            "id",
            "title",
            "company_name",
            "locations",
            "remote",
            "salary_min",
            "salary_max",
            "description_text",
        )
    }
    return [
        ScorePosting(
            id=job.id,
            title=job.title,
            company=job.company_name,
            locations=list(job.locations),
            remote=job.remote,
            salary_min=job.salary_min,
            salary_max=job.salary_max,
            description=job.description_text,
        )
        for job_id in job_ids
        if (job := rows.get(job_id)) is not None
    ]


@dataclass(slots=True)
class ScoreTotals:
    scored: int = 0
    archived: int = 0
    failed: int = 0


type StopCheck = Callable[[], Awaitable[bool]]
type Progress = Callable[[int, int], Awaitable[None]]


async def score_matches(
    user_id: str,
    *,
    preferences: Preferences,
    focus: str | None,
    resume: Resume | None,
    job_ids: Sequence[str],
    threshold: int = DEFAULT_SCORE_THRESHOLD,
    should_stop: StopCheck | None = None,
    on_progress: Progress | None = None,
) -> ScoreTotals:
    """Scores the user's unscored matches among `job_ids`, 16 at a time in batches of 40.

    `preferences` are the search's effective preferences (its brief applied) and `focus` the
    rest of what it asked for. A posting that could not be scored stays unscored and visible
    rather than being silently dropped. Attribution comes from the run's `llm_context`.
    """
    to_score = await db(unscored_job_ids, user_id, job_ids)
    totals = ScoreTotals()
    if not to_score:
        return totals

    instructions = score_instructions(preferences, focus, resume)
    gate = asyncio.Semaphore(CONCURRENCY)
    refused: list[BudgetExceeded] = []
    done = 0

    async def score_one(posting: ScorePosting) -> None:
        async with gate:
            if refused:
                return  # the month's budget is spent: no more calls
            try:
                value = await structured(
                    "score", FastScore, instructions=instructions, prompt=posting_prompt(posting)
                )
                saved, archived = await db(
                    save_fast_score, user_id, posting.id, value.dump(), threshold=threshold
                )
            except BudgetExceeded as error:
                refused.append(error)
                return
            except Exception as error:
                log.warning("could not score %s: %s", posting.id, error)
                totals.failed += 1
                return
            if saved:
                totals.scored += 1
                totals.archived += int(archived)
            else:
                totals.failed += 1  # the match was removed meanwhile

    for offset in range(0, len(to_score), BATCH):
        if should_stop is not None and await should_stop():
            break
        batch = to_score[offset : offset + BATCH]
        postings = await db(load_postings, batch)
        async with asyncio.TaskGroup() as group:
            for posting in postings:
                group.create_task(score_one(posting))
        if refused:
            raise refused[0]
        done += len(batch)
        if on_progress is not None:
            await on_progress(done, len(to_score))
    return totals
