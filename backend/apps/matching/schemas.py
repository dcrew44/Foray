"""What the scoring passes return, stored in `job_matches.fast_eval` / `deep_eval` (design §2.3;
`map/llm.md` §14, a port of `src/lib/ranking/schemas.ts`).

Every field is required, with absence written as null or an empty list: OpenAI's strict
structured output rejects optional properties, and a cheap model given an optional field simply
omits it. The rows store these objects exactly, so field names and order are the stored ones.

The models carry no docstrings on purpose: pydantic turns a class docstring into the JSON
schema's description, and these schemas are sent to the model as they were from TypeScript.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from apps.core.choices import JsonModel

# The posting's level as the ranking passes see it. The profile's seniority
# (`accounts.schemas.Seniority`) has no `unclear`.
RankingSeniority = Literal["intern", "new_grad", "entry", "mid", "senior", "staff_plus", "unclear"]

Recommendation = Literal["strong", "apply", "maybe", "skip"]


class FastScore(JsonModel):
    # The cheap pass, run over hundreds of postings, so it stays small.
    score: int = Field(ge=0, le=100)  # calibrated by the rubric in the instructions
    fit_reasons: list[str]  # at most three, short
    red_flags: list[str]
    seniority: RankingSeniority  # the posting's level, as the posting describes it
    hard_requirement_misses: list[str]  # stated requirements the candidate does not meet


class MatchedRequirement(JsonModel):
    requirement: str  # what the posting asks for, in its own terms
    evidence: str  # the resume evidence that meets it
    fact_ids: list[str]  # ids of the resume items the evidence comes from
    strength: Literal["strong", "partial"]


class EvalGap(JsonModel):
    gap: str
    # `disqualifying` only for a hard requirement clearly failed or one of the user's own
    # dealbreakers; everything else is `learnable`.
    severity: Literal["learnable", "disqualifying"]
    note: str


class DeepEval(JsonModel):
    # Evidence comes before the verdict on purpose: models write fields in order, and a score
    # written after the requirements is anchored to them rather than to a first impression.
    matched_requirements: list[MatchedRequirement]
    gaps: list[EvalGap]
    seniority_fit: Literal["under", "match", "over", "unclear"] = Field(
        description=(
            "The candidate against the posting's level: under = the role asks for more "
            "experience than they have; over = they are overqualified."
        )
    )
    location_fit: Literal["match", "partial", "mismatch", "unclear"]
    salary_fit: Literal["above", "within", "below", "unstated"] = Field(
        description=(
            "The posting's pay against the candidate's salary floor: above = pays more than "
            "their floor; below = pays less."
        )
    )
    risk_flags: list[str]
    talking_points: list[str]  # what to emphasise when applying; read by tailoring
    summary: str  # two or three plain sentences, specific to this person and this role
    score: int = Field(ge=0, le=100)
    recommendation: Recommendation
