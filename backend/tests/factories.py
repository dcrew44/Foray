"""Row factories for tests (design §12.1). Plain functions, no factory_boy.

Each builds one valid row with sensible defaults and saves it; keyword arguments override any
field. Per-user rows take their user first and are created through the scoped manager
(`objects.create_for`), as app code does. Every factory needs the database (`django_db`).

- `make_user(with_resume=True)` gives a user with complete preferences and a base resume, like
  `manage.py create_test_user --with-resume`; the resume is `apps/accounts/sample_resume.json`.
- Scores come with their evaluations (`jm_*_paired`): `make_match(user, fast_score=72)` stores a
  matching `FastScore`.
- Runs, approvals and events take their JSON shapes from `runs.catalogue`.

A lane's own fixtures go in `tests/<app>/conftest.py`, not here.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from django.db.models.functions import Now
from django.utils import timezone

import apps.accounts
from apps.accounts.models import User
from apps.accounts.schemas import SAMPLE_PREFERENCES
from apps.chat.models import Conversation, Message
from apps.core.ids import generate_message_id
from apps.jobs.models import Company, Job
from apps.llm.models import LlmCall
from apps.matching.models import JobMatch
from apps.matching.schemas import DeepEval, EvalGap, FastScore, MatchedRequirement
from apps.resumes.models import Document, Resume
from apps.runs import catalogue
from apps.runs.catalogue import ApprovalKind, ApprovalStatus, RunKind
from apps.runs.models import Approval, Event, Run

SAMPLE_RESUME_PATH = Path(apps.accounts.__file__).resolve().parent / "sample_resume.json"

_seq = itertools.count(1)


def _n() -> int:
    return next(_seq)


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


@cache
def _sample_resume() -> dict[str, Any]:
    return json.loads(SAMPLE_RESUME_PATH.read_text())


def sample_resume_data() -> dict[str, Any]:
    """A fresh copy of the synthetic resume, in `resumes.schema.Resume`'s stored shape."""
    return copy.deepcopy(_sample_resume())


def sample_fast_eval(score: int = 72) -> dict[str, Any]:
    return FastScore(
        score=score,
        fit_reasons=["Backend work in Python", "Payments experience"],
        red_flags=[],
        seniority="mid",
        hard_requirement_misses=[],
    ).dump()


def sample_deep_eval(score: int = 81) -> dict[str, Any]:
    recommendation = (
        "strong" if score >= 80 else "apply" if score >= 65 else "maybe" if score >= 45 else "skip"
    )
    return DeepEval(
        matched_requirements=[
            MatchedRequirement(
                requirement="Production Python",
                evidence="Built the ingestion service that loads 40 million events a day.",
                fact_ids=["d84a4aa2-42c8-4f9d-8a69-aeafe57027b6"],
                strength="strong",
            )
        ],
        gaps=[EvalGap(gap="Rust", severity="learnable", note="Listed as a plus.")],
        seniority_fit="match",
        location_fit="match",
        salary_fit="within",
        risk_flags=[],
        talking_points=["The streaming settlement migration"],
        summary="A close fit for a backend role on a payments team.",
        score=score,
        recommendation=recommendation,
    ).dump()


# --- accounts ------------------------------------------------------------------------------------


def make_user(
    *,
    email: str | None = None,
    name: str = "Test User",
    password: str | None = None,
    with_resume: bool = False,
    **fields: Any,
) -> User:
    """A user with an unusable password unless one is given. `with_resume=True` adds complete
    preferences and a base resume (version 1)."""
    if with_resume:
        fields.setdefault("preferences", SAMPLE_PREFERENCES.dump())
    user = User.objects.create_user(
        email or f"test-{_n()}-{uuid.uuid4().hex[:8]}@example.test",
        password=password,
        name=name,
        **fields,
    )
    if with_resume:
        make_resume(user)
    return user


# --- jobs ----------------------------------------------------------------------------------------


def make_company(
    *,
    name: str | None = None,
    slug: str | None = None,
    source_type: str = "greenhouse",
    ats_slug: str | None = None,
    **fields: Any,
) -> Company:
    n = _n()
    slug = slug or f"company-{n}"
    if source_type == "careers_page":
        fields.setdefault("careers_urls", [f"https://{slug}.example.com/careers"])
    else:
        ats_slug = ats_slug or slug
    fields.setdefault("discovered_via", "seed")
    return Company.objects.create(
        name=name or f"Company {n}",
        slug=slug,
        source_type=source_type,
        ats_slug=ats_slug,
        **fields,
    )


def make_job(
    *,
    company: Company | None = None,
    source: str | None = None,
    external_id: str | None = None,
    title: str = "Software Engineer",
    **fields: Any,
) -> Job:
    """An active posting seen now. Its id follows `jobs_id_shape`."""
    company = company if company is not None else make_company()
    if source is None:
        source = "web" if company.source_type == "careers_page" else company.source_type
    external_id = external_id or (
        _sha1(f"https://{company.slug}.example.com/jobs/{_n()}") if source == "web" else str(_n())
    )
    now = timezone.now()
    locations = fields.pop("locations", ["San Francisco, CA"])
    description = fields.pop("description_text", f"{title} at {company.name}. Python, PostgreSQL.")
    defaults: dict[str, Any] = {
        "id": f"{source}:{company.slug}:{external_id}",
        "company_name": company.name,
        "locations": locations,
        "location_norm": {
            "raw": locations[0] if locations else "",
            "city": "San Francisco",
            "region": "CA",
            "country": "US",
            "metro": "bay-area",
        },
        "remote": "hybrid",
        "description_raw": f"<p>{description}</p>",
        "description_text": description,
        "url": f"https://{company.slug}.example.com/jobs/{external_id}",
        "posted_at": now - timedelta(days=2),
        "raw": {"id": external_id},
        "content_hash": _sha1(f"{title}|{external_id}|{description}"),
        "dedupe_key": _sha1(f"{company.name}|{title}|{locations[:1]}"),
        "first_seen_at": now,
        "last_seen_at": now,
        "fetched_at": now,
    }
    return Job.objects.create(
        **{**defaults, **fields},
        source=source,
        external_id=external_id,
        company=company,
        title=title,
    )


# --- matching ------------------------------------------------------------------------------------


def make_match(
    user: User,
    *,
    job: Job | None = None,
    stage: str = "found",
    fast_score: int | None = None,
    deep_score: int | None = None,
    **fields: Any,
) -> JobMatch:
    """A match. A score without an evaluation gets a sample one, so the pair stays together."""
    if fast_score is not None and "fast_eval" not in fields:
        fields["fast_eval"] = sample_fast_eval(fast_score)
    if deep_score is not None and "deep_eval" not in fields:
        fields["deep_eval"] = sample_deep_eval(deep_score)
    return JobMatch.objects.create_for(
        user.pk,
        job=job if job is not None else make_job(),
        stage=stage,
        fast_score=fast_score,
        deep_score=deep_score,
        **fields,
    )


# --- resumes -------------------------------------------------------------------------------------


def make_document(user: User, *, kind: str = "resume_upload", **fields: Any) -> Document:
    n = _n()
    filename = fields.pop("filename", f"resume-{n}.pdf")
    content = fields.pop("content", f"%PDF-1.7 synthetic {n} {uuid.uuid4().hex}".encode())
    defaults: dict[str, Any] = {
        "filename": filename,
        "mime": "application/pdf",
        "size_bytes": len(content),
        "storage_path": f"{user.pk}/{kind}/{1_790_000_000_000 + n}-{filename}",
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    return Document.objects.create_for(user.pk, kind=kind, **{**defaults, **fields})


def make_resume(
    user: User,
    *,
    kind: str = "base",
    match: JobMatch | None = None,
    version: int | None = None,
    data: dict[str, Any] | None = None,
    **fields: Any,
) -> Resume:
    """A resume version: base by default (the next version for the user), or tailored for
    `match` (the next version for the match)."""
    if kind == "tailored" and match is None:
        match = make_match(user)
    if version is None:
        if kind == "base":
            existing = Resume.objects.for_user(user.pk).filter(kind="base")
        else:
            existing = Resume.objects.for_user(user.pk).filter(kind="tailored", match=match)
        latest = existing.order_by("-version").values_list("version", flat=True).first()
        version = (latest or 0) + 1
    return Resume.objects.create_for(
        user.pk,
        kind=kind,
        match=match,
        version=version,
        data=data if data is not None else sample_resume_data(),
        **fields,
    )


# --- runs ----------------------------------------------------------------------------------------


def _default_input(kind: str) -> dict[str, Any]:
    match kind:
        case RunKind.SEARCH:
            return catalogue.SearchInput(prompt="Search with my saved preferences").dump(
                exclude_none=True
            )
        case RunKind.RANK_DEEP:
            return catalogue.EvaluateInput(match_ids=[]).dump()
        case RunKind.TAILOR:
            return catalogue.TailorInput(
                match_id=str(uuid.uuid4()), prompt="Tailor your resume for a role"
            ).dump()
        case RunKind.RESUME_RENDER:
            return catalogue.RenderInput(
                resume_id=str(uuid.uuid4()), prompt="Render your resume to PDF"
            ).dump()
        case RunKind.PROFILE_IMPORT:
            return catalogue.ImportInput(
                document_id=str(uuid.uuid4()), filename="resume.pdf", prompt="Read your resume"
            ).dump()
        case RunKind.COMPANY_DISCOVERY:
            return catalogue.CompanyAddInput(
                query="Example Co", refresh_after=True, prompt="Add Example Co"
            ).dump(exclude_none=True)
        case RunKind.COMPANY_REFRESH:
            return catalogue.CompanyRefreshInput(
                stale=True, prompt="Refresh boards not fetched lately"
            ).dump(exclude_none=True)
        case _:
            raise ValueError(f"Unknown run kind {kind!r}")


def make_run(
    user: User,
    *,
    kind: str = "search",
    status: str = "queued",
    mode: str = "approve_each",
    input: dict[str, Any] | None = None,
    **fields: Any,
) -> Run:
    """A run row only: no job is deferred (use `runs.lifecycle.start_run` for that)."""
    if status in ("running", "awaiting_approval", *catalogue.TERMINAL):
        fields.setdefault("started_at", timezone.now())
    if status in catalogue.TERMINAL:
        fields.setdefault("finished_at", timezone.now())
    return Run.objects.create_for(
        user.pk,
        kind=kind,
        status=status,
        mode=mode,
        input=input if input is not None else _default_input(kind),
        **fields,
    )


def _default_payload(kind: str) -> dict[str, Any]:
    if kind == ApprovalKind.DEEP_EVAL_BATCH:
        return catalogue.DeepEvalBatchPayload(
            items=[
                catalogue.DeepEvalBatchItem(
                    match_id=str(uuid.uuid4()),
                    job_id="greenhouse:example:1",
                    title="Software Engineer",
                    company="Example",
                    fast_score=78,
                )
            ],
            estimate=catalogue.CostEstimate(
                usd=0.02, per_item_usd=0.02, model="openai:gpt-5.6-terra"
            ),
            note=None,
        ).dump()
    return catalogue.ShortlistPayload(
        items=[
            catalogue.ShortlistItem(
                match_id=str(uuid.uuid4()),
                job_id="greenhouse:example:1",
                title="Software Engineer",
                company="Example",
                deep_score=84,
                recommendation="strong",
                summary="A close fit.",
            )
        ]
    ).dump()


def make_approval(
    user: User,
    *,
    run: Run | None = None,
    kind: str = "deep_eval_batch",
    status: str = "pending",
    **fields: Any,
) -> Approval:
    """An approval on `run` (a new search run awaiting approval by default). A pending one
    expires in 7 days; a decided one carries a decision."""
    if run is None:
        run = make_run(user, status="awaiting_approval" if status == "pending" else "running")
    if status == ApprovalStatus.PENDING:
        fields.setdefault("expires_at", Now() + timedelta(days=7))  # the database's clock
    elif status in (ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED, ApprovalStatus.REJECTED):
        approved = status != ApprovalStatus.REJECTED
        fields.setdefault("decision", catalogue.StoredDecision(approved=approved).dump())
        fields.setdefault(
            "decided_via", "autopilot" if status == ApprovalStatus.AUTO_APPROVED else "ui"
        )
        fields.setdefault("decided_at", Now())
    elif status == ApprovalStatus.EXPIRED:
        fields.setdefault("decided_via", "expiry")
    return Approval.objects.create_for(
        user.pk,
        run=run,
        kind=kind,
        title=fields.pop("title", "Deep-evaluate 1 role"),
        payload=fields.pop("payload", _default_payload(kind)),
        status=status,
        next_step=fields.pop("next_step", catalogue.APPROVAL_NEXT_STEP[kind]),
        **fields,
    )


def make_event(
    user: User,
    *,
    kind: str = "run.started",
    message: str | None = None,
    run: Run | None = None,
    level: str | None = None,
    **fields: Any,
) -> Event:
    """An event row written directly (`runs.events.emit` is the app's only writer)."""
    return Event.objects.create_for(
        user.pk,
        kind=kind,
        message=message or f"Test event {kind}",
        run=run,
        level=level or catalogue.DEFAULT_LEVEL.get(kind, "info"),
        **fields,
    )


# --- chat ----------------------------------------------------------------------------------------


def make_conversation(
    user: User, *, title: str | None = "Test chat", **fields: Any
) -> Conversation:
    fields.setdefault("id", uuid.uuid4())  # client-chosen in the app
    return Conversation.objects.create_for(user.pk, title=title, **fields)


def make_message(
    conversation: Conversation,
    *,
    role: str = "user",
    text: str = "Hello",
    **fields: Any,
) -> Message:
    """A UI message with one text part, owned by the conversation's user."""
    fields.setdefault("message_id", generate_message_id())
    fields.setdefault("parts", [{"type": "text", "text": text}])
    return Message.objects.create_for(
        conversation.user_id, conversation=conversation, role=role, **fields
    )


# --- llm -----------------------------------------------------------------------------------------


def make_llm_call(user: User | None = None, *, purpose: str = "score", **fields: Any) -> LlmCall:
    """A ledger row; `user=None` is a system row (the cron)."""
    defaults: dict[str, Any] = {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "input_tokens": 1200,
        "output_tokens": 150,
        "cost_usd": Decimal("0.000420"),
        "latency_ms": 900,
    }
    values = {**defaults, **fields, "purpose": purpose}
    if user is None:
        return LlmCall.objects.unscoped("tests: a system ledger row").create(user=None, **values)
    return LlmCall.objects.create_for(user.pk, **values)
