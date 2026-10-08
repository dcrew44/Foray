"""The data model (design §2): the database refuses what the app must never write, every
factory builds a valid row, the JSON shapes keep today's keys and read leniently, and the kind
catalogue is complete.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError, connection, models, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.schemas import SAMPLE_PREFERENCES, InterviewState, Preferences, Settings
from apps.chat.models import Message
from apps.jobs.models import ApiUsage, Company, Job
from apps.jobs.schemas import ListingCacheEntry, NormalizedLocation, read_listing_cache
from apps.legacy.models import ImportRecord
from apps.matching.models import JobMatch
from apps.matching.schemas import DeepEval, FastScore
from apps.resumes import schema
from apps.resumes.models import Resume
from apps.resumes.queries import latest_base_resume, latest_base_resume_row
from apps.runs import catalogue
from apps.runs.catalogue import EventKind, Level, RunKind
from apps.runs.models import Approval, Event, Run
from tests import factories as f

pytestmark = pytest.mark.django_db


def rejected(build: Callable[[], object]) -> str:
    """Runs `build` in a savepoint and returns the name of the constraint that refused it."""
    with pytest.raises(IntegrityError) as info, transaction.atomic():
        build()
    diag = getattr(info.value.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) or str(info.value)


# --- the database refuses ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("runs", "status"),
        ("runs", "kind"),
        ("runs", "mode"),
        ("approvals", "status"),
        ("approvals", "next_step"),
        ("job_matches", "stage"),
        ("companies", "status"),
        ("jobs", "remote"),
        ("events", "level"),
        ("documents", "kind"),
        ("messages", "role"),
        ("llm_calls", "purpose"),
    ],
)
def test_bad_status_rejected(table: str, column: str) -> None:
    user = f.make_user()
    rows: dict[str, models.Model] = {
        "runs": f.make_run(user),
        "approvals": f.make_approval(user),
        "job_matches": f.make_match(user),
        "companies": f.make_company(),
        "jobs": f.make_job(),
        "events": f.make_event(user),
        "documents": f.make_document(user),
        "messages": f.make_message(f.make_conversation(user)),
        "llm_calls": f.make_llm_call(user),
    }
    row = rows[table]
    model = type(row)
    manager: Any = model._default_manager  # the tests may use the plain manager

    def write() -> None:
        manager.filter(pk=row.pk).update(**{column: "bogus"})

    assert rejected(write) == f"{table}_{column}_valid"


def test_second_active_search_rejected() -> None:
    user, other = f.make_user(), f.make_user()
    f.make_run(user, kind="search", status="running", dedupe_key="search")

    for status in ("queued", "running", "awaiting_approval"):
        name = rejected(
            lambda status=status: f.make_run(
                user, kind="search", status=status, dedupe_key="search"
            )
        )
        assert name == "runs_one_active_per_key"

    # Finished runs and other users do not count; a run without a key is never deduped.
    f.make_run(user, kind="search", status="completed", dedupe_key="search")
    f.make_run(other, kind="search", status="queued", dedupe_key="search")
    f.make_run(user, kind="search", status="queued", dedupe_key=None)
    f.make_run(user, kind="search", status="queued", dedupe_key=None)


def test_duplicate_base_version_rejected() -> None:
    user, other = f.make_user(), f.make_user()
    f.make_resume(user, version=1)
    assert rejected(lambda: f.make_resume(user, version=1)) == "resumes_base_version"
    f.make_resume(other, version=1)  # versions are per user

    match = f.make_match(user)
    f.make_resume(user, kind="tailored", match=match, version=1)
    assert (
        rejected(lambda: f.make_resume(user, kind="tailored", match=match, version=1))
        == "resumes_tailored_version"
    )
    f.make_resume(user, kind="tailored", match=f.make_match(user), version=1)  # per match

    # A base resume never belongs to a match, and a tailored one always does.
    assert rejected(lambda: f.make_resume(user, kind="base", match=match)) == "resumes_kind_match"
    assert (
        rejected(lambda: Resume.objects.create_for(user.pk, kind="tailored", version=9, data={}))
        == "resumes_kind_match"
    )


@pytest.mark.parametrize(
    "job_id",
    [
        "greenhouse:acme",  # no external id
        "workday:acme:1",  # unknown source
        "greenhouse::1",  # empty company slug
        "greenhouse:acme:",  # empty external id
        "acme-1",
    ],
)
def test_malformed_job_id_rejected(job_id: str) -> None:
    company = f.make_company()
    assert rejected(lambda: f.make_job(company=company, id=job_id)) == "jobs_id_shape"


def test_job_id_shapes_accepted() -> None:
    board = f.make_company(slug="acme")
    f.make_job(company=board, id="greenhouse:acme:4012345")
    f.make_job(company=board, source="jsearch", id="jsearch:acme:abc:def==")  # colons after
    careers = f.make_company(slug="widgets", source_type="careers_page")
    job = f.make_job(company=careers)
    assert job.id.startswith("web:widgets:") and len(job.id.rsplit(":", 1)[1]) == 40


def test_company_shape_rejected() -> None:
    assert rejected(lambda: f.make_company(slug="Acme Labs")) == "companies_slug_shape"
    assert rejected(lambda: f.make_company(slug="-acme")) == "companies_slug_shape"
    assert (
        rejected(lambda: f.make_company(source_type="careers_page", ats_slug="acme"))
        == "companies_ats_slug_matches_source"
    )
    assert (
        rejected(
            lambda: Company.objects.create(
                name="No board", slug="no-board", source_type="lever", discovered_via="user"
            )
        )
        == "companies_ats_slug_matches_source"
    )
    f.make_company(slug="acme", source_type="lever", ats_slug="acme_corp")
    assert (
        rejected(lambda: f.make_company(slug="acme-2", source_type="lever", ats_slug="acme_corp"))
        == "companies_board_unique"
    )
    f.make_company(slug="acme-3", source_type="ashby", ats_slug="acme_corp")  # another board


def test_unpaired_score_rejected() -> None:
    user = f.make_user()
    for prefix in ("fast", "deep"):
        assert (
            rejected(
                lambda prefix=prefix: f.make_match(
                    user, **{f"{prefix}_score": 50, f"{prefix}_eval": None}
                )
            )
            == f"jm_{prefix}_paired"
        )
        evaluation: dict[str, Any] = {
            f"{prefix}_eval": f.sample_fast_eval() if prefix == "fast" else f.sample_deep_eval()
        }
        assert rejected(lambda e=evaluation: f.make_match(user, **e)) == f"jm_{prefix}_paired"
        assert (
            rejected(
                lambda prefix=prefix: f.make_match(
                    user, **{f"{prefix}_score": 101, f"{prefix}_eval": {"score": 101}}
                )
            )
            == f"jm_{prefix}_range"
        )
    assert rejected(lambda: f.make_match(user, user_rating=11)) == "jm_rating_range"
    match = f.make_match(user, fast_score=60, deep_score=85, user_rating=-10)
    assert (match.fast_score, match.deep_score) == (60, 85)


def test_two_pending_approvals_on_one_run_rejected() -> None:
    user = f.make_user()
    run = f.make_run(user, status="awaiting_approval")
    f.make_approval(user, run=run, status="pending")
    assert (
        rejected(lambda: f.make_approval(user, run=run, status="pending"))
        == "approvals_one_pending_per_run"
    )
    # Decided approvals do not count.
    f.make_approval(user, run=run, status="approved")
    f.make_approval(user, run=run, kind="shortlist_proposal", status="expired")


def test_one_notice_per_event_and_message_ids_per_conversation() -> None:
    user = f.make_user()
    conversation = f.make_conversation(user)
    event = f.make_event(user, kind="run.completed", run=f.make_run(user, status="completed"))
    f.make_message(conversation, run_update_event=event)
    assert (
        rejected(lambda: f.make_message(conversation, run_update_event=event))
        == "messages_one_notice_per_event"
    )
    f.make_message(conversation, message_id="abc")
    assert rejected(lambda: f.make_message(conversation, message_id="abc")) == "messages_conv_msg"
    f.make_message(f.make_conversation(user), message_id="abc")  # unique per conversation only


def test_timestamps_are_explicit() -> None:
    """No `auto_now`: a given created_at/updated_at is kept on insert, as the import needs."""
    user = f.make_user()
    then = timezone.now() - timedelta(days=400)
    run = f.make_run(user, created_at=then, updated_at=then)
    run.refresh_from_db()
    assert (run.created_at, run.updated_at) == (then, then)
    run.status = "running"
    run.save(update_fields=["status"])
    run.refresh_from_db()
    assert run.updated_at > then and run.created_at == then


def test_idle_in_transaction_timeout_is_set() -> None:
    """The migration altered the database it ran in (here the test database), for sessions
    opened afterwards (§2.2)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT s.setconfig FROM pg_db_role_setting s"
            " JOIN pg_database d ON d.oid = s.setdatabase"
            " WHERE d.datname = current_database() AND s.setrole = 0"
        )
        assert cursor.fetchone() == (["idle_in_transaction_session_timeout=60s"],)


# --- every factory builds a valid row ------------------------------------------------------------


def _strict_json(row: models.Model) -> None:
    """Every JSON column of the row validates strictly against its model."""
    match row:
        case User():
            Preferences.model_validate(row.preferences)
            InterviewState.model_validate(row.interview_state)
            Settings.model_validate(row.settings)
        case Job():
            NormalizedLocation.model_validate(row.location_norm)
        case JobMatch():
            if row.fast_eval is not None:
                FastScore.model_validate(row.fast_eval)
            if row.deep_eval is not None:
                DeepEval.model_validate(row.deep_eval)
        case Resume():
            schema.Resume.model_validate(row.data)
        case Run():
            catalogue.INPUT_MODELS[row.kind].model_validate(row.input)
        case Approval():
            catalogue.PAYLOAD_MODELS[row.kind].model_validate(row.payload)
            if row.decision is not None:
                catalogue.StoredDecision.model_validate(row.decision)
        case _:
            pass


FACTORIES: dict[str, Callable[[User], models.Model]] = {
    "user": lambda _: f.make_user(),
    "user_with_resume": lambda _: f.make_user(with_resume=True),
    "company": lambda _: f.make_company(),
    "careers_company": lambda _: f.make_company(source_type="careers_page"),
    "job": lambda _: f.make_job(),
    "careers_job": lambda _: f.make_job(company=f.make_company(source_type="careers_page")),
    "match": lambda u: f.make_match(u, fast_score=70, deep_score=82),
    "document": lambda u: f.make_document(u),
    "resume": lambda u: f.make_resume(u),
    "tailored_resume": lambda u: f.make_resume(u, kind="tailored"),
    "approval": lambda u: f.make_approval(u),
    "shortlist_approval": lambda u: f.make_approval(
        u, kind="shortlist_proposal", status="approved"
    ),
    "event": lambda u: f.make_event(u, run=f.make_run(u)),
    "conversation": lambda u: f.make_conversation(u),
    "message": lambda u: f.make_message(f.make_conversation(u)),
    "llm_call": lambda u: f.make_llm_call(u),
    "system_llm_call": lambda _: f.make_llm_call(None),
    **{f"run_{kind}": (lambda u, kind=kind: f.make_run(u, kind=kind)) for kind in RunKind.values},
}


@pytest.mark.parametrize("name", list(FACTORIES))
def test_every_factory_builds_a_valid_row(name: str) -> None:
    owner = f.make_user()
    built = FACTORIES[name](owner)
    row = type(built)._default_manager.get(pk=built.pk)
    row.validate_constraints()
    _strict_json(row)


def test_factories_cover_every_model() -> None:
    from django.apps import apps as django_apps

    ours = {
        model
        for model in django_apps.get_models()
        if model.__module__.startswith("apps.") and model._meta.db_table != "rate_limits"
    }
    owner = f.make_user()
    built = {type(FACTORIES[name](owner)) for name in FACTORIES}
    # The quota counter and the import record are written by one module each, in raw SQL or
    # by the importer; no test needs a factory for them.
    assert ours - built == {ApiUsage, ImportRecord}


def test_user_with_resume_is_ready_to_search() -> None:
    user = f.make_user(with_resume=True)
    assert Preferences.read(user.preferences) == SAMPLE_PREFERENCES
    assert all(value not in (None, []) for value in SAMPLE_PREFERENCES.model_dump().values())
    base = latest_base_resume(user.pk)
    assert base is not None and base.version == 1
    assert (len(base.resume.work), len(base.resume.education), len(base.resume.skills)) == (
        3,
        2,
        14,
    )


# --- JSON shapes ---------------------------------------------------------------------------------


def test_sample_resume_round_trips_byte_for_byte() -> None:
    data = f.sample_resume_data()
    dumped = schema.Resume.model_validate(data).dump()
    assert json.dumps(dumped) == json.dumps(data)


def test_stored_keys_keep_todays_spelling() -> None:
    assert list(Settings().dump()) == ["defaultMode", "scoreThreshold", "deepEvalTopN"]
    assert list(Preferences().dump()) == [
        "target_roles",
        "seniority",
        "locations",
        "remote_pref",
        "salary_floor",
        "work_auth",
        "start_date",
        "must_haves",
        "dealbreakers",
    ]
    output = catalogue.SearchOutput(
        candidates=3,
        new_matches=2,
        scored=3,
        auto_archived=1,
        evaluated=0,
        shortlisted=0,
        top_match_ids=["a"],
    )
    assert list(output.dump()) == [
        "candidates",
        "newMatches",
        "scored",
        "autoArchived",
        "evaluated",
        "shortlisted",
        "strong",
        "topMatchIds",
    ]
    assert list(catalogue.EvaluateInput(match_ids=["a"]).dump()) == ["matchIds"]
    assert list(f.sample_deep_eval()) == [
        "matched_requirements",
        "gaps",
        "seniority_fit",
        "location_fit",
        "salary_fit",
        "risk_flags",
        "talking_points",
        "summary",
        "score",
        "recommendation",
    ]
    verification = schema.Verification(
        passed=False,
        checked_at="2026-10-08T12:00:00.000Z",
        unsupported=[
            schema.UnsupportedClaim(
                section="work", item_id="x", text="Led", reason="r", source="model"
            )
        ],
        revised=True,
        checked=4,
        model_checked=2,
        revisions=[],
        model_error=None,
        base_resume_id=None,
        base_version=1,
    ).dump()
    assert list(verification) == [
        "passed",
        "checkedAt",
        "unsupported",
        "revised",
        "checked",
        "modelChecked",
        "revisions",
        "modelError",
        "baseResumeId",
        "baseVersion",
    ]
    assert list(verification["unsupported"][0]) == [
        "section",
        "item_id",
        "text",
        "reason",
        "source",
    ]
    entry = ListingCacheEntry(v=1, sha1="0" * 40, postings=[], more_listing_urls=["https://x"])
    assert list(entry.dump()) == ["v", "sha1", "postings", "moreListingUrls"]
    # Numbers keep their JSON spelling, as JavaScript wrote them.
    assert json.dumps(SAMPLE_PREFERENCES.dump()["salary_floor"]["amount"]) == "150000"


def test_lenient_readers() -> None:
    # Defaulted columns fall back to the defaults, as `safeParse` with a fallback did.
    assert Settings.read(None) == Settings()
    assert Settings.read({"deepEvalTopN": 5}).deep_eval_top_n == 5
    assert Settings.read({"deepEvalTopN": 5}).score_threshold == 55
    assert Settings.read({"scoreThreshold": 500, "deepEvalTopN": 5}) == Settings()
    assert Settings.read("garbage") == Settings()
    assert Preferences.read({"target_roles": ["Data engineer"], "extra": 1}).target_roles == [
        "Data engineer"
    ]
    assert Preferences.read({"seniority": "wizard"}) == Preferences()
    assert InterviewState.read({}) == InterviewState(version=1, skipped=[])
    # The rest read as None when missing or unreadable, and never raise.
    assert schema.Resume.read(None) is None
    assert schema.Resume.read({"work": "nope"}) is None
    assert schema.Resume.read({}) == schema.Resume()
    assert FastScore.read({"score": 120}) is None
    assert schema.TailorChange.read_list([{"section": "summary"}, "x"]) == []
    cache = read_listing_cache(
        {"https://a": {"v": 1, "sha1": "s", "postings": [], "moreListingUrls": []}, "https://b": 3}
    )
    assert list(cache) == ["https://a"]
    # Imported search outputs have no `strong`.
    legacy = {
        "candidates": 5,
        "newMatches": 5,
        "scored": 5,
        "autoArchived": 1,
        "evaluated": 0,
        "shortlisted": 0,
        "topMatchIds": [],
    }
    parsed = catalogue.read_output(RunKind.SEARCH, legacy)
    assert isinstance(parsed, catalogue.SearchOutput) and parsed.strong == 0
    names = catalogue.read_output(
        RunKind.COMPANY_DISCOVERY, {"added": ["Acme"], "notFound": [], "message": "Added Acme."}
    )
    assert isinstance(names, catalogue.CompanyNamesOutput)
    # Imported search inputs keep camelCase and unknown keys in the row; reading ignores the rest.
    search = catalogue.SearchInput.read({"prompt": "p", "skipRefresh": True, "candidateJobIds": []})
    assert (search.prompt, search.candidate_job_ids) == ("p", [])


def test_decision_omits_item_ids_when_there_are_none() -> None:
    assert catalogue.StoredDecision(approved=True).dump() == {"approved": True}
    assert catalogue.StoredDecision(approved=False, item_ids=[]).dump() == {
        "approved": False,
        "itemIds": [],
    }


def test_with_ids_and_fact_ids() -> None:
    extracted = schema.ExtractedResume.model_validate(
        {
            "basics": {
                "name": "A",
                "headline": None,
                "email": None,
                "phone": None,
                "location": None,
                "links": [{"label": "Site", "url": "https://example.com"}],
            },
            "summary": None,
            "work": [
                {
                    "company": "C",
                    "position": "P",
                    "location": None,
                    "startDate": "2024",
                    "endDate": None,
                    "summary": None,
                    "highlights": ["Did a thing"],
                }
            ],
            "education": [],
            "projects": [],
            "skills": [{"name": "Python", "category": None}],
        }
    )
    ids = iter(["l1", "w1", "s1"])
    resume = schema.with_ids(extracted, "resume", lambda: next(ids))
    assert schema.fact_ids(resume) == ["l1", "w1", "s1"]
    assert resume.work[0].start_date == "2024" and resume.work[0].source == "resume"
    assert resume.dump()["work"][0]["startDate"] == "2024"
    with pytest.raises(ValueError):
        schema.ExtractedResume.model_validate({"basics": {}})  # every property is required


def test_latest_base_resume() -> None:
    user = f.make_user()
    assert latest_base_resume(user.pk) is None
    f.make_resume(user)
    newest = f.make_resume(user)
    f.make_resume(user, kind="tailored")
    base = latest_base_resume(user.pk)
    assert base is not None and (base.id, base.version) == (newest.id, 2)
    f.make_resume(user, data={"work": "unreadable"})
    assert latest_base_resume_row(user.pk) is not None
    assert latest_base_resume(user.pk) is None  # the newest does not read: no facts
    assert latest_base_resume(f.make_user().pk) is None


# --- the kind catalogue --------------------------------------------------------------------------

# §9.4's run kinds.
RUN_KINDS = {
    "search",
    "rank_deep",
    "tailor",
    "resume_render",
    "profile_import",
    "company_discovery",
    "company_refresh",
}

# `map/worker.md` §19 (copied here: docs/migration/ goes away in step 13). Where §19 gives two
# levels, the first is the default.
WORKER_MAP_EVENT_KINDS = {
    "worker.ping": "info",
    "run.started": "info",
    "search.criteria": "info",
    "search.companies.added": "info",
    "search.companies.missing": "warn",
    "source.refresh.start": "info",
    "source.refresh.done": "info",
    "source.aggregator.done": "info",
    "source.aggregator.skipped": "info",
    "prefilter.done": "info",
    "scoring.start": "info",
    "scoring.progress": "debug",
    "scoring.done": "info",
    "evaluation.skipped": "warn",
    "approval.requested": "info",
    "approval.auto": "info",
    "approval.decided": "info",
    "approval.reopened": "warn",
    "approval.expired": "warn",
    "evaluation.start": "info",
    "evaluation.progress": "debug",
    "evaluation.failed": "warn",
    "evaluation.done": "info",
    "shortlist.skipped": "info",
    "shortlist.done": "info",
    "run.completed": "info",
    "run.failed": "error",
    "run.cancelled": "warn",
    "tailor.started": "info",
    "tailor.verified": "info",
    "tailor.failed": "error",
    "tailor.done": "info",
    "render.done": "info",
    "render.failed": "warn",
    "company.discovered": "info",
    "company.not-found": "warn",
    "company.refresh.done": "info",
    "chat.followup": "debug",
}


def test_catalogue_lists_every_run_kind() -> None:
    assert set(RunKind.values) == RUN_KINDS
    for table in (
        catalogue.KIND_TITLE,
        catalogue.FAILURE_PREFIX,
        catalogue.FOLLOW_UP_KIND_NAME,
        catalogue.FOLLOW_UP_KIND_TITLE,
        catalogue.PHASES,
        catalogue.STATS_KEYS,
        catalogue.INPUT_MODELS,
        catalogue.OUTPUT_MODELS,
    ):
        assert set(table) == RUN_KINDS
    for kind in RUN_KINDS:
        assert catalogue.PHASES[kind][-1] == "done"
        assert catalogue.STATS_KEYS[kind][0] == "phase"
    assert catalogue.PHASES["search"][-2] == "shortlisting"
    assert catalogue.PHASES["tailor"][0] == "starting"
    assert catalogue.FAILURE_PREFIX["search"] == catalogue.FAILURE_PREFIX["rank_deep"] == ""
    assert catalogue.FAILURE_PREFIX["tailor"] == "Tailoring failed: "


def test_catalogue_lists_every_event_kind_with_its_default_level() -> None:
    assert set(EventKind.values) >= set(WORKER_MAP_EVENT_KINDS)
    assert set(catalogue.DEFAULT_LEVEL) == set(EventKind.values)
    for kind, level in WORKER_MAP_EVENT_KINDS.items():
        assert catalogue.DEFAULT_LEVEL[kind] == level, kind
    assert set(catalogue.DEFAULT_LEVEL.values()) <= set(Level.values)
    assert all(len(kind) <= Event._meta.get_field("kind").max_length for kind in EventKind.values)
    assert set(EventKind.values) >= catalogue.LEGACY_EVENT_KINDS
    follow_up_kinds = {"approval.requested", "run.completed", "run.failed"}
    assert follow_up_kinds == catalogue.FOLLOW_UP_KINDS
    assert not catalogue.FOLLOW_UP_KINDS & catalogue.LEGACY_EVENT_KINDS
    assert catalogue.FOLLOW_UP_EVENT == "chat.followup"


def test_approval_kinds_have_payloads_and_continuations() -> None:
    kinds = set(catalogue.ApprovalKind.values)
    assert kinds == set(catalogue.PAYLOAD_MODELS) == set(catalogue.APPROVAL_NEXT_STEP)
    assert set(catalogue.APPROVAL_NEXT_STEP.values()) == set(catalogue.NextStep.values)


def test_catalogue_enums_are_schema_ready() -> None:
    """The enums and models go into the OpenAPI schema through the registry (§5.4)."""
    from pydantic import TypeAdapter

    schema_of_kind = TypeAdapter(RunKind).json_schema()
    assert schema_of_kind["enum"] == list(RunKind.values)
    schema_of_output = TypeAdapter(catalogue.SearchOutput).json_schema(
        by_alias=True, mode="serialization"
    )
    assert "topMatchIds" in schema_of_output["properties"]


def test_new_ids_are_uuid7() -> None:
    user = f.make_user()
    assert f.make_run(user).id.version == 7
    assert f.make_company().id.version == 7
    assert isinstance(f.make_conversation(user).id, uuid.UUID)
    assert Message._meta.pk is not None and Message._meta.pk.name == "seq"
