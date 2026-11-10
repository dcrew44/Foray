"""The browser extension's half of the server (#22, #23, #24): a token is its only way in, it is
given an application to fill and nothing else, it reports what it filled and the proof that the
form arrived, and Foray will not fill more than the user's limits allow."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.test import Client
from django.utils import timezone

from apps.applications import extension
from apps.applications.models import Application, ExtensionToken
from apps.core.storage import storage
from apps.matching.models import JobMatch
from foray.api import api
from tests.accounts.helpers import Api
from tests.applications.test_prepare import _greenhouse_match, _waiting
from tests.factories import (
    make_application,
    make_company,
    make_document,
    make_job,
    make_match,
    make_resume,
    make_user,
)
from tests.pages.conftest import get_page, signed_in

pytestmark = pytest.mark.django_db

EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class Extension:
    """A client that signs in the way the extension does: a bearer token, its own origin, and
    no cookie."""

    def __init__(self, token: str) -> None:
        self.client = Client()
        self.headers = {"Authorization": f"Bearer {token}", "Origin": EXTENSION_ORIGIN}

    def get(self, path: str) -> Any:
        return self.client.get(path, headers=self.headers)

    def post(self, path: str, body: Any) -> Any:
        return self.client.post(
            path, data=json.dumps(body), content_type="application/json", headers=self.headers
        )


def _token(user: Any) -> str:
    made = extension.new_token(user.pk, "Laptop")
    assert made is not None
    return made[1]


# --- signing in ----------------------------------------------------------------------------------


def test_a_token_is_shown_once_and_only_its_hash_is_kept() -> None:
    user = make_user()
    api_client = Api(signed_in(user))
    made = api_client.post("/api/extension-tokens", {"label": "  My   laptop "})
    assert made.status_code == 200
    body = made.json()
    assert body["token"].startswith("foray_ext_") and len(body["token"]) > 40
    row = ExtensionToken.objects.for_user(user.pk).get()
    assert row.label == "My laptop" and body["token"] not in (row.token_hash, row.label)
    assert extension.authenticate(body["token"]) == user
    assert extension.authenticate("foray_ext_wrong") is None
    assert extension.authenticate(body["token"].removeprefix("foray_ext_")) is None

    # Five is as many as anyone needs; removing one signs that extension out.
    for _ in range(extension.MAX_TOKENS - 1):
        assert api_client.post("/api/extension-tokens", {}).status_code == 200
    assert api_client.post("/api/extension-tokens", {}).status_code == 409
    removed = api_client.client.delete(
        f"/api/extension-tokens/{row.id}", headers={"Origin": "http://localhost:3000"}
    )
    assert removed.status_code == 200
    assert extension.authenticate(body["token"]) is None


def test_the_extensions_routes_take_a_token_and_nothing_else() -> None:
    user = make_user(name="Avery Quinn")
    token = _token(user)
    application = make_application(user, status="ready")

    assert Extension(token).get("/api/extension/me").json() == {
        "ok": True,
        "name": "Avery Quinn",
        "waiting": 1,
    }
    # No token, a wrong one, or a signed-in browser's session: none of them is the extension.
    assert Client().get("/api/extension/me").status_code == 401
    assert Extension("foray_ext_nobody").get("/api/extension/me").status_code == 401
    assert signed_in(user).get("/api/extension/me").status_code == 401

    # The extension's own origin is on no allow-list, and its POSTs go through all the same.
    path = f"/api/extension/applications/{application.id}/filled"
    assert Extension(token).post(path, {"filled": []}).status_code == 200
    # A request that also carries a session cookie is a browser's, and is refused as one.
    both = signed_in(user)
    refused = both.post(
        path,
        data="{}",
        content_type="application/json",
        headers={"Authorization": f"Bearer {token}", "Origin": EXTENSION_ORIGIN},
    )
    assert refused.status_code == 403
    assert django_settings.SESSION_COOKIE_NAME in both.cookies


def test_every_extension_route_refuses_another_users_application() -> None:
    owner, stranger = make_user(), make_user()
    application = make_application(owner, status="ready")
    theirs = Extension(_token(stranger))
    schema = api.get_openapi_schema(path_prefix="/api")
    swept = 0
    for path, operations in schema["paths"].items():
        if not path.startswith("/api/extension/") or "{id}" not in path:
            continue
        url = path.replace("{id}", str(application.id))
        for method in operations:
            # A body every route can read, so a refusal cannot be a 400.
            body = {"fields": []}
            response = theirs.get(url) if method == "get" else theirs.post(url, body)
            assert response.status_code == 404, f"{method} {path} answered {response.status_code}"
            swept += 1
    assert swept >= 5
    assert theirs.get("/api/extension/applications?url=https://example.test/").json() == {
        "application": None
    }
    untouched = Application.objects.for_user(owner.pk).get(id=application.id)
    assert (untouched.status, untouched.filled_at) == ("ready", None)


# --- what it is given ----------------------------------------------------------------------------


def test_the_payload_is_the_plan_with_the_resume_to_attach() -> None:
    user = make_user(with_resume=True)
    application = _waiting(user)
    ext = Extension(_token(user))

    body = ext.get(f"/api/extension/applications/{application.id}").json()
    assert (body["company"], body["formSource"], body["formRead"]) == ("Figma", "greenhouse", True)
    assert body["status"] == "needs_answers" and body["blocked"] is None
    assert body["maySubmit"] is False
    first = body["fields"][0]
    assert (first["key"], first["value"], first["source"]) == ("first_name", "Avery", "profile")
    assert "Why do you want to join Figma?" in body["missing"]
    assert body["resume"] is None  # no PDF has been made yet
    assert ext.get(f"/api/extension/applications/{application.id}/resume").status_code == 404

    pdf = make_document(user, kind="resume_pdf", filename="Avery Quinn - Resume.pdf")
    storage().put(pdf.storage_path, b"%PDF-1.7 the resume")
    make_resume(user, rendered_document=pdf)
    body = ext.get(f"/api/extension/applications/{application.id}").json()
    assert body["resume"] == {"filename": "Avery Quinn - Resume.pdf", "sizeBytes": pdf.size_bytes}
    file = ext.get(f"/api/extension/applications/{application.id}/resume")
    assert file.status_code == 200 and file.content == b"%PDF-1.7 the resume"
    assert "Avery Quinn - Resume.pdf" in file["Content-Disposition"]


@pytest.fixture(autouse=True)
def _own_storage(settings: Any, tmp_path: Path) -> None:
    settings.STORAGE_DIR = str(tmp_path / "storage")


def test_the_application_for_a_page_is_found_by_its_address_or_its_job_id() -> None:
    user = make_user(with_resume=True)
    application = _waiting(user)  # greenhouse:figma, job 6241542004
    ext = Extension(_token(user))

    def found(url: str) -> str | None:
        from urllib.parse import quote

        body = ext.get(f"/api/extension/applications?url={quote(url, safe='')}").json()
        return body["application"]["id"] if body["application"] else None

    assert found("https://job-boards.greenhouse.io/figma/jobs/6241542004") == str(application.id)
    # The same form embedded on the employer's own careers site.
    assert found("https://www.figma.com/careers/job/?gh_jid=6241542004") == str(application.id)
    # And inside the frame Greenhouse serves for it, which names the job as `token`.
    embedded = "https://job-boards.greenhouse.io/embed/job_app?for=figma&token=6241542004"
    assert found(embedded) == str(application.id)
    # A `token` anywhere else is not a job id.
    assert found("https://example.com/apply?token=6241542004") is None
    assert found("https://job-boards.greenhouse.io/figma/jobs/999") is None
    assert found("not a url") is None
    # One already sent is not waiting to be filled.
    Application.objects.for_user(user.pk).filter(id=application.id).update(status="submitted")
    assert found("https://job-boards.greenhouse.io/figma/jobs/6241542004") is None


def test_a_form_foray_could_not_read_ahead_is_planned_from_what_the_page_shows() -> None:
    user = make_user(with_resume=True)
    job = make_job(company=make_company(source_type="ashby"), title="Software Engineer")
    application = make_application(user, make_match(user, job=job), status="ready")
    ext = Extension(_token(user))

    planned = ext.post(
        f"/api/extension/applications/{application.id}/plan",
        {
            "fields": [
                {"key": "_systemfield_name", "label": "Name", "required": True},
                {"key": "_systemfield_email", "label": "Email", "required": True},
                {
                    "key": "q1",
                    "label": "Will you now or in the future require sponsorship?",
                    "type": "select",
                    "required": True,
                    "options": ["Yes", "No"],
                },
            ]
        },
    ).json()
    by_key = {field["key"]: field for field in planned["fields"]}
    assert by_key["_systemfield_name"]["source"] == "profile"
    assert (by_key["q1"]["value"], by_key["q1"]["source"]) == (None, "missing")
    assert planned["status"] == "needs_answers"
    stored = Application.objects.for_user(user.pk).get(id=application.id)
    assert [question["key"] for question in stored.questions] == [
        "_systemfield_name",
        "_systemfield_email",
        "q1",
    ]

    # A form that was read from its board keeps the board's account of itself.
    read = _waiting(user, match=_greenhouse_match(user))
    before = Application.objects.for_user(user.pk).get(id=read.id).questions
    ext.post(
        f"/api/extension/applications/{read.id}/plan", {"fields": [{"key": "x", "label": "X"}]}
    )
    assert Application.objects.for_user(user.pk).get(id=read.id).questions == before


# --- restraint (#24) -----------------------------------------------------------------------------


def _sent(user: Any, company: Any, hours_ago: float = 1) -> Application:
    application = make_application(
        user, make_match(user, job=make_job(company=company)), status="submitted"
    )
    Application.objects.for_user(user.pk).filter(id=application.id).update(
        submitted_at=timezone.now() - timezone.timedelta(hours=hours_ago)
    )
    return application


def test_foray_fills_only_so_many_applications_to_one_employer_and_in_a_day() -> None:
    user = make_user(settings={"applyPerEmployer": 2, "applyDailyCap": 3})
    acme, other = make_company(name="Acme"), make_company(name="Other")
    ext = Extension(_token(user))

    def blocked(company: Any) -> str | None:
        application = make_application(
            user, make_match(user, job=make_job(company=company)), status="ready"
        )
        return ext.get(f"/api/extension/applications/{application.id}").json()["blocked"]

    assert blocked(acme) is None
    _sent(user, acme, hours_ago=30)
    assert blocked(acme) is None  # one sent
    _sent(user, acme, hours_ago=30 * 24)
    reason = blocked(acme)
    assert reason is not None and reason.startswith(
        "You have sent 2 applications to Acme in the last 60 days"
    )
    assert blocked(other) is None  # the limit is per employer
    # Sixty-one days ago no longer counts.
    Application.objects.for_user(user.pk).filter(status="submitted").update(
        submitted_at=timezone.now() - timezone.timedelta(days=61)
    )
    assert blocked(acme) is None

    for _ in range(3):
        _sent(user, make_company(), hours_ago=2)
    daily = blocked(other)
    assert daily is not None and daily.startswith("You have sent 3 applications in the last 24")
    # Someone else's sending is theirs.
    stranger = make_user()
    assert extension.blocked_reason(stranger, make_application(stranger, status="ready")) is None


# --- proof of arrival (#23) ----------------------------------------------------------------------


def test_what_was_filled_and_the_confirmation_are_kept_for_good() -> None:
    user = make_user(with_resume=True)
    application = _waiting(user)
    ext = Extension(_token(user))
    base = f"/api/extension/applications/{application.id}"
    filled = [
        {"key": "first_name", "label": "First Name", "value": "Avery"},
        {"key": "question_1", "label": "Why Figma?", "value": "Because."},
    ]

    assert ext.post(f"{base}/filled", {"filled": filled}).json() == {"ok": True, "message": "Noted"}
    stored = Application.objects.for_user(user.pk).get(id=application.id)
    assert stored.sent_answers == filled
    assert stored.filled_at is not None and stored.opened_at is not None
    assert stored.status == "needs_answers"  # filling is not sending

    screenshot = "data:image/png;base64," + base64.b64encode(PNG).decode()
    sent = ext.post(
        f"{base}/submitted",
        {
            "url": "https://job-boards.greenhouse.io/figma/jobs/6241542004/confirmation",
            "text": "Thank you   for applying.\n\nWe have received your application.",
            "screenshot": screenshot,
        },
    )
    assert sent.json() == {"ok": True, "message": "Recorded. The match moved to Applied."}
    stored = Application.objects.for_user(user.pk).get(id=application.id)
    assert stored.status == "submitted" and stored.submitted_at is not None
    assert stored.confirmation_text == "Thank you for applying. We have received your application."
    assert stored.confirmation_url.endswith("/confirmation")
    assert stored.sent_answers == filled  # what went in stays as it was reported
    with storage().open(stored.confirmation_image) as handle:
        assert handle.read() == PNG
    assert stored.confirmation_image.startswith(f"{user.pk}/application_proof/")
    assert JobMatch.objects.for_user(user.pk).get(id=application.match_id).stage == "applied"

    # The match page shows the proof to its owner, and the picture is theirs alone.
    proof = get_page(signed_in(user), f"/api/pages/matches/{application.match_id}").json()["match"][
        "application"
    ]
    assert proof["status"] == "submitted" and proof["submittedAt"] is not None
    assert proof["confirmationText"].startswith("Thank you for applying.")
    assert (proof["hasPicture"], proof["autoSubmitted"]) == (True, False)
    assert proof["sent"] == [
        {"label": "First Name", "value": "Avery"},
        {"label": "Why Figma?", "value": "Because."},
    ]
    picture = signed_in(user).get(f"/api/applications/{application.id}/proof")
    assert picture.status_code == 200 and picture.content == PNG
    assert picture["Content-Type"] == "image/png"
    assert (
        signed_in(make_user()).get(f"/api/applications/{application.id}/proof").status_code == 404
    )

    # Reported twice (a reload of the confirming page), it is recorded once.
    again = ext.post(f"{base}/submitted", {"text": "Something else"})
    assert again.json() == {"ok": True, "message": "Already recorded"}
    assert Application.objects.for_user(user.pk).get(id=application.id).confirmation_text == (
        stored.confirmation_text
    )


@pytest.mark.parametrize(
    "screenshot",
    [
        "data:text/html;base64," + base64.b64encode(b"<script>").decode(),
        "data:image/png;base64,not-base64!",
        "data:image/png;base64," + base64.b64encode(b"GIF89a not a png").decode(),
        "https://example.test/picture.png",
    ],
)
def test_a_screenshot_that_is_not_a_picture_is_not_kept(screenshot: str) -> None:
    user = make_user()
    application = make_application(user, status="ready")
    ext = Extension(_token(user))
    response = ext.post(
        f"/api/extension/applications/{application.id}/submitted", {"screenshot": screenshot}
    )
    assert response.status_code == 200
    stored = Application.objects.for_user(user.pk).get(id=application.id)
    assert stored.status == "submitted" and stored.confirmation_image == ""


# --- submitting on its own (#40) -----------------------------------------------------------------


def _field(key: str, label: str, value: str | None, source: str, required: bool = True) -> Any:
    return {
        "key": key,
        "label": label,
        "type": "file" if source == "file" else "text",
        "required": required,
        "options": [],
        "kind": "custom",
        "value": value,
        "source": source,
    }


OWN = [
    _field("first_name", "First name", "Avery", "profile"),
    _field("resume", "Resume", "resume", "file"),
    _field("q1", "Sponsorship?", "No", "saved"),
    _field("gender", "Gender", "Decline", "declined", required=False),
    _field("nick", "Preferred name", None, "skipped", required=False),
]


def _automated(user: Any, *, lane: str | None = "automated", **fields: Any) -> Application:
    match = make_match(user, job=make_job(), lane_override=lane)
    fields.setdefault("status", "ready")
    fields.setdefault("answers", OWN)
    return make_application(user, match, **fields)


def test_foray_presses_submit_only_when_the_setting_is_on_and_every_answer_is_the_users() -> None:
    off = make_user()
    assert extension.may_submit(off, _automated(off)) is False  # off unless turned on
    autopilot = make_user(settings={"defaultMode": "autopilot"})
    assert extension.may_submit(autopilot, _automated(autopilot)) is False  # autopilot is not it

    user = make_user(settings={"autoSubmit": True, "applyPerEmployer": 1})
    assert extension.may_submit(user, _automated(user)) is True
    # Anything else still stops for the user.
    assert extension.may_submit(user, _automated(user, lane="coached")) is False
    assert extension.may_submit(user, _automated(user, lane="none")) is False
    assert extension.may_submit(user, _automated(user, lane=None)) is False  # no review, no lane
    assert extension.may_submit(user, _automated(user, status="needs_answers")) is False
    generated = [*OWN, _field("why", "Why us?", "A drafted answer.", "generated")]
    assert extension.may_submit(user, _automated(user, answers=generated)) is False
    missing = [*OWN, _field("why", "Why us?", None, "missing")]
    assert extension.may_submit(user, _automated(user, answers=missing)) is False
    assert extension.may_submit(user, _automated(user, answers=[])) is False  # a form not read
    # Within the limits (#24): a second application to an employer already sent to is not.
    first = _automated(user)
    company = first.match.job.company
    assert extension.record_submitted(user.pk, first.id, auto=True) is True
    again = make_application(
        user,
        make_match(user, job=make_job(company=company), lane_override="automated"),
        status="ready",
        answers=OWN,
    )
    assert extension.may_submit(user, again) is False

    # The payload carries the same answer.
    ext = Extension(_token(user))
    fresh = _automated(user)
    assert ext.get(f"/api/extension/applications/{fresh.id}").json()["maySubmit"] is True
    assert ext.get(f"/api/extension/applications/{again.id}").json()["maySubmit"] is False


def test_an_application_sent_on_its_own_says_so_in_the_activity_feed() -> None:
    from apps.runs.models import Event

    user = make_user(settings={"autoSubmit": True})
    allowed = _automated(user)
    not_allowed = _automated(user, lane="coached")
    ext = Extension(_token(user))

    ext.post(f"/api/extension/applications/{allowed.id}/submitted", {"auto": True})
    # The extension's word alone is not enough: this one was the user's to send.
    ext.post(f"/api/extension/applications/{not_allowed.id}/submitted", {"auto": True})

    rows = {row.id: row.auto_submitted for row in Application.objects.for_user(user.pk).all()}
    assert rows == {allowed.id: True, not_allowed.id: False}
    messages = list(
        Event.objects.for_user(user.pk)
        .filter(kind="application.submitted")
        .order_by("id")
        .values_list("message", flat=True)
    )
    assert messages[0].startswith("Foray sent your application to ")
    assert messages[0].endswith(" on its own.")
    assert messages[1].startswith("You sent your application to ")


# --- which resume it sends (#61) -----------------------------------------------------------------


def test_each_application_sends_the_variant_its_title_asks_for_and_the_user_can_switch() -> None:
    from apps.applications import resume
    from tests.factories import make_variant

    user = make_user(with_resume=True)
    backend = make_variant(user, name="Backend", keywords=["backend", "api"])
    data = make_variant(user, name="Data", keywords=["data", "analytics"])

    def planned(title: str) -> Application:
        application = make_application(
            user, make_match(user, job=make_job(title=title)), status="ready"
        )
        resume.assign(user.pk, application)
        return (
            Application.objects.for_user(user.pk).select_related("variant").get(id=application.id)
        )

    backend_role = planned("Backend Engineer, Payments")
    data_role = planned("Data Engineer")
    other_role = planned("Product Designer")
    assert (backend_role.variant, backend_role.resume_reason) == (
        backend,
        "Chosen because the title says “backend”.",
    )
    assert data_role.variant == data
    assert (other_role.variant, other_role.resume_reason) == (
        None,
        "Your full resume: no variant matched.",
    )

    # The Apply page names the choice and offers the others.
    page = get_page(signed_in(user), "/api/pages/apply").json()
    by_title = {item["title"]: item["resume"] for item in page["ready"]}
    assert by_title["Backend Engineer, Payments"]["name"] == "Backend"
    assert by_title["Product Designer"] == {
        "variantId": None,
        "name": "Full resume",
        "reason": "Your full resume: no variant matched.",
        "choice": "",
    }
    assert [variant["name"] for variant in page["variants"]] == ["Backend", "Data"]

    # The user switches the backend application to the data variant, then to the full resume,
    # then back to automatic.
    api_client = Api(signed_in(user))
    path = f"/api/applications/{backend_role.id}/resume"
    assert api_client.put(path, {"choice": str(data.id)}).status_code == 200
    switched = Application.objects.for_user(user.pk).get(id=backend_role.id)
    assert (switched.variant_id, switched.resume_reason) == (data.id, "You chose this variant.")
    assert api_client.put(path, {"choice": "base"}).status_code == 200
    assert Application.objects.for_user(user.pk).get(id=backend_role.id).variant_id is None
    assert api_client.put(path, {"choice": ""}).status_code == 200
    assert Application.objects.for_user(user.pk).get(id=backend_role.id).variant_id == backend.id
    # A variant that is not theirs, or nonsense, changes nothing.
    stranger_variant = make_variant(make_user(with_resume=True), name="Theirs")
    assert api_client.put(path, {"choice": str(stranger_variant.id)}).status_code == 404
    assert api_client.put(path, {"choice": "nonsense"}).status_code == 404


def test_the_pdf_the_extension_attaches_is_the_chosen_variants_own() -> None:
    from apps.applications import resume
    from apps.resumes import variants
    from tests.factories import make_variant

    user = make_user(with_resume=True)
    backend = make_variant(user, name="Backend", keywords=["backend"])
    application = make_application(
        user, make_match(user, job=make_job(title="Backend Engineer")), status="ready"
    )
    resume.assign(user.pk, application)
    ext = Extension(_token(user))

    # Nothing has been rendered yet: no resume to attach.
    assert ext.get(f"/api/extension/applications/{application.id}").json()["resume"] is None

    # The full resume's PDF exists, but this application sends the Backend variant.
    base_pdf = make_document(user, kind="resume_pdf", filename="Avery Quinn - Resume.pdf")
    storage().put(base_pdf.storage_path, b"%PDF-1.7 the full resume")
    from apps.resumes.models import Resume

    Resume.objects.for_user(user.pk).filter(kind="base").update(rendered_document=base_pdf)
    assert ext.get(f"/api/extension/applications/{application.id}").json()["resume"] is None

    snapshot = variants.snapshot(user.pk, backend.id)
    variant_pdf = make_document(
        user, kind="resume_pdf", filename="Avery Quinn - Resume - Backend.pdf"
    )
    storage().put(variant_pdf.storage_path, b"%PDF-1.7 the backend variant")
    Resume.objects.for_user(user.pk).filter(id=snapshot.id).update(rendered_document=variant_pdf)

    body = ext.get(f"/api/extension/applications/{application.id}").json()
    assert body["resume"]["filename"] == "Avery Quinn - Resume - Backend.pdf"
    file = ext.get(f"/api/extension/applications/{application.id}/resume")
    assert file.content == b"%PDF-1.7 the backend variant"

    # Switched to the full resume, it attaches that one, and what was attached is recorded.
    assert resume.set_choice(user.pk, application.id, "base") is True
    file = ext.get(f"/api/extension/applications/{application.id}/resume")
    assert file.content == b"%PDF-1.7 the full resume"
    ext.post(f"/api/extension/applications/{application.id}/filled", {"filled": []})
    assert Application.objects.for_user(user.pk).get(id=application.id).document_id == base_pdf.id
