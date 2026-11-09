"""Resume variants (#61): what a variant composes to, which one a posting's title chooses, the
`/api/variants` endpoints, and a variant's own PDF.

A variant stores no fact: it selects, orders and rewords the newest base resume's items by their
ids. So the tests of `compose` are about what the base decides (every fact) and what the variant
decides (order, selection, wording), and the rendering tests about the snapshot row a PDF is made
from: written once per content, and reused by `render_hash` when nothing changed.

Rendering goes through the real `render.render_resume` with only the browser faked
(`render_fitted`), as in `test_render.py`; the `slow` test prints a variant with Chromium.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytest
from django.test import Client
from playwright.async_api import Error as PlaywrightError

from apps.accounts.models import User
from apps.resumes import pipeline, variants
from apps.resumes import render as rendering
from apps.resumes.models import Document, ResumeVariant
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.parse.pdf import parse_pdf
from apps.resumes.render import FittedPdf, render_hash
from apps.resumes.schema import Resume
from apps.resumes.variants import (
    FULL_RESUME,
    VariantItem,
    VariantRefused,
    VariantSpec,
    choose_variant,
    clean_spec,
    compose,
    full_spec,
    matched_keywords,
    resume_for,
    resume_row,
    selects_nothing,
    snapshot,
)
from apps.runs.models import Run
from tests.factories import make_resume, make_user, make_variant, sample_resume_data
from tests.pages.conftest import ORIGIN, get_page, signed_in
from tests.queue import arun_deferred, run_deferred


def sample() -> Resume:
    return Resume.model_validate(sample_resume_data())


def ids(items: list[Any]) -> list[str]:
    return [item.id for item in items]


# --- compose -------------------------------------------------------------------------------------


def test_a_new_variant_is_the_whole_base() -> None:
    base = sample()
    spec = full_spec(base)
    assert compose(base, spec) == base
    assert [entry.highlights for entry in [*spec.work, *spec.projects]] == [None] * 4
    assert (spec.headline, spec.summary) == (None, None)


def test_a_variant_orders_selects_and_rewords_what_the_base_says() -> None:
    base = sample()
    lumen, harbor, fernwood = base.work
    berkeley, laney = base.education
    python, go, sql = base.skills[:3]
    spec = VariantSpec(
        headline="Data engineer",
        summary="Six years of pipelines.",
        work=[
            VariantItem(id=harbor.id, highlights=["Loaded 40 million events a day."]),
            VariantItem(id=lumen.id),
        ],
        projects=[],
        education=[berkeley.id],
        skills=[sql.id, python.id],
    )
    composed = compose(base, spec)

    # Its order and its selection.
    assert ids(composed.work) == [harbor.id, lumen.id]
    assert fernwood.id not in ids(composed.work)
    assert composed.projects == []
    assert ids(composed.education) == [berkeley.id] and laney.id not in ids(composed.education)
    assert [skill.name for skill in composed.skills] == ["SQL", "Python"]
    assert go.id not in ids(composed.skills)
    # Its wording, where it gave any; the base's where it did not.
    assert composed.basics.headline == "Data engineer"
    assert composed.summary == "Six years of pipelines."
    assert composed.work[0].highlights == ["Loaded 40 million events a day."]
    assert composed.work[1].highlights == lumen.highlights
    # Every fact is the base's: a variant has nowhere to say otherwise.
    assert composed.basics.model_copy(update={"headline": base.basics.headline}) == base.basics
    assert composed.work[0].model_copy(update={"highlights": harbor.highlights}) == harbor
    assert composed.work[1] == lumen
    assert composed.education == [berkeley]
    # The base itself is left as it was.
    assert base == sample()


def test_an_id_the_base_no_longer_has_is_dropped_and_a_repeat_counts_once() -> None:
    base = sample()
    lumen = base.work[0]
    spec = VariantSpec(
        work=[
            VariantItem(id="gone"),
            VariantItem(id=lumen.id),
            VariantItem(id=lumen.id, highlights=["A second mention."]),
        ],
        projects=[VariantItem(id="also-gone")],
        education=["nope", base.education[1].id, base.education[1].id],
        skills=["nope", base.skills[0].id],
    )
    composed = compose(base, spec)
    assert composed.work == [lumen]
    assert composed.projects == []
    assert composed.education == [base.education[1]]
    assert composed.skills == [base.skills[0]]


def test_a_variant_follows_the_base_when_the_base_is_edited() -> None:
    base = sample()
    lumen, harbor, _ = base.work
    spec = VariantSpec(
        work=[
            VariantItem(id=harbor.id, highlights=["My own wording."]),
            VariantItem(id=lumen.id),
        ],
        education=ids(base.education),
        skills=ids(base.skills)[:2],
    )
    edited = base.model_copy(deep=True)
    edited.basics.name = "Avery Q. Quinn"
    edited.basics.email = "avery@example.test"
    edited.work[0].position = "Staff Software Engineer"  # lumen: a corrected title
    edited.work[0].highlights.append("Shipped the ledger rewrite.")  # and a new bullet
    edited.work[1].end_date = "March 2023"  # harbor: a corrected date
    edited.skills[0].name = "Python 3"
    new_role = lumen.model_copy(update={"id": "new-role", "company": "Newco"})
    edited.work.insert(0, new_role)
    edited.education.pop()  # the second school is gone

    composed = compose(edited, spec)
    assert (composed.basics.name, composed.basics.email) == (
        "Avery Q. Quinn",
        "avery@example.test",
    )
    by_id = {item.id: item for item in composed.work}
    # The role it left to the base has the base's new title and bullets.
    assert by_id[lumen.id].position == "Staff Software Engineer"
    assert by_id[lumen.id].highlights[-1] == "Shipped the ledger rewrite."
    # The role it reworded keeps its wording and still takes the corrected fact.
    assert by_id[harbor.id].highlights == ["My own wording."]
    assert by_id[harbor.id].end_date == "March 2023"
    assert composed.skills[0].name == "Python 3"
    # A role added to the base is not in the variant until it is chosen, and what the base
    # dropped is dropped.
    assert "new-role" not in by_id
    assert ids(composed.education) == [base.education[0].id]


def test_a_spec_is_stored_clean() -> None:
    base = sample()
    lumen, harbor, fernwood = base.work
    cleaned = clean_spec(
        base,
        VariantSpec(
            headline="   ",
            summary="  Pipelines.  ",
            work=[
                VariantItem(id=lumen.id, highlights=list(lumen.highlights)),  # the base's own
                VariantItem(id="gone"),
                VariantItem(id=harbor.id, highlights=["  Mine.  ", "   "]),
                VariantItem(id=fernwood.id, highlights=[]),  # shown without bullets
                VariantItem(id=lumen.id),
            ],
            education=["nope", base.education[0].id, base.education[0].id],
            skills=[base.skills[1].id, "nope"],
        ),
    )
    assert (cleaned.headline, cleaned.summary) == (None, "Pipelines.")
    assert cleaned.work == [
        VariantItem(id=lumen.id, highlights=None),  # so it keeps following the base
        VariantItem(id=harbor.id, highlights=["Mine."]),
        VariantItem(id=fernwood.id, highlights=[]),
    ]
    assert cleaned.education == [base.education[0].id]
    assert cleaned.skills == [base.skills[1].id]
    assert not selects_nothing(compose(base, cleaned))
    assert selects_nothing(compose(base, VariantSpec(headline="Only words", work=[])))
    # Stored under plain keys, and read back as written.
    assert set(cleaned.dump()) == {
        "headline",
        "summary",
        "work",
        "projects",
        "education",
        "skills",
    }
    assert VariantSpec.read(cleaned.dump()) == cleaned


# --- choosing ------------------------------------------------------------------------------------

BACKEND = ["backend", "api", "platform", "infrastructure"]
DATA = ["data", "analytics", "machine learning", "ML"]
T0 = datetime(2026, 11, 1, tzinfo=UTC)


def variant(name: str, keywords: list[str], *, default: bool = False, age: int = 0) -> Any:
    """An unsaved variant, `age` minutes younger than the oldest."""
    return ResumeVariant(
        name=name,
        keywords=keywords,
        spec={},
        is_default=default,
        created_at=T0 + timedelta(minutes=age),
    )


@pytest.mark.parametrize(
    ("keywords", "title", "said"),
    [
        (["backend"], "Senior Backend Engineer", ["backend"]),
        (["Backend"], "BACKEND ENGINEER (REMOTE)", ["Backend"]),  # any capitals
        (["backend"], "Software Engineer, Backend/API", ["backend"]),
        # A whole word, never part of a longer one.
        (["backend"], "Backendless Developer", []),
        (["data"], "Database Administrator", []),
        (["java"], "JavaScript Engineer", []),
        (["ml"], "HTML Email Developer", []),
        # A multi-word keyword is a phrase.
        (["data engineer"], "Senior Data Engineer II", ["data engineer"]),
        (["data engineer"], "Data Platform Engineer", []),
        (["data engineer"], "Engineer, Data", []),
        (["machine learning"], "Machine Learning Engineer", ["machine learning"]),
        # Breaks inside a word do not count: one word however the posting hyphenates it.
        (["backend"], "Back-End Engineer", ["backend"]),
        (["backend"], "Back End Developer", ["backend"]),
        (["back-end"], "Backend Developer", ["back-end"]),
        (["full stack"], "Fullstack Engineer", ["full stack"]),
        (["machine learning"], "Machine-Learning Scientist", ["machine learning"]),
        (["devops"], "Dev Ops Engineer", ["devops"]),
        # Languages keep their marks.
        (["c++"], "C++ Systems Engineer", ["c++"]),
        (["c++"], "C Developer", []),
        (["c"], "C++ Developer", []),
        (["c#"], "C#/.NET Developer", ["c#"]),
        ([".net"], "C#/.NET Developer", [".net"]),
        # One keyword spelled twice is one keyword; each is reported as the user wrote it.
        (["backend", "back-end", "API"], "Backend API Engineer", ["backend", "API"]),
        (["  ", "---"], "Backend Engineer", []),
        (["backend"], "", []),
    ],
)
def test_a_keyword_is_said_as_whole_words(keywords: list[str], title: str, said: list[str]):
    assert matched_keywords(keywords, title) == said


def test_the_variant_whose_keywords_the_title_says_most_is_chosen() -> None:
    backend, data = variant("Backend", BACKEND), variant("Data", DATA, age=1)
    both = [backend, data]
    assert choose_variant(both, "Backend Engineer") is backend
    assert choose_variant(both, "Data Engineer") is data
    assert choose_variant(both, "Senior Software Engineer, API Platform") is backend
    assert choose_variant(both, "Machine Learning Engineer, Analytics") is data
    # Two of one against one of the other.
    assert choose_variant(both, "Backend Engineer, Data Platform") is backend
    assert choose_variant(both, "ML Platform Engineer, Data") is data
    # The order they are given in does not decide it.
    assert choose_variant([data, backend], "Backend Engineer") is backend


def test_a_tie_goes_to_the_default_and_then_to_the_oldest() -> None:
    backend, data = variant("Backend", BACKEND, age=5), variant("Data", DATA, age=1)
    title = "Data Platform Engineer"  # one keyword each
    assert choose_variant([backend, data], title) is data  # the older one
    assert choose_variant([data, backend], title) is data
    backend.is_default = True
    assert choose_variant([backend, data], title) is backend  # the default is among them
    # A default that is not in the tie does not win it.
    general = variant("General", ["engineer"], default=True, age=9)
    backend.is_default = False
    assert choose_variant([general, backend, data], "Data Platform Lead") is data


def test_a_title_that_says_none_of_them_gets_the_default_or_the_base() -> None:
    backend, data = variant("Backend", BACKEND), variant("Data", DATA, age=1)
    assert choose_variant([backend, data], "Product Designer") is None  # the base resume
    data.is_default = True
    assert choose_variant([backend, data], "Product Designer") is data
    assert choose_variant([backend, data], "") is data
    assert choose_variant([], "Backend Engineer") is None
    # A variant with no keywords is only ever the default.
    plain = variant("Plain", [], age=2)
    assert choose_variant([plain], "Backend Engineer") is None


# --- the acceptance check ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _storage_dir(settings: Any, tmp_path: Path) -> Path:
    settings.STORAGE_DIR = str(tmp_path / "storage")
    return tmp_path / "storage"


def blank_pdf(width: int) -> bytes:
    """A real one-page PDF, different for each width (a reused render counts its pages)."""
    document = pdfium.PdfDocument.new()
    try:
        document.new_page(width, 792).close()
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()
    finally:
        document.close()


@pytest.fixture
def renders(monkeypatch: pytest.MonkeyPatch) -> list[Resume]:
    """The browser, faked: `render_fitted` answers a distinct blank page and records the resume
    it was asked to print. Everything around it (the render hash, the stored file, the document
    row) is the real `render.render_resume`."""
    printed: list[Resume] = []

    async def fitted(resume: Resume) -> FittedPdf:
        printed.append(resume)
        return FittedPdf(pdf=blank_pdf(612 + len(printed)), pages=1, density="normal")

    monkeypatch.setattr(rendering, "render_fitted", fitted)
    return printed


def two_variants(user: User) -> tuple[ResumeVariant, ResumeVariant]:
    """A backend variant that leads with the payments role, and a data variant that leads with
    the analytics role in its own words."""
    base = sample()
    lumen, harbor, fernwood = base.work
    skills = {skill.name: skill.id for skill in base.skills}
    backend = make_variant(
        user,
        name="Backend",
        keywords=BACKEND,
        spec=VariantSpec(
            headline="Backend engineer, payments",
            work=[VariantItem(id=lumen.id), VariantItem(id=harbor.id)],
            projects=[VariantItem(id=base.projects[0].id)],
            education=[base.education[0].id],
            skills=[skills[name] for name in ("Python", "Go", "PostgreSQL", "Kafka")],
        ),
    )
    data = make_variant(
        user,
        name="Data",
        keywords=DATA,
        spec=VariantSpec(
            headline="Data engineer",
            work=[
                VariantItem(
                    id=harbor.id,
                    highlights=["Loaded 40 million events a day into PostgreSQL and BigQuery."],
                ),
                VariantItem(id=fernwood.id),
            ],
            education=[base.education[0].id],
            skills=[skills[name] for name in ("SQL", "Python", "BigQuery")],
        ),
    )
    return backend, data


@pytest.mark.django_db
def test_the_right_variant_is_chosen_for_a_backend_and_a_data_posting_and_can_be_switched():
    user = make_user(with_resume=True)
    backend, data = two_variants(user)

    for_backend = resume_for(user.pk, "Senior Backend Engineer, Payments API")
    assert for_backend is not None and for_backend.variant == backend
    assert for_backend.reason == "Chosen because the title says “backend” and “api”."
    assert for_backend.resume.basics.headline == "Backend engineer, payments"
    assert [role.company for role in for_backend.resume.work] == [
        "Lumen Ledger",
        "Harbor Analytics",
    ]

    for_data = resume_for(user.pk, "Data Engineer")
    assert for_data is not None and for_data.variant == data
    assert for_data.reason == "Chosen because the title says “data”."
    assert [role.company for role in for_data.resume.work] == ["Harbor Analytics", "Fernwood Labs"]
    assert for_data.resume.work[0].highlights == [
        "Loaded 40 million events a day into PostgreSQL and BigQuery."
    ]
    # The same facts under both: one name, one employer's dates.
    assert for_data.resume.basics.name == for_backend.resume.basics.name == "Avery Quinn"
    assert for_data.resume.work[0].start_date == for_backend.resume.work[1].start_date

    # The user switches it: the other variant, or the full resume.
    switched = resume_for(user.pk, "Data Engineer", backend.id)
    assert switched is not None and switched.variant == backend
    assert switched.reason == "You chose this variant."
    assert resume_for(user.pk, "Data Engineer", str(backend.id)) == switched
    full = resume_for(user.pk, "Data Engineer", FULL_RESUME)
    assert full is not None and (full.variant, full.reason) == (None, "You chose your full resume.")
    assert full.resume == sample()
    # A choice that no longer exists falls back to the title.
    for stale in (uuid.uuid4(), "not-an-id"):
        again = resume_for(user.pk, "Data Engineer", stale)
        assert again is not None and again.variant == data

    # A title that says neither: the full resume, until one of them is the default.
    neither = resume_for(user.pk, "Product Designer")
    assert neither is not None and neither.variant is None
    assert (neither.reason, neither.resume) == ("Your full resume: no variant matched.", sample())
    variants.update_variant(user.pk, data.id, is_default=True)
    by_default = resume_for(user.pk, "Product Designer")
    assert by_default is not None and by_default.variant == data
    assert by_default.reason == "Your default variant."

    # Someone with no variants, and someone with no resume.
    plain = resume_for(make_user(with_resume=True).pk, "Backend Engineer")
    assert plain is not None and (plain.variant, plain.reason) == (
        None,
        "Your full resume: you have no variants yet.",
    )
    assert resume_for(make_user().pk, "Backend Engineer") is None


@pytest.mark.django_db(transaction=True)
def test_the_pdf_sent_is_the_chosen_variants_own_render(renders: list[Resume]) -> None:
    user = make_user(with_resume=True)
    backend, data = two_variants(user)
    base_row = ResumeRow.objects.for_user(user.pk).get(kind="base")
    filenames: dict[str, str] = {}
    for title in ("Backend Engineer", "Analytics Engineer", "Product Designer"):
        chosen = resume_for(user.pk, title)
        assert chosen is not None
        row = resume_row(user.pk, chosen.variant)
        assert row is not None
        result = asyncio.run(rendering.render_resume(user.pk, row.id))
        document = Document.objects.for_user(user.pk).get(id=result.document_id)
        filenames[title] = document.filename
        assert renders[-1] == chosen.resume  # what was printed is what was chosen
        assert Resume.model_validate(row.data) == chosen.resume
        if chosen.variant is None:
            assert row == base_row
        else:
            assert (row.kind, row.variant_id) == ("variant", chosen.variant.id)
    assert filenames == {
        "Backend Engineer": "Avery Quinn - Resume - Backend.pdf",
        "Analytics Engineer": "Avery Quinn - Resume - Data.pdf",
        "Product Designer": "Avery Quinn - Resume.pdf",
    }
    assert Document.objects.for_user(user.pk).filter(kind="resume_pdf").count() == 3
    assert {
        row.variant_id for row in ResumeRow.objects.for_user(user.pk).filter(kind="variant")
    } == {
        backend.id,
        data.id,
    }


@pytest.mark.django_db
def test_a_variant_that_selects_nothing_from_a_new_upload_is_never_sent() -> None:
    """A new file is read from scratch, so every item gets a new id and the variants made from
    the old one name nothing in it."""
    user = make_user(with_resume=True)
    backend, _ = two_variants(user)
    variants.update_variant(user.pk, backend.id, is_default=True)
    reread = sample_resume_data()
    for section in ("work", "education", "projects", "skills"):
        for item in reread[section]:
            item["id"] = str(uuid.uuid4())
    make_resume(user, data=reread)  # base version 2

    chosen = resume_for(user.pk, "Backend Engineer")
    assert chosen is not None and chosen.variant is None
    assert chosen.reason == "Your full resume: no variant matched."
    assert chosen.resume == Resume.model_validate(reread)
    forced = resume_for(user.pk, "Backend Engineer", backend.id)
    assert forced is not None and forced.variant is None
    with pytest.raises(VariantRefused, match="selects nothing from your current resume"):
        snapshot(user.pk, backend.id)
    assert not ResumeRow.objects.for_user(user.pk).filter(kind="variant").exists()


# --- the endpoints -------------------------------------------------------------------------------


class Browser:
    """A signed-in client: JSON bodies, with the browser's Origin on unsafe methods."""

    def __init__(self, user: User) -> None:
        self.user = user
        self.client: Client = signed_in(user)

    def send(self, method: str, path: str, body: Any = None) -> Any:
        return getattr(self.client, method)(
            path,
            data=json.dumps({} if body is None else body),
            content_type="application/json",
            headers={"Origin": ORIGIN},
        )

    def create(self, name: str, keywords: list[str] | None = None) -> Any:
        return self.send("post", "/api/variants", {"name": name, "keywords": keywords or []})

    def patch(self, variant_id: Any, body: dict[str, Any]) -> Any:
        return self.send("patch", f"/api/variants/{variant_id}", body)

    def delete(self, variant_id: Any) -> Any:
        return self.send("delete", f"/api/variants/{variant_id}")

    def render(self, variant_id: Any) -> Any:
        return self.send("post", f"/api/variants/{variant_id}/render")

    def listed(self) -> list[dict[str, Any]]:
        return get_page(self.client, "/api/pages/profile").json()["variants"]


def refused(response: Any) -> tuple[int, str]:
    body = response.json()
    assert body["ok"] is False and set(body) == {"ok", "message"}
    return response.status_code, body["message"]


@pytest.mark.django_db
def test_a_variant_is_created_as_everything_and_listed_on_the_profile_page() -> None:
    browser = Browser(make_user(with_resume=True))
    assert browser.listed() == []
    response = browser.create("  Backend   roles ", [" backend ", "API", "api", "Back-End", " "])
    assert response.status_code == 200, response.content
    body = response.json()
    assert body == {"ok": True, "id": body["id"], "message": "Variant created"}

    [listed] = browser.listed()
    base = sample()
    assert listed == {
        "id": body["id"],
        "name": "Backend roles",
        # Trimmed, and each once however it is capitalised or hyphenated.
        "keywords": ["backend", "API"],
        "isDefault": False,
        "spec": full_spec(base).dump(),
        "pdf": None,
        "updatedAt": listed["updatedAt"],
    }
    stored = ResumeVariant.objects.for_user(browser.user.pk).get()
    assert compose(base, VariantSpec.model_validate(stored.spec)) == base


@pytest.mark.django_db
def test_creating_a_variant_is_refused_in_a_sentence() -> None:
    browser = Browser(make_user(with_resume=True))
    assert browser.create("Backend", ["backend"]).status_code == 200
    # One name per user, however it is capitalised.
    assert refused(browser.create("  BACKEND ")) == (
        409,
        "You already have a variant called “BACKEND”.",
    )
    assert refused(browser.create("   ")) == (400, "Give the variant a name.")
    assert refused(browser.create("x" * 61)) == (
        400,
        "A variant's name can be 60 characters at most.",
    )
    assert refused(browser.create("Data", ["k" * 41])) == (
        400,
        "A keyword can be 40 characters at most.",
    )
    assert refused(browser.create("Data", [f"word{n}" for n in range(21)])) == (
        400,
        "A variant can have 20 keywords at most.",
    )
    for unreadable in ({"keywords": ["data"]}, {"name": 7}, {"name": "Data", "keywords": "data"}):
        assert refused(browser.send("post", "/api/variants", unreadable)) == (
            400,
            "That variant could not be read.",
        )
    # Six at most.
    for name in ("Data", "Platform", "Frontend", "Mobile", "Security"):
        assert browser.create(name).status_code == 200
    assert refused(browser.create("One more")) == (
        409,
        "You can keep up to 6 variants. Delete one to add another.",
    )
    assert ResumeVariant.objects.for_user(browser.user.pk).count() == 6
    # Someone else's names and count are their own.
    assert Browser(make_user(with_resume=True)).create("Backend").status_code == 200

    # Without a resume there is nothing to make a variant of.
    assert refused(Browser(make_user()).create("Backend")) == (
        422,
        "Upload a resume before making a variant.",
    )
    assert Client().post("/api/variants", data="{}", content_type="application/json").json() == {
        "ok": False,
        "message": "Not signed in.",
    }


@pytest.mark.django_db
def test_a_variant_is_changed_a_field_at_a_time() -> None:
    browser = Browser(make_user(with_resume=True))
    backend = browser.create("Backend", ["backend"]).json()["id"]
    data = browser.create("Data", ["data"]).json()["id"]
    base = sample()
    lumen, harbor, fernwood = base.work

    # The spec: an order, a selection and its own bullets.
    spec = {
        "headline": "  Data engineer ",
        "summary": None,
        "work": [
            {"id": harbor.id, "highlights": ["Built the ingestion service.", "  "]},
            {"id": lumen.id, "highlights": lumen.highlights},  # untouched: stays the base's
            {"id": "not-in-the-resume"},
        ],
        "projects": [],
        "education": [base.education[0].id],
        "skills": [base.skills[2].id, base.skills[0].id],
    }
    response = browser.patch(data, {"spec": spec})
    assert (response.status_code, response.json()) == (
        200,
        {"ok": True, "id": data, "message": "Variant saved"},
    )
    by_id = {item["id"]: item for item in browser.listed()}
    assert by_id[data]["spec"] == {
        "headline": "Data engineer",
        "summary": None,
        "work": [
            {"id": harbor.id, "highlights": ["Built the ingestion service."]},
            {"id": lumen.id, "highlights": None},
        ],
        "projects": [],
        "education": [base.education[0].id],
        "skills": [base.skills[2].id, base.skills[0].id],
    }
    assert (by_id[data]["name"], by_id[data]["keywords"]) == ("Data", ["data"])  # left alone
    assert fernwood.id not in json.dumps(by_id[data]["spec"])

    # The name and the keywords.
    assert (
        browser.patch(data, {"name": "Data and ML", "keywords": ["data", "ML"]}).status_code == 200
    )
    assert browser.patch(data, {"name": "data and ml"}).status_code == 200  # its own name, recased
    by_id = {item["id"]: item for item in browser.listed()}
    assert (by_id[data]["name"], by_id[data]["keywords"]) == ("data and ml", ["data", "ML"])
    assert by_id[data]["spec"]["headline"] == "Data engineer"  # the spec was left alone
    assert refused(browser.patch(data, {"name": "backend"})) == (
        409,
        "You already have a variant called “backend”.",
    )
    assert refused(browser.patch(data, {"name": " "})) == (400, "Give the variant a name.")

    # The default moves: one at most.
    assert browser.patch(backend, {"isDefault": True}).status_code == 200
    assert browser.patch(data, {"isDefault": True}).status_code == 200
    assert {item["name"]: item["isDefault"] for item in browser.listed()} == {
        "Backend": False,
        "data and ml": True,
    }
    assert browser.patch(data, {"isDefault": False}).status_code == 200
    assert not any(item["isDefault"] for item in browser.listed())

    # A spec that names nothing in the resume is refused, and nothing changes.
    for nothing in (
        {"work": [], "projects": [], "education": [], "skills": []},
        {"headline": "Only words"},
        {"work": [{"id": "not-in-the-resume"}], "skills": ["nor-this"]},
    ):
        assert refused(browser.patch(data, {"spec": nothing})) == (
            400,
            "A variant needs at least one role, project, school or skill from your resume.",
        )
    for unreadable in ({"spec": {"work": "all"}}, {"spec": []}, {"keywords": "data"}):
        assert refused(browser.patch(data, unreadable)) == (400, "That variant could not be read.")
    after = {item["id"]: item for item in browser.listed()}
    assert after[data]["spec"] == by_id[data]["spec"]


@pytest.mark.django_db
def test_another_users_variant_does_not_exist() -> None:
    owner, other = Browser(make_user(with_resume=True)), Browser(make_user(with_resume=True))
    variant_id = owner.create("Backend", ["backend"]).json()["id"]
    before = ResumeVariant.objects.for_user(owner.user.pk).values().get()
    gone = (404, "That variant no longer exists.")
    for target in (variant_id, uuid.uuid4(), "not-an-id"):
        assert refused(other.patch(target, {"name": "Mine now", "isDefault": True})) == gone
        assert refused(other.render(target)) == gone
        assert refused(other.delete(target)) == gone
    assert ResumeVariant.objects.for_user(owner.user.pk).values().get() == before
    assert other.listed() == []
    assert not Run.objects.for_user(other.user.pk).exists()
    assert not ResumeRow.objects.for_user(owner.user.pk).filter(kind="variant").exists()
    # The same holds below the API.
    with pytest.raises(VariantRefused, match="no longer exists"):
        snapshot(other.user.pk, variant_id)
    chosen = resume_for(other.user.pk, "Backend Engineer", variant_id)
    assert chosen is not None and chosen.variant is None


@pytest.mark.django_db(transaction=True)
def test_deleting_a_variant_takes_its_snapshots_and_keeps_its_pdfs(renders: list[Resume]) -> None:
    browser = Browser(make_user(with_resume=True))
    variant_id = browser.create("Backend", ["backend"]).json()["id"]
    assert browser.render(variant_id).status_code == 202
    run_deferred()
    [document] = Document.objects.for_user(browser.user.pk).filter(kind="resume_pdf")

    response = browser.delete(variant_id)
    assert (response.status_code, response.json()) == (
        200,
        {"ok": True, "message": "Variant deleted"},
    )
    assert browser.listed() == []
    assert not ResumeRow.objects.for_user(browser.user.pk).filter(kind="variant").exists()
    assert ResumeRow.objects.for_user(browser.user.pk).filter(kind="base").count() == 1
    # The PDF it made stays on file, and still downloads.
    assert Document.objects.for_user(browser.user.pk).get(id=document.id) == document
    assert browser.client.get(f"/api/documents/{document.id}").status_code == 200
    assert refused(browser.delete(variant_id)) == (404, "That variant no longer exists.")
    # The name is free again.
    assert browser.create("Backend").status_code == 200


# --- rendering -----------------------------------------------------------------------------------


def finished(run_id: str, user: User) -> dict[str, Any]:
    """The output of a render run that completed."""
    run = Run.objects.for_user(user.pk).get(id=run_id)
    assert run.status == "completed", run.error
    assert isinstance(run.output, dict)
    return run.output


@pytest.mark.django_db(transaction=True)
def test_rendering_a_variant_snapshots_it_once_and_reuses_its_pdf(renders: list[Resume]) -> None:
    user = make_user(with_resume=True)
    browser = Browser(user)
    base = sample()
    lumen, harbor, _ = base.work
    variant_id = browser.create("Backend", ["backend"]).json()["id"]
    spec = {
        "work": [{"id": harbor.id}, {"id": lumen.id, "highlights": ["Led settlement."]}],
        "education": [base.education[0].id],
        "skills": [base.skills[0].id],
    }
    assert browser.patch(variant_id, {"spec": spec}).status_code == 200
    composed = compose(base, VariantSpec.model_validate(spec))

    # The first render writes the snapshot it prints, with the run, and nothing else.
    response = browser.render(variant_id)
    assert response.status_code == 202, response.content
    first = response.json()
    assert first == {
        "ok": True,
        "runId": first["runId"],
        "message": "Rendering your resume to PDF. It takes a few seconds.",
    }
    row = ResumeRow.objects.for_user(user.pk).get(kind="variant")
    assert (str(row.variant_id), row.version, row.match_id) == (variant_id, 1, None)
    assert Resume.model_validate(row.data) == composed
    run = Run.objects.for_user(user.pk).get(id=first["runId"])
    assert (run.kind, run.status) == ("resume_render", "queued")
    assert run.input == {"resumeId": str(row.id), "prompt": "Render your Backend resume to PDF"}
    assert run.dedupe_key == f"render:{row.id}"
    assert browser.listed()[0]["pdf"] is None  # not printed yet

    run_deferred()
    output = finished(first["runId"], user)
    assert output == {
        "resumeId": str(row.id),
        "documentId": output["documentId"],
        "pages": 1,
        "density": "normal",
    }
    document = Document.objects.for_user(user.pk).get(id=output["documentId"])
    # Named for the variant, and keyed by that name, so it is never the base's file.
    assert document.filename == "Avery Quinn - Resume - Backend.pdf"
    assert document.render_hash == render_hash(composed, "Backend") != render_hash(composed)
    row.refresh_from_db()
    assert row.rendered_document_id == document.id
    assert renders == [composed]
    assert browser.listed()[0]["pdf"] == {"documentId": str(document.id), "current": True}

    # Nothing changed: the same snapshot row, and the PDF is reused without printing.
    second = browser.render(variant_id).json()
    assert second["runId"] != first["runId"]
    run_deferred()
    again = finished(second["runId"], user)
    assert again == {
        "resumeId": str(row.id),
        "documentId": str(document.id),
        "pages": 1,
        "density": None,
    }
    assert ResumeRow.objects.for_user(user.pk).filter(kind="variant").count() == 1
    assert Document.objects.for_user(user.pk).filter(kind="resume_pdf").count() == 1
    assert len(renders) == 1

    # The base is edited: the variant follows it, so the PDF on file is no longer current.
    edited = sample_resume_data()
    edited["work"][1]["highlights"][0] = "Built the ingestion service, 60 million events a day."
    assert browser.send("put", "/api/resume", edited).status_code == 200
    assert browser.listed()[0]["pdf"] == {"documentId": str(document.id), "current": False}

    third = browser.render(variant_id).json()
    run_deferred()
    newer = ResumeRow.objects.for_user(user.pk).filter(kind="variant").order_by("-version").first()
    assert newer is not None and (newer.version, newer.id != row.id) == (2, True)
    assert finished(third["runId"], user)["resumeId"] == str(newer.id)
    assert newer.data["work"][0]["highlights"][0].startswith("Built the ingestion service, 60")
    assert newer.data["work"][1]["highlights"] == ["Led settlement."]  # its own wording stays
    assert len(renders) == 2
    pdf = browser.listed()[0]["pdf"]
    assert pdf["current"] is True and pdf["documentId"] != str(document.id)
    # The older snapshot still says what the first PDF was made from.
    row.refresh_from_db()
    assert (Resume.model_validate(row.data), row.rendered_document_id) == (composed, document.id)


@pytest.mark.django_db(transaction=True)
def test_a_variant_that_shows_everything_is_still_its_own_file(renders: list[Resume]) -> None:
    user = make_user(with_resume=True)
    browser = Browser(user)
    variant_id = browser.create("Everything").json()["id"]
    base_run = browser.send("post", "/api/resume/render").json()
    variant_run = browser.render(variant_id).json()
    run_deferred()
    base_pdf = finished(base_run["runId"], user)["documentId"]
    variant_pdf = finished(variant_run["runId"], user)["documentId"]
    assert base_pdf != variant_pdf
    assert dict(Document.objects.for_user(user.pk).values_list("id", "filename")) == {
        uuid.UUID(base_pdf): "Avery Quinn - Resume.pdf",
        uuid.UUID(variant_pdf): "Avery Quinn - Resume - Everything.pdf",
    }
    assert renders == [sample(), sample()]  # the same resume, printed for each name
    assert browser.listed()[0]["pdf"] == {"documentId": variant_pdf, "current": True}
    # Renaming it names the next PDF, without a new snapshot: what it says did not change.
    assert browser.patch(variant_id, {"name": "General"}).status_code == 200
    assert browser.listed()[0]["pdf"] == {"documentId": variant_pdf, "current": False}
    renamed = browser.render(variant_id).json()
    run_deferred()
    document = Document.objects.for_user(user.pk).get(
        id=finished(renamed["runId"], user)["documentId"]
    )
    assert document.filename == "Avery Quinn - Resume - General.pdf"
    assert ResumeRow.objects.for_user(user.pk).filter(kind="variant").count() == 1
    assert browser.listed()[0]["pdf"] == {"documentId": str(document.id), "current": True}


@pytest.mark.django_db
def test_a_render_that_cannot_start_says_why() -> None:
    user = make_user(with_resume=True)
    browser = Browser(user)
    variant_id = browser.create("Backend").json()["id"]
    # A second start while the first is queued is the same run.
    first = browser.render(variant_id).json()
    assert browser.render(variant_id).json()["runId"] == first["runId"]
    assert Run.objects.for_user(user.pk).count() == 1

    # The newest base does not read: there is nothing to compose from.
    make_resume(user, data={"work": "not a list"})
    assert refused(browser.render(variant_id)) == (422, "Upload a resume before making a variant.")
    # A new upload, whose items all have new ids: the variant selects nothing from it.
    reread = sample_resume_data()
    for section in ("work", "education", "projects", "skills"):
        for item in reread[section]:
            item["id"] = str(uuid.uuid4())
    make_resume(user, data=reread)
    assert refused(browser.render(variant_id)) == (
        422,
        "This variant selects nothing from your current resume. Choose what it shows, then "
        "make the PDF.",
    )
    started = pipeline.render_variant(user.pk, uuid.uuid4())
    assert (started.ok, started.refusal, started.message) == (
        False,
        "not_found",
        "That variant no longer exists.",
    )
    assert Run.objects.for_user(user.pk).count() == 1
    assert ResumeRow.objects.for_user(user.pk).filter(kind="variant").count() == 1


# --- with a browser ------------------------------------------------------------------------------


def _chromium_problem() -> str | None:
    async def probe() -> None:
        try:
            await rendering._CHROMIUM.browser()  # pyright: ignore[reportPrivateUsage]
        finally:
            await rendering.close_browser()

    try:
        asyncio.run(probe())
    except PlaywrightError as error:
        return str(error)
    return None


@pytest.fixture
def chromium() -> Iterator[None]:
    problem = _chromium_problem()
    if problem is not None:
        pytest.skip(f"no Chromium to render with: {problem}")
    yield


def _drain_and_close_browser() -> None:
    async def run() -> None:
        try:
            await arun_deferred()
        finally:
            await rendering.close_browser()

    asyncio.run(run())


@pytest.mark.slow
@pytest.mark.django_db(transaction=True)
def test_a_variant_prints_to_a_pdf_of_what_it_selects(chromium: None) -> None:
    user = make_user(with_resume=True)
    browser = Browser(user)
    base = sample()
    lumen, harbor, fernwood = base.work
    variant_id = browser.create("Data", ["data"]).json()["id"]
    spec = {
        "headline": "Data engineer",
        "work": [{"id": harbor.id, "highlights": ["Loaded 40 million events a day."]}],
        "education": [base.education[0].id],
        "skills": [base.skills[2].id],
    }
    assert browser.patch(variant_id, {"spec": spec}).status_code == 200
    started = browser.render(variant_id).json()
    _drain_and_close_browser()
    output = finished(started["runId"], user)
    assert output["density"] in ("normal", "compact")

    served = browser.client.get(f"/api/documents/{output['documentId']}?download=1")
    assert served.status_code == 200 and served.content.startswith(b"%PDF-")
    name = "Avery Quinn - Resume - Data.pdf"
    assert served["Content-Disposition"].startswith(f'attachment; filename="{name}"')
    text = parse_pdf(served.content).text
    assert text.startswith("Avery Quinn")
    for shown in ("Data engineer", harbor.company, "Loaded 40 million events a day.", "SQL"):
        assert shown in text, shown
    for hidden in (lumen.company, fernwood.company, "ingestion service", "Kubernetes"):
        assert hidden not in text, hidden

    # Again, unchanged: no second file.
    again = browser.render(variant_id).json()
    _drain_and_close_browser()
    assert finished(again["runId"], user)["documentId"] == output["documentId"]
    assert Document.objects.for_user(user.pk).filter(kind="resume_pdf").count() == 1
