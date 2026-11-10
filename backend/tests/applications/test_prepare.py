"""Prepared applications (#60, #62): a form's questions read from its board, an answer planned
for each from the profile and saved answers, what is missing named, and what the user can do
with the result."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx2
import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.applications import actions, prepare
from apps.applications.answers import SavedAnswers, load, save
from apps.applications.forms import STANDARD_FIELDS, parse_greenhouse
from apps.applications.models import AnswerKind, Application
from apps.applications.prepare import Profile, plan_form, profile_of
from apps.matching.models import JobMatch
from apps.resumes.queries import latest_base_resume
from tests.accounts.helpers import Api
from tests.factories import make_application, make_company, make_job, make_match, make_user
from tests.jobs.conftest import fixture_text, mock_client
from tests.pages.conftest import get_page, signed_in
from tests.queue import deferred

db = pytest.mark.django_db(transaction=True)
K = AnswerKind

FIGMA = fixture_text("forms/greenhouse-figma.json")
FORM_URL = "https://boards-api.greenhouse.io/v1/boards/figma/jobs/6241542004?questions=true"
PROFILE = Profile(
    first_name="Avery",
    last_name="Quinn",
    full_name="Avery Quinn",
    email="avery@example.test",
    phone="",
    location="Oakland, CA",
    linkedin="https://www.linkedin.com/in/averyquinn",
)


# --- reading a form ------------------------------------------------------------------------------


def test_a_greenhouse_form_is_its_questions_with_their_options() -> None:
    form = parse_greenhouse(FIGMA, "https://example.test/apply")
    assert (form.source, form.complete) == ("greenhouse", True)
    assert form.apply_url.startswith("https://")
    by_key = {field.key: field for field in form.fields}
    assert [field.key for field in form.fields[:5]] == [
        "first_name",
        "last_name",
        "email",
        "phone",
        "resume",  # the upload; the paste-your-resume box beside it is not a second field
    ]
    assert by_key["resume"].type == "file" and by_key["resume"].required
    authorized = next(field for field in form.fields if "authorized to work" in field.label)
    assert (authorized.type, authorized.required, authorized.options) == (
        "select",
        True,
        ["Yes", "No"],
    )
    # The page's hidden latitude and longitude are not questions; the location box is.
    assert "latitude" not in by_key and by_key["location"].type == "text"
    # The voluntary disclosure, with the option that declines.
    veteran = by_key["veteran_status"]
    assert veteran.required is False and "I don't wish to answer" in veteran.options


# --- planning ------------------------------------------------------------------------------------


def _by_key(planned: list[prepare.Planned]) -> dict[str, prepare.Planned]:
    return {item.key: item for item in planned}


def test_every_field_is_answered_from_the_profile_or_saved_answers_or_named_as_missing() -> None:
    form = parse_greenhouse(FIGMA, "")
    saved = SavedAnswers({"work_authorization": "yes", "previously_employed": "no"}, {})
    planned = _by_key(plan_form(form.fields, PROFILE, saved))

    assert (planned["first_name"].value, planned["first_name"].source) == ("Avery", "profile")
    assert planned["email"].value == "avery@example.test"
    assert (planned["resume"].value, planned["resume"].source) == ("resume", "file")
    assert planned["location"].value == "Oakland, CA"
    # LinkedIn is a link the resume already has.
    linkedin = next(item for item in planned.values() if item.kind == "linkedin")
    assert (linkedin.value, linkedin.source) == (PROFILE.linkedin, "profile")
    # Saved answers, mapped onto the form's own options.
    authorized = next(item for item in planned.values() if item.kind == "work_authorization")
    assert (authorized.value, authorized.source) == ("Yes", "saved")
    worked = next(item for item in planned.values() if item.kind == "previously_employed")
    assert worked.value == "No"
    # Nobody answered the voluntary disclosure: it is declined, with the form's own words.
    assert (planned["veteran_status"].value, planned["veteran_status"].source) == (
        "I don't wish to answer",
        "declined",
    )
    # The resume has no phone and nobody saved one: required, so it is missing.
    assert (planned["phone"].value, planned["phone"].source) == (None, "missing")
    # An optional question with no answer is left empty, not asked for.
    preferred = next(item for item in planned.values() if item.label == "Preferred First Name")
    assert preferred.source == "skipped"
    # The employer's own questions wait for the user: nothing is written for them.
    why = next(item for item in planned.values() if item.label.startswith("Why do you want"))
    assert (why.value, why.source, why.kind) == (None, "missing", "custom")

    missing = prepare.missing_of(list(planned.values()))
    assert "Phone" in missing and "Why do you want to join Figma?" in missing
    assert "Preferred First Name" not in missing and "VeteranStatus" not in missing


def test_a_form_that_cannot_be_read_ahead_plans_the_fields_every_form_has() -> None:
    planned = _by_key(plan_form(STANDARD_FIELDS, PROFILE, SavedAnswers({"phone": "555 0100"}, {})))
    assert planned["phone"].value == "555 0100"  # the saved answer stands in for the resume's
    assert prepare.missing_of(list(planned.values())) == []


@pytest.mark.django_db
def test_the_profile_comes_from_the_newest_base_resume() -> None:
    user = make_user(with_resume=True)
    base = latest_base_resume(user.pk)
    assert base is not None
    profile = profile_of(base.resume)
    assert profile.full_name == base.resume.basics.name
    assert profile.first_name and profile.email == (base.resume.basics.email or "")
    assert profile_of(None) == Profile()


# --- preparing -----------------------------------------------------------------------------------


def _greenhouse_match(user: Any, **job_fields: Any) -> JobMatch:
    company = make_company(name="Figma", slug="figma", source_type="greenhouse", ats_slug="figma")
    job = make_job(
        company=company,
        external_id="6241542004",
        title="PhD Intern, Software Engineer",
        **job_fields,
    )
    return make_match(user, job=job, fast_score=92)


def _serve(monkeypatch: pytest.MonkeyPatch, response: httpx2.Response) -> list[str]:
    asked: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append(str(request.url))
        return response

    monkeypatch.setattr(prepare, "http_client", lambda: mock_client(handler))
    return asked


@db
async def test_preparing_reads_the_form_once_and_says_what_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await sync_to_async(make_user)(with_resume=True)
    match = await sync_to_async(_greenhouse_match)(user)
    asked = _serve(monkeypatch, httpx2.Response(200, text=FIGMA))

    application = await sync_to_async(prepare.start)(user.pk, match.id)
    assert application is not None and application.status == "preparing"
    jobs = await sync_to_async(deferred)("application.prepare")
    assert [job.args for job in jobs] == [{"application_id": str(application.id)}]
    # Starting again while it waits queues nothing more.
    await sync_to_async(prepare.start)(user.pk, match.id)
    assert len(await sync_to_async(deferred)("application.prepare")) == 1

    assert await prepare.prepare(str(application.id)) == "needs_answers"
    assert asked == [FORM_URL]
    stored = await Application.objects.for_user(user.pk).aget(id=application.id)
    assert (stored.status, stored.form_source) == ("needs_answers", "greenhouse")
    assert stored.prepared_at is not None and stored.ready_at is None
    assert len(stored.questions) == len(stored.answers) > 10
    assert "Why do you want to join Figma?" in prepare.missing_of(stored.answers)
    # Another user's match is nobody's to prepare.
    stranger = await sync_to_async(make_user)()
    assert await sync_to_async(prepare.start)(stranger.pk, match.id) is None


@db
async def test_a_posting_that_closed_or_a_form_that_fails_is_said_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apps.core.http._sleep", _no_sleep)
    user = await sync_to_async(make_user)(with_resume=True)
    match = await sync_to_async(_greenhouse_match)(user)
    application = await sync_to_async(prepare.start)(user.pk, match.id)
    assert application is not None

    _serve(monkeypatch, httpx2.Response(500, text="oops"))
    assert await prepare.prepare(str(application.id)) == "failed"
    failed = await Application.objects.for_user(user.pk).aget(id=application.id)
    assert failed.error == "The application form could not be read. Try again later."

    # Asked again, the board says the posting is gone: nothing left to apply to.
    assert await sync_to_async(actions.reopen)(user.pk, application.id) is True
    _serve(monkeypatch, httpx2.Response(404, text="Not Found"))
    assert await prepare.prepare(str(application.id)) == "dismissed"
    gone = await Application.objects.for_user(user.pk).aget(id=application.id)
    assert (gone.status, gone.error) == ("dismissed", "This posting has closed.")


async def _no_sleep(seconds: float) -> None:
    return None


# --- what the user does --------------------------------------------------------------------------


def _waiting(user: Any, **fields: Any) -> Application:
    """An application read from the Figma form and planned, waiting on its answers."""
    form = parse_greenhouse(FIGMA, "https://job-boards.greenhouse.io/figma/jobs/6241542004")
    base = latest_base_resume(user.pk)
    planned = plan_form(form.fields, profile_of(base.resume if base else None), load(user.pk))
    return make_application(
        user,
        fields.pop("match", None) or _greenhouse_match(user),
        status=fields.pop("status", "needs_answers"),
        form_source="greenhouse",
        apply_url=form.apply_url,
        questions=[field.dump() for field in form.fields],
        answers=[item.dump() for item in planned],
        prepared_at=timezone.now(),
        **fields,
    )


def _missing(user: Any, application: Application) -> list[dict[str, Any]]:
    stored = Application.objects.for_user(user.pk).get(id=application.id)
    return [a for a in stored.answers if a["source"] == "missing" and a["required"]]


@pytest.mark.django_db
def test_answering_what_is_missing_makes_it_ready_and_keeps_the_answers() -> None:
    user = make_user(with_resume=True)
    application = _waiting(user)
    waiting_on = _missing(user, application)
    assert len(waiting_on) >= 5

    ready: bool | None = False
    for question in waiting_on:
        value = question["options"][0] if question["options"] else "A considered answer."
        ready = actions.answer(user.pk, application.id, question["key"], value)
    assert ready is True
    stored = Application.objects.for_user(user.pk).get(id=application.id)
    assert stored.status == "ready" and stored.ready_at is not None

    saved = load(user.pk)
    # Kept under the question's own words, and under the shared question when it says that.
    assert saved.get(K.CUSTOM, "Why do you want to join Figma?") == "A considered answer."
    assert saved.get(K.WORK_AUTHORIZATION) == "yes"
    assert saved.get(K.PREVIOUSLY_EMPLOYED) == "yes"  # the form's first option

    # The next application with the same form is ready the moment it is planned.
    other = _waiting(user, match=make_match(user, job=make_job(title="Another role")))
    assert _missing(user, other) == []


@pytest.mark.django_db
def test_an_answer_the_question_cannot_take_is_refused_in_a_sentence() -> None:
    user = make_user(with_resume=True)
    application = _waiting(user)
    choice = next(a for a in _missing(user, application) if a["options"])
    with pytest.raises(actions.AnswerRefused, match="one of the form's own options"):
        actions.answer(user.pk, application.id, choice["key"], "Perhaps")
    with pytest.raises(actions.AnswerRefused, match="Write an answer"):
        actions.answer(user.pk, application.id, choice["key"], "  ")
    with pytest.raises(actions.AnswerRefused, match="no longer on this application"):
        actions.answer(user.pk, application.id, "question_0", "Yes")
    assert actions.answer(make_user().pk, application.id, choice["key"], "Yes") is None


@pytest.mark.django_db
def test_saving_answers_on_the_profile_plans_open_applications_again() -> None:
    user = make_user(with_resume=True)
    application = _waiting(user)
    before = {a["label"] for a in _missing(user, application)}
    assert "Phone" in before

    api = Api(signed_in(user))
    response = api.put("/api/answers", {"answers": [{"kind": "phone", "answer": "555 0100"}]})
    assert response.json() == {"ok": True, "message": "Answers saved"}
    assert "Phone" not in {a["label"] for a in _missing(user, application)}


@pytest.mark.django_db
def test_sending_dismissing_and_bringing_back() -> None:
    user = make_user(with_resume=True)
    application = make_application(user, _greenhouse_match(user), status="ready")
    api = Api(signed_in(user))
    path = f"/api/applications/{application.id}"

    assert api.patch(path, {"action": "opened"}).status_code == 200
    first = Application.objects.for_user(user.pk).get(id=application.id).opened_at
    assert first is not None
    api.patch(path, {"action": "opened"})  # only the first time is kept
    assert Application.objects.for_user(user.pk).get(id=application.id).opened_at == first

    sent = api.patch(path, {"action": "submitted"})
    assert sent.json() == {"ok": True, "message": "Marked as sent. The match moved to Applied."}
    stored = Application.objects.for_user(user.pk).get(id=application.id)
    assert stored.status == "submitted" and stored.submitted_at is not None
    assert JobMatch.objects.for_user(user.pk).get(id=application.match_id).stage == "applied"
    # Sent is sent: it cannot be dismissed, only taken back.
    assert api.patch(path, {"action": "dismiss"}).status_code == 409
    assert api.patch(path, {"action": "reopen"}).status_code == 200
    assert Application.objects.for_user(user.pk).get(id=application.id).status == "ready"

    assert api.patch(path, {"action": "dismiss"}).status_code == 200
    assert Application.objects.for_user(user.pk).get(id=application.id).status == "dismissed"
    # Brought back, it is prepared again by the worker.
    assert api.patch(path, {"action": "reopen"}).status_code == 200
    assert Application.objects.for_user(user.pk).get(id=application.id).status == "preparing"
    assert len(deferred("application.prepare")) == 1


@pytest.mark.django_db
def test_the_endpoint_starts_a_preparation_and_refuses_a_match_that_is_not_yours() -> None:
    user = make_user(with_resume=True)
    match = _greenhouse_match(user)
    api = Api(signed_in(user))
    started = api.post("/api/applications", {"matchId": str(match.id)})
    assert started.status_code == 202
    body = started.json()
    assert body["message"] == "Reading the application form"
    assert Application.objects.for_user(user.pk).get(id=body["applicationId"]).status == "preparing"
    assert (
        Api(signed_in(make_user()))
        .post("/api/applications", {"matchId": str(match.id)})
        .status_code
        == 404
    )


# --- the Apply page ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_apply_page_lists_what_is_ready_oldest_posting_first(
    django_assert_num_queries: Any,
) -> None:
    user = make_user(with_resume=True)
    now = timezone.now()

    def ready(title: str, days: int, **fields: Any) -> Application:
        job = make_job(title=title, posted_at=now - timedelta(days=days))
        return make_application(
            user, make_match(user, job=job, fast_score=80), status="ready", **fields
        )

    ready("Posted yesterday", 1)
    ready("Posted last week", 7)
    ready("Posted today", 0)
    waiting = _waiting(user)
    make_application(user, status="preparing")
    make_application(user, status="dismissed")
    sent = ready("Sent already", 3, submitted_at=now)
    Application.objects.for_user(user.pk).filter(id=sent.id).update(status="submitted")
    make_application(make_user(), status="ready")  # someone else's

    client = signed_in(user)
    with django_assert_num_queries(3 + 2):  # the page's three, after the session and the user
        page = get_page(client, "/api/pages/apply").json()

    assert [item["title"] for item in page["ready"]] == [
        "Posted last week",
        "Posted yesterday",
        "Posted today",
    ]
    assert [item["id"] for item in page["needsAnswers"]] == [str(waiting.id)]
    assert (page["preparing"], page["failed"]) == (1, [])
    assert [item["title"] for item in page["sent"]] == ["Sent already"]
    assert page["sent"][0]["postedToAppliedS"] == 3 * 86400
    needs = page["needsAnswers"][0]
    assert needs["formRead"] is True and needs["company"] == "Figma"
    missing = [a["label"] for a in needs["answers"] if a["source"] == "missing" and a["required"]]
    assert "Why do you want to join Figma?" in missing

    # The match page says where its application stands.
    match_page = get_page(client, f"/api/pages/matches/{waiting.match_id}").json()
    stands = match_page["match"]["application"]
    assert (stands["id"], stands["status"], stands["missing"]) == (
        str(waiting.id),
        "needs_answers",
        len(missing),
    )
    assert (stands["submittedAt"], stands["sent"], stands["hasPicture"]) == (None, [], False)


@pytest.mark.django_db
def test_save_keeps_the_shared_answer_a_person_gave_first() -> None:
    """An answer on a form does not overwrite what the Profile page holds."""
    user = make_user(with_resume=True)
    save(user.pk, K.PREVIOUSLY_EMPLOYED, "no")
    application = _waiting(user)
    # Planned as "No" already; pin "Yes" for this wording only.
    key = next(
        a["key"]
        for a in Application.objects.for_user(user.pk).get(id=application.id).answers
        if a["kind"] == "previously_employed"
    )
    actions.answer(user.pk, application.id, key, "Yes")
    assert load(user.pk).get(K.PREVIOUSLY_EMPLOYED) == "no"
    planned = Application.objects.for_user(user.pk).get(id=application.id).answers
    assert next(a for a in planned if a["key"] == key)["value"] == "Yes"
