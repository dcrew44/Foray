"""Workday, SmartRecruiters and iCIMS (#57), against payloads captured in November 2026.

Three of these list postings without their descriptions, so they are `ListedAdapter`s read a
posting at a time (`refresh._refresh_listed`); the fourth (`jibe`, iCIMS career sites) is a
whole-board adapter with pages. Each adapter's quirk is pinned by the payload that showed it,
and the incremental refresh is driven with a board made for the purpose.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from asgiref.sync import sync_to_async

from apps.core.clock import database_now
from apps.jobs import refresh
from apps.jobs.discovery import AtsLink, discover_company, find_ats_links, has_unsupported_ats
from apps.jobs.models import Company, Job
from apps.jobs.refresh import refresh_company
from apps.jobs.sources import (
    BOARD_ADAPTERS,
    GUESS_SOURCES,
    LISTED_ADAPTERS,
    SOURCES,
    icims,
    jibe,
    smartrecruiters,
    workday,
)
from apps.jobs.sources.jsearch import DiscoveredBoard, ats_from_apply_options
from apps.jobs.sources.links import BoardLink, board_links, site_label
from apps.jobs.sources.poll import PollState
from apps.jobs.sources.types import (
    BoardNotFound,
    ListedAdapter,
    ListedPosting,
    Listing,
    NormalizedJob,
)
from apps.jobs.verify import verify_board
from tests.factories import make_company
from tests.jobs.conftest import (
    assert_matches_golden,
    fixture_json,
    fixture_text,
    golden,
    html,
    mock_client,
    routes,
)
from tests.jobs.test_ingest import posting

db = pytest.mark.django_db(transaction=True)

NVIDIA = "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
NVIDIA_API = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite"
NVIDIA_LISTING = fixture_json("workday/nvidia.listing.json")
NVIDIA_POSTING = fixture_json("workday/nvidia.posting.json")
SERVICENOW_LISTING = fixture_json("smartrecruiters/servicenow.listing.json")
SERVICENOW_POSTING = fixture_json("smartrecruiters/servicenow.posting.json")
PNNL = fixture_json("jibe/pnnl.json")
GDMS_SITEMAP = fixture_text("icims/gdms.sitemap.xml")
GDMS_PAGE = fixture_text("icims/gdms.posting.html")


def test_every_source_has_exactly_one_adapter() -> None:
    assert set(SOURCES) == set(BOARD_ADAPTERS) | set(LISTED_ADAPTERS)
    assert not set(BOARD_ADAPTERS) & set(LISTED_ADAPTERS)
    assert set(LISTED_ADAPTERS) == {"workday", "smartrecruiters", "icims"}
    # None of the new boards can be guessed from a company's name.
    assert not {"workday", "smartrecruiters", "icims", "jibe"} & set(GUESS_SOURCES)


# --- goldens -------------------------------------------------------------------------------------


def _normalized(source: str, expected: dict[str, Any]) -> list[NormalizedJob]:
    slug = expected["slug"]
    if source == "jibe":
        return jibe.parse_board(slug, fixture_text(expected["fixture"]))
    if source == "icims":
        page = fixture_text(expected["fixture"])
        found = icims.json_ld(page)
        assert found is not None
        posting_id = re.search(r"/jobs/(\d+)/", found["url"])
        assert posting_id is not None
        return [icims.normalize_job(slug, posting_id.group(1), found, expected["companyName"])]
    detail = fixture_json(expected["fixture"])
    if source == "workday":
        return [workday.normalize_job(slug, detail["jobPostingInfo"], expected["companyName"])]
    return [smartrecruiters.normalize_job(slug, detail, expected["companyName"])]


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("workday", "nvidia"),
        ("workday", "workday"),
        ("smartrecruiters", "servicenow"),
        ("jibe", "pnnl"),
        ("icims", "gdms"),
    ],
)
def test_adapter_matches_its_golden(source: str, name: str) -> None:
    expected = golden(f"adapters/{source}/{name}.json")
    jobs = _normalized(source, expected)
    assert [job.id for job in jobs] == [job["id"] for job in expected["jobs"]]
    for job, want in zip(jobs, expected["jobs"], strict=True):
        assert_matches_golden(job, want)


# --- Workday -------------------------------------------------------------------------------------


def test_a_workday_board_is_its_address() -> None:
    board = workday.parse_slug(NVIDIA)
    assert (board.host, board.tenant, board.site) == (
        "nvidia.wd5.myworkdayjobs.com",
        "nvidia",
        "NVIDIAExternalCareerSite",
    )
    assert board.api == NVIDIA_API
    assert board.public == f"https://{NVIDIA}" and board.slug == NVIDIA
    assert board.key == "nvidia.NVIDIAExternalCareerSite"

    other = workday.parse_slug("wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs")
    assert (other.tenant, other.site) == ("wf", "WellsFargoJobs")
    assert other.api == "https://wd1.myworkdaysite.com/wday/cxs/wf/WellsFargoJobs"
    assert other.public == "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs"

    for bad in ("nvidia", "example.com/Site", "nvidia.wd5.myworkdayjobs.com", NVIDIA + "/x/y"):
        with pytest.raises(BoardNotFound):
            workday.parse_slug(bad)


def test_workday_boards_are_found_in_links() -> None:
    text = (
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/X_JR1 "
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite "
        "https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/job/Y "
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    )
    assert [board.slug for board in workday.boards_in(text)] == [
        NVIDIA,
        "wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs",
    ]


def _workday_item(n: int) -> dict[str, Any]:
    return {
        "title": f"Engineer {n}",
        "externalPath": f"/job/US-CA-Santa-Clara/Engineer-{n}_JR{n}",
        "locationsText": "US, CA, Santa Clara",
        "postedOn": "Posted Today",
        "bulletFields": [f"JR{n}"],
    }


@dataclass
class WorkdaySite:
    """One career site's backend. `capped` is the variant that reports its total on the first
    page only, never more than 2,000, and answers an offset past that with page one."""

    postings: int
    capped: bool = False
    requests: list[httpx2.Request] = field(default_factory=list)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.method != "POST" or str(request.url) != f"{NVIDIA_API}/jobs":
            return httpx2.Response(404, text="Not Found")
        if request.headers.get("content-type") != "application/json":
            return httpx2.Response(500, text="no content type")
        body = json.loads(request.content)
        offset, limit = body["offset"], body["limit"]
        if limit > workday.PAGE:
            return httpx2.Response(400, json={"errorCode": "HTTP_400"})
        shown = min(self.postings, workday.CAP) if self.capped else self.postings
        if self.capped and offset >= workday.CAP:
            offset = 0
        items = [_workday_item(n) for n in range(offset, min(offset + limit, shown))]
        total = shown if (offset == 0 or not self.capped) else 0
        return httpx2.Response(200, json={"total": total, "jobPostings": items, "facets": []})

    @property
    def offsets(self) -> list[int]:
        return [json.loads(request.content)["offset"] for request in self.requests]


async def test_workday_lists_twenty_a_page_until_the_first_page_s_total() -> None:
    site = WorkdaySite(postings=45)
    async with mock_client(site.handler) as client:
        listing = await workday.list_board(client, NVIDIA)
    assert site.offsets == [0, 20, 40]
    assert len(listing.postings) == 45 and listing.complete
    first = listing.postings[0]
    assert first.external_id == "Engineer-0_JR0"
    assert json.loads(site.requests[0].content) == {
        "appliedFacets": {},
        "limit": 20,
        "offset": 0,
        "searchText": "",
    }


async def test_workday_s_capped_variant_stops_at_two_thousand_and_says_it_is_incomplete() -> None:
    site = WorkdaySite(postings=2803, capped=True)
    async with mock_client(site.handler) as client:
        listing = await workday.list_board(client, NVIDIA)
    # A hundred pages, never an offset the server would answer with page one again.
    assert site.offsets == list(range(0, 2000, 20))
    assert len(listing.postings) == 2000
    assert listing.complete is False  # what is missing from it cannot be concluded closed


async def test_workday_skips_placeholders_and_reads_real_listings() -> None:
    page = copy.deepcopy(NVIDIA_LISTING)
    page["total"] = len(page["jobPostings"]) + 1
    page["jobPostings"].append({"bulletFields": ["R266544"]})  # no title, no path
    async with mock_client(lambda request: httpx2.Response(200, json=page)) as client:
        listing = await workday.list_board(client, NVIDIA)
        assert await workday.count(client, NVIDIA) == page["total"]
    assert [p.external_id for p in listing.postings] == [
        workday.posting_id(item) for item in NVIDIA_LISTING["jobPostings"]
    ]
    assert listing.postings[0].external_id == "Principal-CPU-Software-Architect_JR2019801"


async def test_the_workday_probe_ignores_the_wording_of_the_date() -> None:
    page = {"total": 2, "jobPostings": [_workday_item(1), _workday_item(2)]}
    answers = {"page": page}
    async with mock_client(lambda request: httpx2.Response(200, json=answers["page"])) as client:
        today = await workday.probe(client, NVIDIA)
        tomorrow_page = copy.deepcopy(page)
        for item in tomorrow_page["jobPostings"]:
            item["postedOn"] = "Posted Yesterday"
        answers["page"] = tomorrow_page
        assert await workday.probe(client, NVIDIA) == today
        answers["page"] = {**tomorrow_page, "total": 3}
        assert await workday.probe(client, NVIDIA) != today
        renamed = copy.deepcopy(page)
        renamed["jobPostings"][0]["title"] = "Senior Engineer 1"
        answers["page"] = renamed
        assert await workday.probe(client, NVIDIA) != today


@pytest.mark.parametrize(
    ("status", "body"),
    [(404, "workday/_site-not-found.json"), (422, "workday/_tenant-unknown.json")],
)
async def test_an_unknown_workday_site_or_tenant_is_no_board(status: int, body: str) -> None:
    answer = httpx2.Response(status, json=fixture_json(body))
    async with mock_client(lambda request: answer) as client:
        with pytest.raises(BoardNotFound):
            await workday.list_board(client, NVIDIA)
        with pytest.raises(BoardNotFound):
            await workday.probe(client, NVIDIA)


async def test_a_workday_posting_is_one_request_by_its_id() -> None:
    listed = ListedPosting("Principal-CPU-Software-Architect_JR2019801", "v", {})
    url = f"{NVIDIA_API}/job/Principal-CPU-Software-Architect_JR2019801"
    gone = httpx2.Response(404, json=fixture_json("workday/_posting-not-found.json"))
    async with mock_client(routes({url: httpx2.Response(200, json=NVIDIA_POSTING)})) as client:
        job = await workday.read_posting(client, NVIDIA, listed, "NVIDIA")
    assert job is not None
    assert (
        job.id
        == "workday:nvidia.NVIDIAExternalCareerSite:Principal-CPU-Software-Architect_JR2019801"
    )
    assert job.company_name == "NVIDIA"  # the registry's name; the payload has "2100 NVIDIA USA"
    # "US, CA, Santa Clara" turned the way the location parser reads it.
    assert job.locations[:2] == ["Santa Clara, CA", "Austin, TX"]
    assert job.location_norm is not None and job.location_norm.metro == "bay-area"
    assert job.posted_at is not None and job.posted_at.isoformat() == "2026-11-09T00:00:00+00:00"
    assert job.url.startswith(f"https://{NVIDIA}/job/")
    async with mock_client(lambda request: gone) as client:
        assert await workday.read_posting(client, NVIDIA, listed, "NVIDIA") is None


def test_a_workday_workplace_among_the_locations_is_not_a_place() -> None:
    """Argonne lists "On-site" as an additional location and writes "Lemont, IL USA"."""
    info = {
        "title": "Software Engineer",
        "jobPostingId": "Software-Engineer_1",
        "location": "Lemont, IL USA",
        "additionalLocations": ["On-site"],
        "jobDescription": "<p>Write software.</p>",
    }
    job = workday.normalize_job(NVIDIA, info, "Argonne")
    assert job.locations == ["Lemont, IL"]
    assert job.location_norm is not None
    assert (job.location_norm.region, job.location_norm.country) == ("IL", "US")
    assert job.remote == "onsite"
    # A posting whose only location is "Remote" keeps it.
    alone = {**info, "location": "Remote", "additionalLocations": []}
    assert workday.normalize_job(NVIDIA, alone, "Argonne").locations == ["Remote"]


# --- SmartRecruiters -----------------------------------------------------------------------------


def _sr_item(n: int) -> dict[str, Any]:
    return {
        "id": str(7000 + n),
        "name": f"Role {n}",
        "releasedDate": f"2026-11-0{n % 9 + 1}T00:00:00.000Z",
    }


async def test_smartrecruiters_lists_a_hundred_a_page() -> None:
    asked: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append(request.url.query.decode())
        offset = int(request.url.params["offset"])
        items = [_sr_item(n) for n in range(offset, min(offset + 100, 250))]
        return httpx2.Response(
            200, json={"offset": offset, "limit": 100, "totalFound": 250, "content": items}
        )

    async with mock_client(handler) as client:
        listing = await smartrecruiters.list_board(client, "ServiceNow")
    assert asked == ["limit=100&offset=0", "limit=100&offset=100", "limit=100&offset=200"]
    assert len(listing.postings) == 250 and listing.complete
    assert listing.postings[0].version == "2026-11-01T00:00:00.000Z|Role 0"


async def test_an_unknown_smartrecruiters_company_is_told_from_an_empty_one() -> None:
    """Both answer 200 with nothing listed; only the company's own page says which it is."""
    empty = httpx2.Response(200, json=fixture_json("smartrecruiters/_unknown.json"))
    away = httpx2.Response(302, headers={"location": "https://jobs.smartrecruiters.com/"})

    def unknown(request: httpx2.Request) -> httpx2.Response:
        return away if request.url.host == "careers.smartrecruiters.com" else empty

    def quiet(request: httpx2.Request) -> httpx2.Response:
        return html("<h1>Careers at Visa</h1>") if request.url.host.startswith("careers") else empty

    async with mock_client(unknown) as client:
        with pytest.raises(BoardNotFound):
            await smartrecruiters.list_board(client, "NoSuchCompany")
        with pytest.raises(BoardNotFound):
            await smartrecruiters.count(client, "NoSuchCompany")
    async with mock_client(quiet) as client:
        assert (await smartrecruiters.list_board(client, "Visa")).postings == []
        assert await smartrecruiters.count(client, "Visa") == 0
    with pytest.raises(BoardNotFound):
        smartrecruiters.list_url("not a slug")


async def test_a_smartrecruiters_posting_keeps_no_recruiter_s_name() -> None:
    detail = {**SERVICENOW_POSTING, "creator": {"name": "A Recruiter", "avatarUrl": ""}}
    listed = ListedPosting(detail["id"], "v", {})
    url = smartrecruiters.posting_url("ServiceNow", detail["id"])
    gone = httpx2.Response(404, json=fixture_json("smartrecruiters/_posting-not-found.json"))
    async with mock_client(routes({url: httpx2.Response(200, json=detail)})) as client:
        job = await smartrecruiters.read_posting(client, "ServiceNow", listed, "ServiceNow")
        probe = None
        with pytest.raises(BoardNotFound):
            probe = await smartrecruiters.probe(client, "ServiceNow")
    assert probe is None and job is not None
    assert "creator" not in job.raw and "A Recruiter" not in json.dumps(job.raw)
    assert job.locations == ["San Diego, CALIFORNIA, United States"]
    assert job.location_norm is not None and job.location_norm.region == "CA"
    assert job.remote == "remote" and job.apply_url is not None
    assert job.description_raw is not None and "<h3>Qualifications</h3>" in job.description_raw
    async with mock_client(lambda request: gone) as client:
        assert await smartrecruiters.read_posting(client, "ServiceNow", listed, "x") is None


async def test_the_smartrecruiters_probe_is_one_posting() -> None:
    asked: list[str] = []
    page = {"totalFound": 754, "content": SERVICENOW_LISTING["content"][:1]}

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append(str(request.url))
        return httpx2.Response(200, json=page)

    async with mock_client(handler) as client:
        first = await smartrecruiters.probe(client, "ServiceNow")
        page = {**page, "totalFound": 753}
        assert await smartrecruiters.probe(client, "ServiceNow") != first
    assert asked[0].endswith("/ServiceNow/postings?limit=1&offset=0")


# --- iCIMS career sites (Jibe) -------------------------------------------------------------------


async def test_a_career_site_is_its_pages_five_seconds_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(jibe, "_sleep", no_wait)
    template = PNNL["jobs"][0]["data"]
    asked: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params["page"])
        asked.append(page)
        ids = range((page - 1) * 100, min(page * 100, 250))
        jobs = [{"data": {**template, "slug": str(n), "req_id": str(n)}} for n in ids]
        return httpx2.Response(200, json={"jobs": jobs, "totalCount": 250})

    async with mock_client(handler) as client:
        jobs = await jibe.fetch_board(client, "careers.pnnl.gov")
        assert await jibe.count(client, "careers.pnnl.gov") == 250
    assert asked == [1, 2, 3, 1]  # the count is page one alone
    assert len(jobs) == 250 and waits == [5.0, 5.0]
    assert jobs[0].id == "jibe:careers.pnnl.gov:0"


async def test_a_career_site_that_is_not_this_product_is_no_board() -> None:
    answers = [
        html("<html>Careers</html>"),
        httpx2.Response(500, text="A server error occurred"),
        httpx2.Response(200, json={"something": "else"}),
        httpx2.Response(404, text="Not Found"),
    ]
    for answer in answers:
        async with mock_client(lambda request, answer=answer: answer) as client:
            with pytest.raises(BoardNotFound):
                await jibe.fetch_board(client, "careers.example.com")
    with pytest.raises(BoardNotFound):
        jibe.board_url("not a host/path")


def test_a_career_site_posting_with_no_fixed_place_is_remote() -> None:
    job = PNNL["jobs"][0]["data"]
    placed = jibe.normalize_job("careers.pnnl.gov", job)
    assert placed.locations == ["Richland, Washington", "Seattle, Washington"]
    assert placed.remote == "onsite"
    anywhere = jibe.normalize_job("careers.pnnl.gov", {**job, "location_type": "ANY"})
    assert anywhere.remote == "remote"
    assert placed.apply_url == "https://careers-pnnl.icims.com/jobs/12949/login"


# --- iCIMS portals -------------------------------------------------------------------------------


async def test_an_icims_portal_is_listed_from_its_sitemap() -> None:
    url = icims.sitemap_url("careers-gdms")
    async with mock_client(routes({url: httpx2.Response(200, text=GDMS_SITEMAP)})) as client:
        listing = await icims.list_board(client, "careers-gdms")
        assert await icims.count(client, "careers-gdms") == 3
        fingerprint = await icims.probe(client, "careers-gdms")
    # The `/jobs/intro` entry is not a posting; each posting's version is its `lastmod`.
    assert [(p.external_id, p.version) for p in listing.postings] == [
        ("75431", "2026-11-09T15:50:48-05:00"),
        ("75490", "2026-11-09T15:39:55-05:00"),
        ("75075", "2026-11-09T15:04:23-05:00"),
    ]
    assert (
        listing.postings[2]
        .item["loc"]
        .endswith("sr-deputy-program-manager-engineering-%28dpme%29/job")
    )
    assert len(fingerprint) == 40


async def test_an_icims_posting_is_read_from_its_page_s_json_ld() -> None:
    listed = ListedPosting("78250", "v", {})
    url = icims.posting_url("careers-gdms", "78250")
    assert url == "https://careers-gdms.icims.com/jobs/78250/job?in_iframe=1"
    async with mock_client(routes({url: html(GDMS_PAGE)})) as client:
        job = await icims.read_posting(client, "careers-gdms", listed, "GDMS")
    assert job is not None
    assert (job.id, job.locations) == ("icims:careers-gdms:78250", ["Manassas, VA"])
    assert job.company_name == "General Dynamics Mission Systems, Inc"
    assert job.employment_type is None  # the page says OTHER
    assert job.posted_at is not None and job.posted_at.hour == 5

    for answer in (httpx2.Response(410, text="gone"), html("<html>no posting here</html>")):
        async with mock_client(lambda request, answer=answer: answer) as client:
            assert await icims.read_posting(client, "careers-gdms", listed, "GDMS") is None


def test_an_icims_date_the_page_made_up_and_a_blank_name_are_not_believed() -> None:
    found = icims.json_ld(GDMS_PAGE)
    assert found is not None
    invented = {**found, "datePosted": "2024-11-09T20:51:37.120Z"}
    assert icims.normalize_job("careers-gdms", "1", invented, "GDMS").posted_at is None
    blank = {**found, "hiringOrganization": {"name": "UNAVAILABLE"}}
    assert icims.normalize_job("careers-gdms", "1", blank, "GDMS").company_name == "GDMS"


async def test_an_icims_portal_that_moved_says_where_to() -> None:
    stub = fixture_text("icims/amd.moved.html")
    table = {
        icims.sitemap_url("careers-amd"): httpx2.Response(403, text="not on a trusted network"),
        "https://careers-amd.icims.com/jobs/search?ss=1&in_iframe=1": html(stub),
    }
    async with mock_client(routes(table)) as client:
        with pytest.raises(BoardNotFound):
            await icims.list_board(client, "careers-amd")
        assert await icims.moved_to(client, "careers-amd") == "careers.amd.com"
        assert await icims.moved_to(client, "careers-nowhere") is None
    with pytest.raises(BoardNotFound):
        icims.sitemap_url("Not A Subdomain")


# --- reading a listed board a posting at a time --------------------------------------------------


@dataclass
class Board:
    """A listed board made for the test: what is open, each posting's version, and counters."""

    company: Company
    open: dict[int, str] = field(default_factory=dict)  # posting number -> listing version
    complete: bool = True
    exists: bool = True
    broken: set[int] = field(default_factory=set)
    gone: set[int] = field(default_factory=set)
    probes: int = 0
    listings: int = 0
    read: list[int] = field(default_factory=list)

    def fingerprint(self) -> str:
        return json.dumps(sorted(self.open.items()))

    @property
    def adapter(self) -> ListedAdapter:
        async def count(client: Any, slug: str) -> int:
            return len(self.open)

        async def probe(client: Any, slug: str) -> str:
            self.probes += 1
            if not self.exists:
                raise BoardNotFound("workday", slug)
            return self.fingerprint()

        async def list_board(client: Any, slug: str) -> Listing:
            self.listings += 1
            postings = [ListedPosting(str(n), version, {}) for n, version in self.open.items()]
            return Listing(postings, complete=self.complete)

        async def read_posting(
            client: Any, slug: str, listed: ListedPosting, company_name: str
        ) -> NormalizedJob | None:
            number = int(listed.external_id)
            self.read.append(number)
            if number in self.broken:
                raise RuntimeError("the posting could not be read")
            if number in self.gone:
                return None
            return posting(self.company, number, description=f"Version {listed.version}.")

        return ListedAdapter("workday", count, probe, list_board, read_posting)


async def listed_board(**opened: str) -> Board:
    company = await sync_to_async(make_company)(
        slug="acme", source_type="workday", ats_slug="acme.wd1.myworkdayjobs.com/Careers"
    )
    return Board(company, {int(number): version for number, version in opened.items()})


async def pass_over(board: Board, *, force: bool = False):
    company = await Company.objects.aget(id=board.company.id)
    async with mock_client(routes({})) as client:
        return await refresh._refresh_listed(client, company, board.adapter, force=force)


def _open(company: Company) -> dict[str, tuple[str | None, int | None]]:
    rows = Job.objects.filter(company_id=company.id, is_active=True)
    return {
        external: (version, lag)
        for external, version, lag in rows.values_list(
            "external_id", "listing_version", "seen_lag_s"
        )
    }


@db
async def test_a_listed_board_is_read_once_and_then_only_asked() -> None:
    board = await listed_board(**{"1": "a", "2": "a", "3": "a"})
    first = await pass_over(board)
    assert (first.status, first.jobs, first.unchanged) == ("ok", 3, False)
    assert first.ingest is not None and first.ingest.inserted == 3
    assert sorted(board.read) == [1, 2, 3]
    # A first read measures nothing: every posting was up before anyone looked.
    assert await sync_to_async(_open)(board.company) == {
        "1": ("a", None),
        "2": ("a", None),
        "3": ("a", None),
    }
    company = await Company.objects.aget(id=board.company.id)
    state = PollState.read(company.poll_state)
    assert (state.url, state.digest, state.pending) == ("workday", board.fingerprint(), False)
    assert (company.last_fetch_status, company.last_fetch_job_count) == ("ok", 3)

    second = await pass_over(board)
    assert (second.unchanged, second.jobs, second.ingest) == (True, 3, None)
    assert (board.probes, board.listings, len(board.read)) == (2, 1, 3)  # one probe, nothing else


@db
async def test_only_what_is_new_or_changed_is_read_and_what_left_is_closed() -> None:
    board = await listed_board(**{"1": "a", "2": "a", "3": "a"})
    await pass_over(board)
    await Company.objects.filter(id=board.company.id).aupdate(
        last_ok_at=await sync_to_async(database_now)() - timedelta(minutes=15)
    )
    board.read.clear()
    board.open = {1: "a", 2: "b", 4: "a"}  # 2 changed, 3 left, 4 arrived

    outcome = await pass_over(board)
    assert sorted(board.read) == [2, 4]
    assert outcome.ingest is not None
    assert (outcome.ingest.inserted, outcome.ingest.updated, outcome.ingest.deactivated) == (
        1,
        1,
        1,
    )
    found = await sync_to_async(_open)(board.company)
    assert set(found) == {"1", "2", "4"}
    assert found["2"][0] == "b"
    # The new posting is timed against the last look (it carries a September date).
    lag = found["4"][1]
    assert lag is not None and 890 <= lag <= 915
    assert not await Job.objects.filter(external_id="3", is_active=True).aexists()


@db
async def test_an_incomplete_listing_closes_nothing() -> None:
    board = await listed_board(**{"1": "a", "2": "a"})
    await pass_over(board)
    board.open = {1: "a"}
    board.complete = False  # the source cut the list short: 2 may simply be past the cap
    outcome = await pass_over(board)
    assert outcome.ingest is not None and outcome.ingest.deactivated == 0
    assert set(await sync_to_async(_open)(board.company)) == {"1", "2"}


@db
async def test_a_big_board_is_caught_up_over_several_passes_and_not_timed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(refresh, "POSTINGS_PER_PASS", 2)
    board = await listed_board(**{str(n): "a" for n in range(1, 6)})

    await pass_over(board)
    assert len(board.read) == 2
    company = await Company.objects.aget(id=board.company.id)
    assert PollState.read(company.poll_state).pending is True

    # The probe has not changed, and the pass goes on with the rest anyway.
    await pass_over(board)
    assert len(board.read) == 4
    last = await pass_over(board)
    assert sorted(board.read) == [1, 2, 3, 4, 5] and last.unchanged is False
    company = await Company.objects.aget(id=board.company.id)
    assert PollState.read(company.poll_state).pending is False
    # What a later pass of the catch-up brought in was there all along.
    assert {lag for _, lag in (await sync_to_async(_open)(board.company)).values()} == {None}

    settled = await pass_over(board)
    assert settled.unchanged is True and len(board.read) == 5


@db
async def test_a_posting_that_fails_is_tried_again_and_one_that_has_gone_is_not() -> None:
    board = await listed_board(**{"1": "a", "2": "a", "3": "a"})
    board.broken, board.gone = {2}, {3}
    outcome = await pass_over(board)
    assert outcome.status == "ok" and set(await sync_to_async(_open)(board.company)) == {"1"}
    company = await Company.objects.aget(id=board.company.id)
    assert PollState.read(company.poll_state).pending is True  # 2 failed: next pass goes on

    board.broken, board.read = set(), []
    await pass_over(board)
    assert sorted(board.read) == [2, 3]  # 3 is asked for again only because the pass went on
    assert set(await sync_to_async(_open)(board.company)) == {"1", "2"}
    company = await Company.objects.aget(id=board.company.id)
    assert PollState.read(company.poll_state).pending is False


@db
async def test_a_board_whose_postings_all_fail_is_an_error_that_backs_off() -> None:
    board = await listed_board(**{"1": "a", "2": "a"})
    board.broken = {1, 2}
    outcome = await pass_over(board)
    assert outcome.status == "error" and "could be read" in (outcome.error or "")
    company = await Company.objects.aget(id=board.company.id)
    assert (company.last_fetch_status, company.fetch_failures) == ("error", 1)


@db
async def test_one_empty_listing_closes_nothing_and_a_second_does() -> None:
    board = await listed_board(**{"1": "a", "2": "a"})
    await pass_over(board)
    board.open = {}
    first = await pass_over(board)
    assert first.ingest is not None and first.ingest.deactivated == 0
    assert len(await sync_to_async(_open)(board.company)) == 2
    # The guard held, so the fingerprint was not remembered: the next pass lists again.
    second = await pass_over(board)
    assert second.unchanged is False and second.ingest is not None
    assert second.ingest.deactivated == 2
    assert await sync_to_async(_open)(board.company) == {}


@db
async def test_fetch_now_reads_every_posting_again_and_a_missing_board_is_retired() -> None:
    board = await listed_board(**{"1": "a", "2": "a"})
    await pass_over(board)
    board.read.clear()
    await pass_over(board, force=True)
    assert sorted(board.read) == [1, 2]

    board.exists = False
    outcome = await pass_over(board)
    assert outcome.status == "not-found"
    company = await Company.objects.aget(id=board.company.id)
    assert (company.status, company.poll_state) == ("invalid", {})


@db
async def test_refresh_company_reads_a_real_listed_adapter_through_the_client() -> None:
    """End to end with the Workday adapter: page one, then the one posting it lists."""
    company = await sync_to_async(make_company)(
        name="NVIDIA", slug="nvidia", source_type="workday", ats_slug=NVIDIA
    )
    listing = {**NVIDIA_LISTING, "total": 1, "jobPostings": NVIDIA_LISTING["jobPostings"][:1]}
    detail_url = f"{NVIDIA_API}/job/Principal-CPU-Software-Architect_JR2019801"
    asked: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append((request.method, str(request.url)))
        if request.method == "POST":
            return httpx2.Response(200, json=listing)
        if str(request.url) == detail_url:
            return httpx2.Response(200, json=NVIDIA_POSTING)
        return httpx2.Response(404, text="Not Found")

    async with mock_client(handler) as client:
        first = await refresh_company(client, company)
        second = await refresh_company(client, await Company.objects.aget(id=company.id))
    assert (first.status, first.jobs) == ("ok", 1) and second.unchanged is True
    # The probe, the listing, the posting; then the probe alone.
    assert asked == [
        ("POST", f"{NVIDIA_API}/jobs"),
        ("POST", f"{NVIDIA_API}/jobs"),
        ("GET", detail_url),
        ("POST", f"{NVIDIA_API}/jobs"),
    ]
    job = await Job.objects.aget(company_id=company.id)
    assert (job.source, job.company_name, job.level) == ("workday", "NVIDIA", "staff_plus")
    assert job.listing_version is not None and job.facts_version is not None


# --- finding these boards ------------------------------------------------------------------------


def test_board_links_carry_the_slug_an_adapter_wants_and_a_name_for_the_company() -> None:
    page = (
        '<a href="https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite">Jobs</a>'
        '<a href="https://jobs.smartrecruiters.com/ServiceNow/744000160721658-lead">One</a>'
        '<a href="https://careers.smartrecruiters.com/ServiceNow">All</a>'
        '<a href="https://us-careers-rivian.icims.com/jobs/1/x/job">Rivian</a>'
        '<a href="https://www.icims.com/products">not a portal</a>'
    )
    assert board_links(page) == [
        BoardLink("workday", NVIDIA, "nvidia"),
        BoardLink("smartrecruiters", "ServiceNow", "ServiceNow"),
        BoardLink("icims", "us-careers-rivian", "rivian"),
    ]
    assert find_ats_links(page) == [
        AtsLink("workday", NVIDIA, "nvidia"),
        AtsLink("smartrecruiters", "ServiceNow", "ServiceNow"),
        AtsLink("icims", "us-careers-rivian", "rivian"),
    ]
    assert not has_unsupported_ats(page)  # they have adapters now
    assert has_unsupported_ats('<a href="https://acme.taleo.net/careersection">x</a>')
    assert (
        site_label("careers.pnnl.gov") == "pnnl" and site_label("jobs.keysight.com") == "keysight"
    )


def test_an_aggregator_s_workday_link_is_a_board_we_can_read() -> None:
    job = {
        "job_apply_link": "https://www.linkedin.com/jobs/view/1",
        "apply_options": [
            {
                "apply_link": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/x/Y_JR1"
            }
        ],
    }
    assert ats_from_apply_options(job) == DiscoveredBoard("workday", NVIDIA, label="nvidia")


@db
async def test_a_pasted_workday_link_registers_the_board_under_the_tenant_s_name() -> None:
    listing = {**NVIDIA_LISTING, "total": 3}
    posted: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            posted.append(str(request.url))
            return httpx2.Response(200, json=listing)
        return httpx2.Response(404, text="Not Found")

    async with mock_client(handler) as client:
        result = await discover_company(
            client, "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
        )
    company = result.registered
    assert company is not None and result.created
    assert (company.source_type, company.ats_slug) == ("workday", NVIDIA)
    assert (company.slug, company.name, company.domain) == ("nvidia", "Nvidia", None)
    assert posted == [f"{NVIDIA_API}/jobs"]  # verified by its count: one request
    assert result.trail[0] == {"step": "ats-link", "detail": f"pasted workday:{NVIDIA}"}


@db
async def test_an_icims_portal_that_moved_is_registered_as_the_career_site_it_moved_to() -> None:
    table = {
        "https://careers-amd.icims.com/jobs/search?ss=1&in_iframe=1": html(
            fixture_text("icims/amd.moved.html")
        ),
        jibe.board_url("careers.amd.com"): httpx2.Response(200, json=PNNL),
    }
    async with mock_client(routes(table)) as client:
        result = await discover_company(client, "https://careers-amd.icims.com/jobs/1/x/job")
    company = result.registered
    assert company is not None
    assert (company.source_type, company.ats_slug, company.slug) == (
        "jibe",
        "careers.amd.com",
        "amd",
    )


@db
async def test_an_employer_s_own_career_site_is_found_by_asking_it() -> None:
    """Nothing in `careers.pnnl.gov` says iCIMS, so discovery asks the site itself."""
    asked: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append(str(request.url))
        if str(request.url) == jibe.board_url("careers.pnnl.gov"):
            return httpx2.Response(200, json=PNNL)
        return httpx2.Response(404, text="Not Found")

    async with mock_client(handler) as client:
        result = await discover_company(client, "pnnl.gov")
    company = result.registered
    assert company is not None
    assert (company.source_type, company.ats_slug) == ("jibe", "careers.pnnl.gov")
    assert (company.slug, company.domain) == ("pnnl", "pnnl.gov")
    assert {"step": "career-site", "detail": "careers.pnnl.gov"} in result.trail
    # It was asked after the careers paths, and before any slug was guessed against an ATS.
    assert jibe.board_url("pnnl.gov") in asked
    assert not [url for url in asked if "greenhouse" in url or "lever" in url]


async def test_a_listed_board_is_verified_by_its_count() -> None:
    site = WorkdaySite(postings=45)
    async with mock_client(site.handler) as client:
        outcome = await verify_board(client, "workday", NVIDIA)
    assert (outcome.status, outcome.job_count) == ("ok", 45)
    assert site.offsets == [0]  # not the three pages reading it would take
    sitemap = httpx2.Response(200, text=GDMS_SITEMAP)
    async with mock_client(routes({icims.sitemap_url("careers-gdms"): sitemap})) as client:
        found = await verify_board(client, "icims", "careers-gdms")
        missing = await verify_board(client, "icims", "careers-none")
    assert (found.status, found.job_count, missing.status) == ("ok", 3, "not-found")


@db
async def test_a_listed_board_is_hot_while_it_lists_an_early_career_role() -> None:
    """A listed board's postings are not all in hand at a pass, so whether it lists an
    early-career role is asked of the facts already read."""
    board = await listed_board(**{"1": "a"})
    await pass_over(board)
    company = await Company.objects.aget(id=board.company.id)
    assert company.last_relevant_at is not None  # "Engineer 1" is the first rung of a ladder

    await Job.objects.filter(company_id=company.id).aupdate(level="senior")
    await Company.objects.filter(id=company.id).aupdate(last_relevant_at=None)
    board.open = {1: "a", 2: "a"}  # "Engineer 2" arrives: the second rung
    await pass_over(board)
    company = await Company.objects.aget(id=board.company.id)
    assert company.last_relevant_at is None
