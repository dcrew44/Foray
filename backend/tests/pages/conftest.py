"""Fixtures for the page tests (design §5.3, §12.3).

`full_graph(...)` gives one user everything a page can show: a rendered base resume, matches in
four stages (one deep-evaluated with a tailored, rendered resume), a run of every kind with the
inputs its start function stores, a pending approval, a conversation with messages, events and
ledger rows. The tenancy sweep builds two of them; the page tests use one.

Every page call carries `X-Foray-SSR: 1`, as `django()` in `src/lib/api/server.ts` sends it, so
the sliding session never writes during a measured request (§4.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.chat.models import Conversation
from apps.jobs.models import Company, Job
from apps.matching.models import JobMatch
from apps.resumes.models import Document, Resume
from apps.runs.models import Approval, Run
from tests.factories import (
    make_approval,
    make_company,
    make_conversation,
    make_document,
    make_event,
    make_job,
    make_llm_call,
    make_match,
    make_message,
    make_resume,
    make_run,
    make_user,
)

SSR = {"X-Foray-SSR": "1"}
ORIGIN = "http://localhost:3000"


def signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def get_page(client: Client, path: str) -> Any:
    return client.get(path, headers=SSR)


TAILOR_CHANGES = [
    {
        "section": "work",
        "item_id": "w-1",
        "before": "Built the ingestion service.",
        "after": "Built the ingestion service that loads 40 million events a day.",
        "reason": "The posting asks for high-volume data work.",
        "evidence_fact_ids": ["w-1"],
    }
]
TAILOR_VERIFICATION = {
    "passed": True,
    "checkedAt": "2026-10-01T12:00:00.000Z",
    "unsupported": [],
    "revised": False,
    "checked": 12,
    "modelChecked": 3,
    "revisions": [],
    "modelError": None,
    "baseResumeId": None,
    "baseVersion": 1,
}


@dataclass
class Graph:
    """One user's rows, named for the tests."""

    user: User
    marker: str
    company: Company
    jobs: dict[str, Job]
    matches: dict[str, JobMatch]
    base: Resume
    tailored: Resume
    upload: Document
    base_pdf: Document
    tailored_pdf: Document
    conversation: Conversation
    runs: dict[str, Run]
    approval: Approval

    @property
    def markers(self) -> list[str]:
        """Text only this user's rows hold."""
        return [self.marker]

    @property
    def ids(self) -> list[str]:
        """This user's per-user row ids."""
        rows: list[Any] = [
            *self.matches.values(),
            *self.runs.values(),
            self.base,
            self.tailored,
            self.upload,
            self.base_pdf,
            self.tailored_pdf,
            self.conversation,
            self.approval,
        ]
        return [str(row.pk) for row in rows] + [str(self.user.pk)]


def full_graph(
    *,
    marker: str = "Avery",
    name: str | None = None,
    email: str | None = None,
    company: Company | None = None,
    other_company: Company | None = None,
) -> Graph:
    """A ready user (complete preferences, a base resume) with one of everything. `marker` is in
    every per-user text: the name, prompts, titles, notes, file names and job titles."""
    user = make_user(with_resume=True, name=name or f"{marker} Quinn", email=email)
    company = company or make_company(name="Stripe", slug="stripe")
    other_company = other_company or make_company(name="Linear", slug="linear", source_type="ashby")
    base = Resume.objects.for_user(user.pk).get(kind="base")
    base_pdf = make_document(user, kind="resume_pdf", filename=f"{marker} - Resume.pdf")
    base.rendered_document = base_pdf
    base.save(update_fields=["rendered_document", "updated_at"])
    upload = make_document(user, filename=f"{marker} resume.pdf")

    jobs: dict[str, Job] = {}
    jobs["backend"] = make_job(
        company=company,
        title=f"{marker} Backend Engineer",
        salary_min=150000,
        salary_max=190000,
        salary_currency="USD",
        salary_period="year",
        apply_url="https://stripe.example.com/apply/1",
    )
    jobs["platform"] = make_job(company=company, title=f"{marker} Platform Engineer")
    jobs["product"] = make_job(company=other_company, title=f"{marker} Product Engineer")
    jobs["data"] = make_job(company=other_company, title=f"{marker} Data Engineer", is_active=False)

    matches: dict[str, JobMatch] = {}
    matches["found"] = make_match(
        user, job=jobs["backend"], fast_score=88, deep_score=92, notes=f"{marker} replied."
    )
    matches["shortlisted"] = make_match(
        user, job=jobs["platform"], stage="shortlisted", fast_score=70
    )
    matches["applied"] = make_match(user, job=jobs["product"], stage="applied", fast_score=75)
    matches["archived"] = make_match(user, job=jobs["data"], stage="archived", fast_score=40)

    tailored_pdf = make_document(
        user, kind="resume_pdf", filename=f"{marker} - Resume - Stripe.pdf"
    )
    tailored = make_resume(
        user,
        kind="tailored",
        match=matches["found"],
        changes=TAILOR_CHANGES,
        verification=TAILOR_VERIFICATION,
        rendered_document=tailored_pdf,
    )
    matches["found"].tailored_resume = tailored
    matches["found"].save(update_fields=["tailored_resume", "updated_at"])

    conversation = make_conversation(user, title=f"{marker} chat")
    make_message(conversation, text=f"{marker} asks about backend roles")

    found, shortlisted = str(matches["found"].id), str(matches["shortlisted"].id)
    runs: dict[str, Run] = {}
    runs["search"] = make_run(
        user,
        kind="search",
        status="completed",
        input={"prompt": f"{marker} backend roles", "hints": {"roles": ["Backend Engineer"]}},
        output={
            "candidates": 4,
            "newMatches": 4,
            "scored": 4,
            "autoArchived": 1,
            "evaluated": 1,
            "shortlisted": 0,
            "strong": 1,
            "topMatchIds": [found],
        },
        conversation=conversation,
    )
    runs["rank_deep"] = make_run(
        user, kind="rank_deep", status="completed", input={"matchIds": [found, shortlisted]}
    )
    runs["tailor"] = make_run(
        user,
        kind="tailor",
        status="completed",
        input={"matchId": found, "prompt": f"Tailor your resume for {marker}"},
    )
    runs["render_base"] = make_run(
        user,
        kind="resume_render",
        status="completed",
        input={"resumeId": str(base.id), "prompt": "Render your resume to PDF"},
    )
    runs["render_tailored"] = make_run(
        user,
        kind="resume_render",
        status="completed",
        input={"resumeId": str(tailored.id), "prompt": "Render your tailored resume"},
    )
    runs["import"] = make_run(
        user,
        kind="profile_import",
        status="completed",
        input={
            "documentId": str(upload.id),
            "filename": f"{marker} resume.pdf",
            "prompt": "Read your resume",
        },
    )
    runs["add"] = make_run(
        user,
        kind="company_discovery",
        status="completed",
        input={"query": f"{marker}.example", "refreshAfter": True, "prompt": f"Add {marker}"},
    )
    runs["names"] = make_run(
        user,
        kind="company_discovery",
        status="completed",
        input={"names": ["Linear", "Retool"], "refreshAfter": False, "prompt": "Find job boards"},
    )
    runs["refresh_one"] = make_run(
        user,
        kind="company_refresh",
        status="completed",
        input={"slug": company.slug, "name": company.name, "stale": False, "prompt": "Refresh"},
    )
    runs["refresh_stale"] = make_run(
        user,
        kind="company_refresh",
        status="completed",
        input={"stale": True, "prompt": "Refresh the stale boards"},
    )
    runs["waiting"] = make_run(
        user,
        kind="search",
        status="awaiting_approval",
        input={"prompt": f"{marker} platform roles"},
    )
    approval = make_approval(user, run=runs["waiting"], title=f"{marker} approval")
    runs["running"] = make_run(
        user,
        kind="tailor",
        status="running",
        input={"matchId": shortlisted, "prompt": f"Tailor for {marker} platform"},
    )

    make_event(user, kind="run.completed", run=runs["search"], message=f"{marker} search done")
    make_event(user, kind="approval.requested", run=runs["waiting"], message=f"{marker} asks")

    make_llm_call(user, purpose="score", run=runs["search"], cost_usd=Decimal("0.012000"))
    make_llm_call(
        user,
        purpose="evaluate",
        run=runs["rank_deep"],
        provider="openai",
        model="gpt-5.6-terra",
        cost_usd=Decimal("0.210000"),
        latency_ms=12000,
    )
    make_llm_call(
        user,
        purpose="chat",
        conversation=conversation,
        model=f"{marker.lower()}-chat-model",
        cost_usd=Decimal("0.003000"),
        error="Provider timeout",
    )
    # Last month's spend is not this month's.
    make_llm_call(
        user, purpose="score", cost_usd=Decimal("5"), created_at=timezone.now() - timedelta(days=45)
    )
    return Graph(
        user=user,
        marker=marker,
        company=company,
        jobs=jobs,
        matches=matches,
        base=base,
        tailored=tailored,
        upload=upload,
        base_pdf=base_pdf,
        tailored_pdf=tailored_pdf,
        conversation=conversation,
        runs=runs,
        approval=approval,
    )
