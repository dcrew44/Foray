"""Whether a score is right (#2).

`llm_bakeoff` says what a model costs, how long it takes and how far it drifts from another
model. None of that says whether either of them is right. This measures a model against labels:
forty real postings, each read against one fixed candidate (the sample candidate every bake-off
uses, a mid-level backend engineer in Oakland) and called `strong`, `apply`, `maybe` or `skip`
by the product's own rubric.

- `labels.json` (`tests/fixtures/match-quality/`) holds the labels with each reader's sentence.
  The postings' text is other people's writing and stays out of the repository: it is read from
  `notes/benchmarks/match-quality/postings.jsonl` (the private notes clone), or from the mirror
  by id for postings still in it.
- `agreement` is the arithmetic: how often a model's band is the label's, how often it is at most
  one band away, and the two shares the product acts on: of the postings worth applying to, how
  many the fast score would keep, and of the ones to skip, how many it would archive.

The labels are a reading, not the truth. Two readers made them independently and a third settled
their disagreements; `LABELS.md` beside them says who and how, and how often the readers
agreed. They are worth replacing with the owner's own whenever the owner has an afternoon.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from django.conf import settings

from apps.jobs.models import Job
from apps.matching.deep_eval import EvalPosting
from apps.matching.fast_score import ScorePosting

__all__ = [
    "BANDS",
    "KEEP_AT",
    "LABELS_PATH",
    "Agreement",
    "Labelled",
    "agreement",
    "band_of",
    "eval_sample",
    "load",
    "score_sample",
]

FIXTURES = Path(settings.BASE_DIR) / "tests" / "fixtures" / "match-quality"
LABELS_PATH = FIXTURES / "labels.json"
NOTES_POSTINGS = (
    Path(settings.BASE_DIR).parent / "notes" / "benchmarks" / "match-quality" / "postings.jsonl"
)
# Worst to best. The deep review's bands (`matching.deep_eval.RUBRIC`), which are also what a
# label means.
BANDS = ("skip", "maybe", "apply", "strong")
# The default `score_threshold`: a fast score under it archives the match.
KEEP_AT = 55


def band_of(score: int) -> str:
    """A 0-100 score as the rubric's band."""
    return (
        "strong" if score >= 85 else "apply" if score >= 70 else "maybe" if score >= 50 else "skip"
    )


@dataclass(frozen=True, slots=True)
class Labelled:
    id: str
    label: str
    posting: dict[str, Any]  # title, company, locations, remote, salary fields, description


def _mirror(ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for job in Job.objects.filter(id__in=list(ids)):
        found[job.id] = {
            "id": job.id,
            "title": job.title,
            "company": job.company_name,
            "locations": list(job.locations),
            "remote": job.remote,
            "employment_type": job.employment_type,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "salary_currency": job.salary_currency,
            "salary_period": job.salary_period,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
            "description": job.description_text,
        }
    return found


def load(postings: Path | None = None, labels: Path | None = None) -> list[Labelled]:
    """The labelled postings with their text, in the labels' order. The text comes from
    `postings` (a JSONL file) when given, else the notes clone, else the mirror; a labelled
    posting whose text is nowhere to be found is left out. Sync."""
    wanted = json.loads((labels or LABELS_PATH).read_text())
    source = postings or (NOTES_POSTINGS if NOTES_POSTINGS.exists() else None)
    texts: dict[str, dict[str, Any]] = {}
    if source is not None:
        for line in source.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                texts[row["id"]] = row
    missing = [item["id"] for item in wanted if item["id"] not in texts]
    if missing:
        texts.update(_mirror(missing))
    return [
        Labelled(item["id"], item["label"], texts[item["id"]])
        for item in wanted
        if item["id"] in texts and item["label"] in BANDS
    ]


def score_sample(labelled: Sequence[Labelled]) -> list[ScorePosting]:
    return [
        ScorePosting(
            id=item.id,
            title=item.posting["title"],
            company=item.posting["company"],
            locations=list(item.posting.get("locations") or []),
            remote=item.posting.get("remote") or "unknown",
            salary_min=item.posting.get("salary_min"),
            salary_max=item.posting.get("salary_max"),
            description=item.posting.get("description"),
        )
        for item in labelled
    ]


def eval_sample(labelled: Sequence[Labelled]) -> list[tuple[str, EvalPosting]]:
    sample: list[tuple[str, EvalPosting]] = []
    for item in labelled:
        posting = item.posting
        posted = posting.get("posted_at")
        sample.append(
            (
                item.id,
                EvalPosting(
                    title=posting["title"],
                    company=posting["company"],
                    locations=list(posting.get("locations") or []),
                    remote=posting.get("remote") or "unknown",
                    employment_type=posting.get("employment_type"),
                    salary_min=posting.get("salary_min"),
                    salary_max=posting.get("salary_max"),
                    salary_currency=posting.get("salary_currency"),
                    salary_period=posting.get("salary_period"),
                    posted_at=datetime.fromisoformat(posted) if posted else None,
                    description=posting.get("description"),
                ),
            )
        )
    return sample


@dataclass(frozen=True, slots=True)
class Agreement:
    judged: int  # labelled postings the model gave an answer for
    exact: int  # the model's band is the label
    within_one: int  # at most one band away
    # Of the postings labelled apply or strong: how many the model put at or above KEEP_AT.
    worth: int
    kept: int
    # Of the postings labelled skip: how many it put under KEEP_AT.
    skips: int
    archived: int
    confusion: dict[str, dict[str, int]]  # label -> the model's band -> count

    def share(self, part: int, whole: int) -> str:
        return f"{part}/{whole}" if whole else "none"


def agreement(labels: dict[str, str], scores: dict[str, int]) -> Agreement:
    """A model's scores against the labels (`{posting id: label}`, `{posting id: 0-100}`)."""
    confusion = {label: Counter[str]() for label in BANDS}
    exact = within = worth = kept = skips = archived = 0
    for job_id, score in scores.items():
        label = labels.get(job_id)
        if label not in BANDS:
            continue
        band = band_of(score)
        confusion[label][band] += 1
        distance = abs(BANDS.index(band) - BANDS.index(label))
        exact += distance == 0
        within += distance <= 1
        if label in ("apply", "strong"):
            worth += 1
            kept += score >= KEEP_AT
        if label == "skip":
            skips += 1
            archived += score < KEEP_AT
    judged = sum(sum(row.values()) for row in confusion.values())
    return Agreement(
        judged=judged,
        exact=exact,
        within_one=within,
        worth=worth,
        kept=kept,
        skips=skips,
        archived=archived,
        confusion={label: dict(row) for label, row in confusion.items()},
    )
