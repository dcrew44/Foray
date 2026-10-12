"""The strong-tier pass: one posting, read in full, against the whole resume (design §9.4;
`map/llm.md` §13; a port of `src/lib/ranking/deep-eval.ts`).

Where the fast score triages hundreds of postings on a truncated description, this reads the top
few properly and has to show its working: every matched requirement names the resume items it
rests on, by id, so a claim can be traced to the bullet that says so, and tailoring later reuses
the evidence. It costs a few cents a posting on a strong model, which is why a search asks
before running it, and why the approval quotes a price from the prompt this module builds.

Prompt layout: the rubric and the candidate (`eval_instructions`) are identical for every
posting in a batch, so a provider's prompt cache can reuse them; the posting is the prompt.
Numbers in prompts go through the JavaScript formatters (§8.10).
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from apps.accounts.models import User
from apps.accounts.profile import describe_preferences
from apps.accounts.schemas import Preferences
from apps.core.db import db
from apps.core.errors import UserFacingError
from apps.core.jsonio import js_locale
from apps.llm import pricing, registry
from apps.llm.structured import structured
from apps.matching.brief import EMPTY_BRIEF, SearchBrief, apply_brief
from apps.matching.models import JobMatch
from apps.matching.queries import as_uuid, save_deep_eval
from apps.matching.schemas import DeepEval
from apps.resumes.queries import latest_base_resume
from apps.resumes.schema import fact_ids
from apps.resumes.text import JS_SPACE, js_trim, resume_to_text

__all__ = [
    "CALL_TIMEOUT_S",
    "EVAL_OUTPUT_TOKENS",
    "MAX_DESCRIPTION_CHARS",
    "RUBRIC",
    "TYPICAL_EVAL_INPUT_CHARS",
    "CostEstimate",
    "EvalCandidate",
    "EvalPosting",
    "MatchEvaluation",
    "NoResumeError",
    "cost_phrase",
    "estimate_eval_cost",
    "estimate_eval_cost_for",
    "eval_instructions",
    "eval_prompt",
    "evaluate_match",
    "evaluate_model_ref",
    "format_pay",
    "format_posted",
    "load_eval_candidate",
    "load_eval_posting",
    "run_deep_eval",
    "sanitize_deep_eval",
    "strip_fact_ids",
]

# How much of a posting the deep pass reads: effectively all of it.
MAX_DESCRIPTION_CHARS = 12_000
# A ceiling on one call. Evaluations take tens of seconds; one that hangs must fail on its own
# rather than hold the whole batch open.
CALL_TIMEOUT_S = 240

RUBRIC = """You evaluate one job posting for one candidate, in depth. Your verdict decides whether they spend an application on it, so be specific, honest and decisive.

Score 0–100 on this scale, which is not a feeling:
- 85–100 (strong): squarely what they asked for, and the resume shows direct
  evidence for the posting's core requirements.
- 70–84 (apply): a real fit. Most core requirements are met with evidence; the
  gaps are learnable or minor.
- 50–69 (maybe): plausible but uncertain — an adjacent domain, a level that is
  somewhat off, or a posting too vague to judge.
- 0–49 (skip): not worth an application — a disqualifying gap, the wrong level
  or discipline, or one of their dealbreakers.
The recommendation is the band's label and always agrees with the score.

Requirements and evidence:
- matched_requirements: requirements the posting actually states (required or
  preferred), each with the resume evidence that meets it. Quote or closely
  paraphrase the resume; never credit experience the resume does not show.
  fact_ids are the bracketed ids of the resume items the evidence comes from,
  copied exactly — only ids that appear in the resume. strength is "strong"
  when the evidence meets the requirement outright, "partial" when it is
  adjacent or smaller in scale.
- gaps: stated requirements the resume does not show. "disqualifying" only for
  a hard requirement they clearly fail (a degree, a clearance, work
  authorisation, years of experience far beyond theirs) or a conflict with one
  of their dealbreakers; everything else is "learnable". note says why, in one
  sentence.
- If the posting does not state something, it is unknown, not a miss. A
  technology absent from the resume is a gap only if the posting requires it.
- Any disqualifying gap means skip — or maybe, if you are unsure it is truly
  required.

Fit:
- seniority_fit: the candidate against the posting's level. "under" when it
  asks for more experience than they have, "over" when they are overqualified,
  "unclear" when the posting does not say.
- location_fit: the posting's location and remote policy against theirs.
  "partial" for, say, hybrid when they want remote, or a nearby metro.
  "unclear" when the posting does not say.
- salary_fit: the posting's pay against their salary floor — "above",
  "within" (the floor falls inside the range) or "below". "unstated" when the
  posting gives no pay or they gave no floor.

Writing — the candidate reads this themselves, on the job's page:
- Write summary, gap notes, risk_flags and talking_points in the second
  person, speaking to the candidate: "You have just over five years of
  backend work…", "Lead with your ingestion-service rebuild…". Never refer to
  them by name or as "the candidate", "they" or "he/she". evidence is the
  exception: keep it a close quote or paraphrase of the resume.
- summary: two or three plain sentences on why this role fits them or does
  not. Specific; no generic praise, no restating the title.
- risk_flags: concrete things worth knowing before applying (a relocation
  requirement, contract-to-hire, heavy on-call, a posting open for months).
  Empty when there are none.
- talking_points: three to five things to emphasise when applying, each tied
  to a specific resume item and to what this posting asks for.
- Resume ids belong in fact_ids and nowhere else. Every other field is read by
  a person: never write an id, or a bracketed reference to one, in summary,
  requirement, evidence, gap, note, risk_flags or talking_points."""  # noqa: E501 (prompt text, pinned by the §8.10 goldens)


# --- the prompt ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvalCandidate:
    """Who is being evaluated. Loaded once per batch: it is the same for every posting."""

    resume_id: uuid.UUID | str
    resume_version: int
    resume_text: str  # `resume_to_text` output: items carry their `[id]`
    preferences: Preferences  # the effective ones: the search's brief applied
    focus: str | None  # what the search that asked for this review is after
    fact_ids: tuple[str, ...]  # every fact id in the resume; citations outside it are dropped


@dataclass(frozen=True, slots=True)
class EvalPosting:
    title: str
    company: str
    locations: list[str]
    remote: str
    employment_type: str | None
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_period: str | None
    posted_at: datetime | None
    description: str | None


def eval_instructions(candidate: EvalCandidate) -> str:
    """The half of the prompt that is identical for every posting in a batch."""
    focus = f"\n- this search: {candidate.focus}" if candidate.focus else ""
    return (
        f"{RUBRIC}\n\n<candidate>\nWhat they are looking for:\n"
        f"{describe_preferences(candidate.preferences)}{focus}\n\n"
        "Resume (each item's [id] is what fact_ids cite):\n"
        f"{candidate.resume_text}\n</candidate>"
    )


_WHITESPACE = re.compile(f"[{JS_SPACE}]+")


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def format_pay(posting: EvalPosting) -> str:
    low, high = _finite(posting.salary_min), _finite(posting.salary_max)
    if low is None and high is None:
        return "unstated"
    if low is not None and high is not None and low != high:
        pay = f"{js_locale(low)}–{js_locale(high)}"
    else:
        pay = js_locale(low if low is not None else high)  # type: ignore[arg-type]
    text = f"{pay} {posting.salary_currency or ''} per {posting.salary_period or 'unstated period'}"
    return _WHITESPACE.sub(" ", text)


def format_posted(posted_at: datetime | None, now: datetime) -> str:
    if posted_at is None:
        return "unknown"
    elapsed_ms = (now - posted_at) // timedelta(milliseconds=1)
    days = max(0, elapsed_ms // 86_400_000)
    return f"{_iso_date(posted_at)} ({days} {'day' if days == 1 else 'days'} ago)"


def _iso_date(at: datetime) -> str:
    return at.astimezone(UTC).date().isoformat()


def eval_prompt(posting: EvalPosting, now: datetime | None = None) -> str:
    """The per-posting half."""
    now = now or datetime.now(UTC)
    description = js_trim(posting.description) if posting.description is not None else ""
    truncated = len(description) > MAX_DESCRIPTION_CHARS
    body = description[:MAX_DESCRIPTION_CHARS] if description else "(no description)"
    cut = (
        f"\n[Description cut at {js_locale(MAX_DESCRIPTION_CHARS)} characters.]"
        if truncated
        else ""
    )
    location = " | ".join(posting.locations) or "unstated"
    return (
        "<posting>\n"
        f"Title: {posting.title}\n"
        f"Company: {posting.company}\n"
        f"Location: {location} ({posting.remote})\n"
        f"Employment type: {posting.employment_type or 'unstated'}\n"
        f"Pay: {format_pay(posting)}\n"
        f"Posted: {format_posted(posting.posted_at, now)}\n"
        f"Today: {_iso_date(now)}\n\n"
        f"{body}{cut}\n"
        "</posting>"
    )


# --- making an evaluation safe to store and show --------------------------------------------------

_S = f"[{JS_SPACE}]"
# One or more UUIDs in brackets, e.g. `[3d98…]` or `[3d98…, 71ac…]`.
BRACKETED_UUIDS = re.compile(
    rf"{_S}*\[{_S}*[0-9a-f]{{8}}-[0-9a-f-]{{27,}}(?:{_S}*[,;]{_S}*[0-9a-f]{{8}}-[0-9a-f-]{{27,}})*"
    rf"{_S}*\]",
    re.IGNORECASE | re.ASCII,
)
UUID_IN_TEXT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE | re.ASCII
)
_EDGE_BRACKETS = re.compile(r"^\[|\]$")
_DOUBLE_SPACES = re.compile(r" {2,}")


def _ids_in(text: str) -> list[str]:
    """The UUIDs a model wrote inside brackets in a piece of prose."""
    return [
        found for group in BRACKETED_UUIDS.findall(text) for found in UUID_IN_TEXT.findall(group)
    ]


def strip_fact_ids(text: str, valid_ids: Collection[str] = ()) -> str:
    """Removes bracketed ids from prose: any bracketed UUID, and any bracketed id the resume
    actually uses (resume ids are UUIDs in practice, but nothing guarantees it)."""
    out = BRACKETED_UUIDS.sub("", text)
    for fact_id in valid_ids:
        if f"[{fact_id}]" not in out:
            continue
        out = re.sub(rf"{_S}*\[{re.escape(fact_id)}\]", "", out)
    return js_trim(_DOUBLE_SPACES.sub(" ", out))


def sanitize_deep_eval(value: DeepEval, valid_ids: Collection[str]) -> DeepEval:
    """Makes an evaluation safe to store and show.

    Citations: invented or mangled ids are dropped (tailoring reads these ids later); brackets
    copied along with an id are forgiven. Prose: bracketed ids are stripped from every field a
    person reads, after any valid id found in a requirement's evidence has been moved into its
    `fact_ids`, so the citation survives the clean-up.
    """
    ids = set(valid_ids)

    def prose(text: str) -> str:
        return strip_fact_ids(text, valid_ids)

    def clean_list(items: list[str]) -> list[str]:
        return [cleaned for cleaned in (prose(item) for item in items) if cleaned]

    requirements = []
    for item in value.matched_requirements:
        cited: dict[str, None] = {}
        for raw in [*item.fact_ids, *_ids_in(item.evidence)]:
            fact_id = js_trim(_EDGE_BRACKETS.sub("", js_trim(raw)))
            if fact_id in ids:
                cited[fact_id] = None
        requirements.append(
            item.model_copy(
                update={
                    "requirement": prose(item.requirement),
                    "evidence": prose(item.evidence),
                    "fact_ids": list(cited),
                }
            )
        )
    return value.model_copy(
        update={
            "matched_requirements": requirements,
            "gaps": [
                gap.model_copy(update={"gap": prose(gap.gap), "note": prose(gap.note)})
                for gap in value.gaps
            ],
            "risk_flags": clean_list(value.risk_flags),
            "talking_points": clean_list(value.talking_points),
            "summary": prose(value.summary),
        }
    )


# --- running it ----------------------------------------------------------------------------------


class NoResumeError(UserFacingError):
    def __init__(self) -> None:
        super().__init__(
            "There is no resume on file to evaluate against. Upload one on the profile page first."
        )


async def run_deep_eval(
    candidate: EvalCandidate, posting: EvalPosting, *, now: datetime | None = None
) -> DeepEval:
    """One evaluation: the model call and the citation check, no database. Attribution comes
    from the caller's `llm_context`."""
    value = await structured(
        "evaluate",
        DeepEval,
        instructions=eval_instructions(candidate),
        prompt=eval_prompt(posting, now),
        timeout=CALL_TIMEOUT_S,
    )
    return sanitize_deep_eval(value, candidate.fact_ids)


def load_eval_candidate(user_id: str, brief: SearchBrief | None = None) -> EvalCandidate | None:
    """The candidate as the evaluation sees them: the current base resume and their stated
    preferences, with the search's brief applied (an entry-level role found by an entry-level
    search must not be marked down as too junior because the saved preference says senior).
    None when there is no usable resume: an evaluation without one could only guess. Sync."""
    base = latest_base_resume(user_id)
    if base is None:
        return None
    saved = User.objects.filter(pk=user_id).values_list("preferences", flat=True).first()
    brief = brief or EMPTY_BRIEF
    return EvalCandidate(
        resume_id=base.id,
        resume_version=base.version,
        resume_text=resume_to_text(base.resume),
        preferences=apply_brief(Preferences.read(saved), brief),
        focus=brief.focus,
        fact_ids=tuple(fact_ids(base.resume)),
    )


@dataclass(frozen=True, slots=True)
class MatchEvaluation:
    match_id: str
    job_id: str
    title: str
    company: str
    evaluation: DeepEval


@dataclass(frozen=True, slots=True)
class _LoadedPosting:
    job_id: str
    posting: EvalPosting


def load_eval_posting(user_id: str, match_id: str) -> _LoadedPosting | None:
    """The posting behind one of the user's matches. Sync."""
    parsed = as_uuid(match_id)
    if parsed is None:
        return None
    match = JobMatch.objects.for_user(user_id).select_related("job").filter(id=parsed).first()
    if match is None:
        return None
    job = match.job
    return _LoadedPosting(
        job_id=job.id,
        posting=EvalPosting(
            title=job.title,
            company=job.company_name,
            locations=list(job.locations),
            remote=job.remote,
            employment_type=job.employment_type,
            salary_min=job.salary_min,
            salary_max=job.salary_max,
            salary_currency=job.salary_currency,
            salary_period=job.salary_period,
            posted_at=job.posted_at,
            description=job.description_text,
        ),
    )


async def evaluate_match(
    user_id: str, match_id: str, candidate: EvalCandidate, *, now: datetime | None = None
) -> MatchEvaluation:
    """Evaluates one of the user's matches and stores the result on it (`deep_score`,
    `deep_eval`; a "skip" archives an untouched match). Raises when the match is not theirs or
    the model fails; the caller decides what a failure means for its run."""
    loaded = await db(load_eval_posting, user_id, match_id)
    if loaded is None:
        raise UserFacingError("That match does not exist.")
    evaluation = await run_deep_eval(candidate, loaded.posting, now=now)
    saved = await db(save_deep_eval, user_id, match_id, evaluation.dump())
    if not saved:
        raise UserFacingError("That match was removed during evaluation.")
    return MatchEvaluation(
        match_id=str(match_id),
        job_id=loaded.job_id,
        title=loaded.posting.title,
        company=loaded.posting.company,
        evaluation=evaluation,
    )


# --- what it costs -------------------------------------------------------------------------------

# Output tokens one evaluation is assumed to spend, reasoning included. Measured 2026-09-24 over
# eight evaluations on gpt-5.6-terra at medium effort: 1,370-2,126 output tokens, $0.019-0.030
# each. Output is most of the bill, so this sits above the average: a quote that comes in under
# is fine, one that comes in over is not.
EVAL_OUTPUT_TOKENS = 1_800
SCHEMA_OVERHEAD_TOKENS = 400  # the JSON schema and message framing a structured call adds
CHARS_PER_TOKEN = 4
POSTING_FRAME_CHARS = 400  # header lines and tags around a posting's description
# A prompt of typical size: the rubric, a one-page resume with preferences, a mid-length posting.
TYPICAL_EVAL_INPUT_CHARS = len(RUBRIC) + 6_500 + 7_000


@dataclass(frozen=True, slots=True)
class CostEstimate:
    usd: float | None  # for all `n` evaluations; None when the model has no price on file
    per_item_usd: float | None
    model: str  # `provider:model`, or "unconfigured" when no evaluate model is set


def _round_usd(value: float) -> float:
    """`Math.round(value * 10_000) / 10_000`: half up, toward +∞."""
    return math.floor(value * 10_000 + 0.5) / 10_000


def evaluate_model_ref() -> str | None:
    try:
        return registry.model_ref("evaluate")
    except Exception:
        return None


def estimate_eval_cost(
    n: int,
    *,
    input_chars: float | None = None,
    model_ref: str | None = None,
    at: datetime | None = None,
) -> CostEstimate:
    """The cost of `n` evaluations from the list price of the configured model. Provider prompt
    caching is ignored, so the estimate errs high on input, the small half of the bill."""
    ref = model_ref or evaluate_model_ref()
    if not ref:
        return CostEstimate(usd=None, per_item_usd=None, model="unconfigured")
    if pricing.rate_for(ref, at) is None:
        return CostEstimate(usd=None, per_item_usd=None, model=ref)
    count = max(0, math.floor(n))
    chars = TYPICAL_EVAL_INPUT_CHARS if input_chars is None else input_chars
    input_tokens = math.ceil(chars / CHARS_PER_TOKEN) + SCHEMA_OVERHEAD_TOKENS
    per_item = pricing.cost_usd(
        ref, input_tokens=input_tokens, output_tokens=EVAL_OUTPUT_TOKENS, at=at
    )
    return CostEstimate(
        usd=_round_usd(per_item * count), per_item_usd=_round_usd(per_item), model=ref
    )


def estimate_eval_cost_for(
    candidate: EvalCandidate,
    description_chars: Sequence[int],
    *,
    model_ref: str | None = None,
    at: datetime | None = None,
) -> CostEstimate:
    """The estimate for specific postings, from the prompt that would actually be sent: this
    candidate's instructions plus each description as the pass would cut it."""
    n = len(description_chars)
    if n == 0:
        return estimate_eval_cost(0, model_ref=model_ref, at=at)
    instructions = len(eval_instructions(candidate))
    total = sum(
        instructions + POSTING_FRAME_CHARS + min(chars, MAX_DESCRIPTION_CHARS)
        for chars in description_chars
    )
    return estimate_eval_cost(n, input_chars=total / n, model_ref=model_ref, at=at)


def cost_phrase(usd: float | None) -> str | None:
    """ "about $0.42", "under a cent", or None when there is no price."""
    if usd is None:
        return None
    if usd < 0.01:
        return "under a cent"
    return f"about ${_to_fixed_2(usd)}"


def _to_fixed_2(value: float) -> str:
    """`value.toFixed(2)`: the exact binary value rounded half up."""
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
