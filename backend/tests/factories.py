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
from apps.applications.models import Application, ApplicationAnswer
from apps.chat.models import Conversation, Message
from apps.contacts.link import company_key
from apps.contacts.models import Contact, OutreachNote
from apps.core.ids import generate_message_id
from apps.jobs.models import BoardCandidate, Company, Job
from apps.llm.models import LlmCall
from apps.matching.models import JobMatch
from apps.matching.schemas import DeepEval, EvalGap, FastScore, MatchedRequirement
from apps.matching.verdict import band_of
from apps.notifications.models import Notification
from apps.resumes import schema as resume_schema
from apps.resumes.models import Document, Resume, ResumeVariant
from apps.resumes.variants import VariantSpec, compose, full_spec
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
    recommendation = band_of(score)  # the rubric's bands, as a real review would say
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


def make_answer(
    user: User, *, kind: str = "sponsorship", answer: str = "no", **fields: Any
) -> ApplicationAnswer:
    """A saved answer to one of the questions application forms share (#21)."""
    fields.setdefault("question", "Will you now or in the future require sponsorship?")
    return ApplicationAnswer.objects.create_for(user.pk, kind=kind, answer=answer, **fields)


def make_application(user: User, match: JobMatch | None = None, **fields: Any) -> Application:
    """A match's application (#60), `preparing` unless told otherwise."""
    match = match if match is not None else make_match(user)
    return Application.objects.create_for(user.pk, match=match, **fields)


def make_candidate(
    *, source_type: str = "greenhouse", ats_slug: str | None = None, **fields: Any
) -> BoardCandidate:
    """A board seen in a crawl and not asked yet (#58)."""
    fields.setdefault("found_in", ["commoncrawl:CC-MAIN-2026-43"])
    fields.setdefault("newest", "CC-MAIN-2026-43")
    return BoardCandidate.objects.create(
        source_type=source_type, ats_slug=ats_slug or f"candidate-{_n()}", **fields
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


def make_variant(
    user: User,
    *,
    name: str | None = None,
    keywords: list[str] | None = None,
    spec: VariantSpec | dict[str, Any] | None = None,
    **fields: Any,
) -> ResumeVariant:
    """A resume variant (#61). Without a `spec` it shows everything in the sample resume, as a
    new variant shows everything in the base."""
    if spec is None:
        spec = full_spec(resume_schema.Resume.model_validate(sample_resume_data()))
    return ResumeVariant.objects.create_for(
        user.pk,
        name=name or f"Variant {_n()}",
        keywords=keywords if keywords is not None else ["backend"],
        spec=spec.dump() if isinstance(spec, VariantSpec) else spec,
        **fields,
    )


def make_resume(
    user: User,
    *,
    kind: str = "base",
    match: JobMatch | None = None,
    variant: ResumeVariant | None = None,
    version: int | None = None,
    data: dict[str, Any] | None = None,
    **fields: Any,
) -> Resume:
    """A resume version: base by default (the next version for the user), tailored for `match`
    (the next version for the match), or a snapshot of `variant` (the next for the variant,
    holding what it composes to from the sample resume)."""
    if kind == "tailored" and match is None:
        match = make_match(user)
    if kind == "variant" and variant is None:
        variant = make_variant(user)
    if version is None:
        if kind == "base":
            existing = Resume.objects.for_user(user.pk).filter(kind="base")
        elif kind == "variant":
            existing = Resume.objects.for_user(user.pk).filter(kind="variant", variant=variant)
        else:
            existing = Resume.objects.for_user(user.pk).filter(kind="tailored", match=match)
        latest = existing.order_by("-version").values_list("version", flat=True).first()
        version = (latest or 0) + 1
    if data is None and variant is not None:
        base = resume_schema.Resume.model_validate(sample_resume_data())
        data = compose(base, VariantSpec.model_validate(variant.spec)).dump()
    return Resume.objects.create_for(
        user.pk,
        kind=kind,
        match=match,
        variant=variant,
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
        case RunKind.WATCH:
            return catalogue.WatchInput(prompt="Check new postings").dump(exclude_none=True)
        case RunKind.OUTREACH:
            return catalogue.OutreachInput(
                match_id=str(uuid.uuid4()),
                contact_id=str(uuid.uuid4()),
                prompt="Draft a note to a contact",
            ).dump()
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


# --- notifications -------------------------------------------------------------------------------


def make_notification(
    user: User, *, match: JobMatch | None = None, channel: str = "ntfy", **fields: Any
) -> Notification:
    """A phone notification waiting to be sent, due now, for `match` (by default a new match
    the deep review called strong, as a watch would have announced)."""
    return Notification.objects.create_for(
        user.pk,
        match=match if match is not None else make_match(user, fast_score=88, deep_score=86),
        channel=channel,
        **fields,
    )


# --- contacts ------------------------------------------------------------------------------------


def make_contact(
    user: User,
    *,
    name: str | None = None,
    company: Company | None = None,
    company_name: str | None = None,
    **fields: Any,
) -> Contact:
    """Someone the user knows (#63). With `company`, a contact already linked to that
    registered company, under its name unless `company_name` says how the file spelled it."""
    n = _n()
    written = company_name if company_name is not None else (company.name if company else "")
    fields.setdefault("source", "linkedin")
    fields.setdefault("dedupe_key", _sha1(f"contact-{n}"))
    return Contact.objects.create_for(
        user.pk,
        name=name or f"Contact {n}",
        company=company,
        company_name=written,
        company_key=company_key(written),
        **fields,
    )


def make_outreach_note(
    user: User,
    *,
    match: JobMatch | None = None,
    contact: Contact | None = None,
    **fields: Any,
) -> OutreachNote:
    """A note drafted to a contact about a match (#25): by default to a new contact at the
    company of a new match."""
    match = match if match is not None else make_match(user)
    if contact is None:
        contact = make_contact(user, company=match.job.company)
    fields.setdefault("body", "A short note, as the model drafted it.")
    return OutreachNote.objects.create_for(user.pk, match=match, contact=contact, **fields)


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
