"""Growing the registry from public lists of board ids (#58): reading ids out of URLs and out of
Common Crawl's index, asking each candidate's board once, and what registering one records."""

from __future__ import annotations

import gzip
import json
from typing import Any

import httpx2
import pytest
from asgiref.sync import sync_to_async
from django.core.management import call_command

from apps.jobs import candidates, crawl_index, radar
from apps.jobs.candidates import Seen, add_candidates, board_in_url, grow, link_known, seen_in_urls
from apps.jobs.models import BoardCandidate, Company, Job
from apps.jobs.registry import CompanyInput, upsert_company
from apps.jobs.sources import ashby, lever
from apps.jobs.sources.poll import PollState
from tests.factories import make_company, make_job
from tests.jobs.conftest import fixture_json, greenhouse_pages, html, mock_client, routes

db = pytest.mark.django_db(transaction=True)

ANTHROPIC = fixture_json("greenhouse/anthropic.json")
LINEAR = fixture_json("ashby/linear.json")
CRAWL = "CC-MAIN-2026-43"


# --- board ids in URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://boards.greenhouse.io/Stripe/jobs/123", ("greenhouse", "stripe")),
        ("https://job-boards.eu.greenhouse.io/acme", ("greenhouse", "acme")),
        ("https://boards.greenhouse.io/embed/job_board?for=Figma&b=x", ("greenhouse", "figma")),
        ("https://boards-api.greenhouse.io/v1/boards/duolingo/jobs", ("greenhouse", "duolingo")),
        ("https://jobs.ashbyhq.com/Notion/abc-123", ("ashby", "notion")),
        ("https://api.ashbyhq.com/posting-api/job-board/ramp", ("ashby", "ramp")),
        # Lever's ids are case-sensitive: `Palantir` is no board and `palantir` is.
        ("https://jobs.lever.co/palantir/1f0c", ("lever", "palantir")),
        ("https://jobs.lever.co/Wealthfront", ("lever", "Wealthfront")),
        ("https://api.lever.co/v0/postings/spotify?mode=json", ("lever", "spotify")),
        ("https://jobs.smartrecruiters.com/ServiceNow/744000", ("smartrecruiters", "ServiceNow")),
        (
            "https://jobs.smartrecruiters.com/oneclick-ui/company/Visa/publication/1",
            ("smartrecruiters", "Visa"),
        ),
        ("https://jobs.gem.com/Retool/am9icG9zdDo", ("gem", "retool")),
        ("https://apply.workable.com/huggingface/j/ABC123/", ("workable", "huggingface")),
        ("https://acme.recruitee.com/o/engineer", ("recruitee", "acme")),
        (
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x_JR1",
            ("workday", "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"),
        ),
    ],
)
def test_a_board_is_read_out_of_a_url(url: str, board: tuple[str, str]) -> None:
    assert board_in_url(url) == board


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/robots.txt",
        "https://boards.greenhouse.io/embed/job_app?token=1",
        "https://jobs.ashbyhq.com/api/non-user-graphql",
        "https://jobs.ashbyhq.com/some%20company",  # an id that needs escaping is left
        "https://jobs.eu.lever.co/acme",  # another service, which Foray does not read
        "https://www.greenhouse.io/blog/hiring",
        "https://example.com/careers",
        "not a url at all",
    ],
)
def test_what_is_not_a_board_is_nothing(url: str) -> None:
    assert board_in_url(url) is None


def test_a_crawl_s_urls_name_each_board_once_in_its_commonest_spelling() -> None:
    urls = [
        "https://boards.greenhouse.io/stripe/jobs/1",
        "https://boards.greenhouse.io/Stripe/jobs/2",
        "https://jobs.smartrecruiters.com/servicenow/1",
        "https://jobs.smartrecruiters.com/ServiceNow/2",
        "https://jobs.smartrecruiters.com/ServiceNow/3",
        "https://jobs.lever.co/acme",
        "https://jobs.lever.co/Acme",
        "https://example.com/",
    ]
    seen = seen_in_urls(urls, f"commoncrawl:{CRAWL}", CRAWL)
    assert [(s.source, s.slug) for s in seen] == [
        ("greenhouse", "stripe"),
        ("lever", "Acme"),  # case-sensitive: two boards
        ("lever", "acme"),
        ("smartrecruiters", "ServiceNow"),
    ]
    assert {(s.found_in, s.newest) for s in seen} == {(f"commoncrawl:{CRAWL}", CRAWL)}


# --- the candidates table ------------------------------------------------------------------------


@db
def test_a_candidate_seen_again_gains_the_new_place_and_keeps_its_answer() -> None:
    add_candidates(
        [Seen("greenhouse", "acme", "commoncrawl:CC-MAIN-2026-39", 2, "CC-MAIN-2026-39")]
    )
    BoardCandidate.objects.update(status="empty")
    add_candidates(
        [
            Seen("greenhouse", "acme", "commoncrawl:CC-MAIN-2026-43", 1, "CC-MAIN-2026-43"),
            Seen("greenhouse", "acme", "list:someone/new-grad", 1, ""),
            Seen("not-a-source", "x", "file"),
        ]
    )
    candidate = BoardCandidate.objects.get()
    assert candidate.found_in == [
        "commoncrawl:CC-MAIN-2026-39",
        "commoncrawl:CC-MAIN-2026-43",
        "list:someone/new-grad",
    ]
    assert (candidate.seen, candidate.newest, candidate.status) == (2, "CC-MAIN-2026-43", "empty")
    # Read out of a third crawl on its own, it has been seen in three.
    add_candidates(
        [Seen("greenhouse", "acme", "commoncrawl:CC-MAIN-2026-35", 1, "CC-MAIN-2026-35")]
    )
    assert BoardCandidate.objects.get().seen == 3


@db
def test_a_company_that_is_also_a_candidate_is_no_longer_only_the_seed_s() -> None:
    """#28: a board first learned from the old seed, seen again in a public crawl."""
    seeded, _ = upsert_company(
        CompanyInput("Stripe", "stripe", "greenhouse", "Stripe", discovered_via="seed")
    )
    cased, _ = upsert_company(CompanyInput("Acme", "acme", "lever", "Acme", discovered_via="seed"))
    assert seeded.found_in == ["seed"]
    add_candidates(
        [
            Seen("greenhouse", "stripe", f"commoncrawl:{CRAWL}", 3, CRAWL),
            Seen("lever", "acme", f"commoncrawl:{CRAWL}", 1, CRAWL),  # another board: Lever's case
        ]
    )
    assert link_known() == 1
    assert Company.objects.get(id=seeded.id).found_in == ["seed", f"commoncrawl:{CRAWL}"]
    assert Company.objects.get(id=cased.id).found_in == ["seed"]
    statuses = dict(BoardCandidate.objects.values_list("ats_slug", "status"))
    assert statuses == {"stripe": "live", "acme": "new"}
    assert link_known() == 0  # nothing new to learn the second time


@db
def test_registering_a_board_twice_unions_where_it_was_found() -> None:
    first, created = upsert_company(
        CompanyInput("Acme", "acme", "greenhouse", "acme", discovered_via="user")
    )
    again, created_again = upsert_company(
        CompanyInput(
            "Acme", "acme", "greenhouse", "acme", discovered_via="crawl", found_in=["list:a/b"]
        )
    )
    assert (created, created_again) == (True, False)
    assert first.found_in == ["user"] and again.found_in == ["user", "list:a/b"]
    assert again.discovered_via == "user"  # the first way stays the first way


# --- asking the boards ---------------------------------------------------------------------------


def _pages() -> dict[str, httpx2.Response]:
    return {
        **greenhouse_pages("anthropic", ANTHROPIC),
        **greenhouse_pages("hollow", {"jobs": [], "meta": {"total": 0}}),
        **greenhouse_pages("clinic", _clinic()),
        ashby.board_url("linear"): httpx2.Response(200, json=LINEAR),
        "https://jobs.ashbyhq.com/linear": html("<html><title>Linear Jobs</title></html>"),
        lever.board_url("flaky"): httpx2.Response(500, text="oops"),
    }


def _clinic() -> Any:
    """A real board with nothing a software engineer would apply to."""
    job = {**ANTHROPIC["jobs"][0], "id": 1, "title": "Registered Behavior Technician"}
    return {"jobs": [job], "meta": {"total": 1}}


@db
async def test_grow_registers_the_boards_that_answer_and_remembers_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apps.core.http._sleep", _no_sleep)
    await sync_to_async(add_candidates)(
        [
            Seen("greenhouse", "anthropic", f"commoncrawl:{CRAWL}", 4, CRAWL),
            Seen("greenhouse", "hollow", f"commoncrawl:{CRAWL}", 1, CRAWL),
            Seen("greenhouse", "vanished", f"commoncrawl:{CRAWL}", 1, CRAWL),
            Seen("ashby", "linear", f"commoncrawl:{CRAWL}", 2, CRAWL),
            Seen("lever", "flaky", f"commoncrawl:{CRAWL}", 1, CRAWL),
            Seen("greenhouse", "clinic", f"commoncrawl:{CRAWL}", 1, CRAWL),
            Seen("smartrecruiters", "Visa", f"commoncrawl:{CRAWL}", 9, CRAWL),  # not asked (#69)
        ]
    )
    async with mock_client(routes(_pages())) as client:
        summary = await grow(client, limit=50)

    assert summary.asked == 6 and summary.registered == 2
    statuses = await sync_to_async(
        lambda: dict(BoardCandidate.objects.values_list("ats_slug", "status"))
    )()
    assert statuses == {
        "anthropic": "live",
        "hollow": "empty",
        "vanished": "missing",
        "linear": "live",
        "flaky": "error",
        "clinic": "skipped",  # a real board that lists no software role
        "Visa": "new",
    }
    assert not await Company.objects.filter(ats_slug="clinic").aexists()

    anthropic = await Company.objects.aget(ats_slug="anthropic")
    assert anthropic.name == ANTHROPIC["jobs"][0]["company_name"]
    assert (anthropic.discovered_via, anthropic.found_in) == ("crawl", [f"commoncrawl:{CRAWL}"])
    assert anthropic.last_fetch_job_count == len(ANTHROPIC["jobs"])
    # The answer that proved the board was real is the one that was ingested, and the poll
    # state it left means the next scheduled look only asks.
    assert await Job.objects.filter(company_id=anthropic.id).acount() == len(ANTHROPIC["jobs"])
    assert summary.postings >= len(ANTHROPIC["jobs"])
    assert PollState.read(anthropic.poll_state).digest is not None
    # A board's first sight times nothing.
    assert not await Job.objects.filter(company_id=anthropic.id, seen_lag_s__isnull=False).aexists()

    linear = await Company.objects.aget(ats_slug="linear")
    assert linear.name == "Linear"  # from the page people see: the API never says

    # Asked once: a second run has nothing left but, with `retry`, the one that failed.
    async with mock_client(routes(_pages())) as client:
        assert (await grow(client, limit=50)).asked == 0
        assert (await grow(client, limit=50, retry=True)).asked == 1


@db
async def test_any_board_registers_one_that_lists_no_software_role() -> None:
    await sync_to_async(add_candidates)([Seen("greenhouse", "clinic", f"commoncrawl:{CRAWL}")])
    async with mock_client(routes(greenhouse_pages("clinic", _clinic()))) as client:
        summary = await grow(client, any_board=True)
    assert summary.registered == 1


@pytest.mark.parametrize(
    ("title", "software"),
    [
        ("Software Engineer, New Grad", True),
        ("Senior Backend Developer", True),
        ("ML Engineer", True),
        ("AI Researcher", True),
        ("SDE II", True),
        ("Full-Stack Engineer", True),
        ("Data Scientist", True),
        ("Email Marketing Manager", False),  # "ai" and "ml" only as whole words
        ("Registered Nurse", False),
        ("Mechanical Engineer", False),
    ],
)
def test_a_software_title_is_told_loosely(title: str, software: bool) -> None:
    assert radar.is_software_title(title) is software


async def _no_sleep(seconds: float) -> None:
    return None


@db
async def test_candidates_are_asked_newest_seen_first() -> None:
    await sync_to_async(add_candidates)(
        [
            Seen("greenhouse", "old", "commoncrawl:CC-MAIN-2025-47", 9, "CC-MAIN-2025-47"),
            Seen("greenhouse", "recent", f"commoncrawl:{CRAWL}", 1, CRAWL),
            Seen("greenhouse", "recent-often", f"commoncrawl:{CRAWL}", 5, CRAWL),
        ]
    )
    order = await sync_to_async(candidates.next_candidates)(["greenhouse"], 2)
    assert [candidate.ats_slug for candidate in order] == ["recent-often", "recent"]
    # One place's candidates can be asked on their own, whatever their age.
    old = await sync_to_async(candidates.next_candidates)(
        ["greenhouse"], 5, place="commoncrawl:CC-MAIN-2025-47"
    )
    assert [candidate.ats_slug for candidate in old] == ["old"]


@db
async def test_a_workday_site_must_answer_its_own_search_to_be_registered() -> None:
    """Most Workday sites are hospitals and retailers: `matching` keeps those out."""
    tech, shop = "tech.wd1.myworkdayjobs.com/Careers", "shop.wd1.myworkdayjobs.com/Jobs"
    await sync_to_async(add_candidates)(
        [
            Seen("workday", tech, f"commoncrawl:{CRAWL}"),
            Seen("workday", shop, f"commoncrawl:{CRAWL}"),
        ]
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked = json.loads(request.content)
        host = request.url.host
        searching = asked["searchText"] == "software engineer"
        total = 0 if searching and host.startswith("shop") else 40
        items = [
            {"title": "Role", "externalPath": f"/job/x/Role_{n}"} for n in range(min(total, 20))
        ]
        return httpx2.Response(200, json={"total": total, "jobPostings": items})

    async with mock_client(handler) as client:
        summary = await grow(client, sources=["workday"], matching="software engineer")
    assert summary.by_status == {"live": 1, "skipped": 1}
    company = await Company.objects.aget()
    # Named for the tenant, registered unread: its postings cost a request each, and the
    # scheduled refresh reads them within its budget.
    assert (company.name, company.slug, company.ats_slug) == ("Tech", "tech", tech)
    assert company.last_fetched_at is None


# --- Common Crawl's index ------------------------------------------------------------------------


class Index:
    """A crawl's index in miniature: sorted captures in gzip blocks, a `cluster.idx` naming each
    block's first key, and a server that answers byte ranges of both and nothing else."""

    def __init__(self, captures: list[tuple[str, str, str]], per_block: int = 2) -> None:
        lines = sorted(
            f"{key} 20261012000000 {json.dumps({'url': url, 'status': status})}".encode()
            for key, url, status in captures
        )
        self.cdx = b""
        cluster: list[bytes] = []
        for start in range(0, len(lines), per_block):
            block = gzip.compress(b"\n".join(lines[start : start + per_block]) + b"\n")
            first = lines[start].split(b" {", 1)[0]
            cluster.append(
                first + f"\tcdx-00000.gz\t{len(self.cdx)}\t{len(block)}\t{start}".encode()
            )
            self.cdx += block
        self.cluster = b"\n".join(cluster) + b"\n"
        self.ranges = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        body = self.cluster if url.endswith("cluster.idx") else self.cdx
        if "range" not in request.headers:
            return httpx2.Response(200, content=body)  # the whole file: never wanted
        self.ranges += 1
        first, last = request.headers["range"].removeprefix("bytes=").split("-")
        piece = body[int(first) : int(last) + 1]
        end = int(first) + len(piece) - 1
        return httpx2.Response(
            206, content=piece, headers={"content-range": f"bytes {first}-{end}/{len(body)}"}
        )


def _captures() -> list[tuple[str, str, str]]:
    filler = [
        (f"com,site{n:03d})/page", f"https://site{n:03d}.com/page", "200") for n in range(120)
    ]
    boards = [
        (
            "io,greenhouse,boards)/stripe/jobs/1",
            "https://boards.greenhouse.io/stripe/jobs/1",
            "200",
        ),
        ("io,greenhouse,boards)/gone", "https://boards.greenhouse.io/gone", "404"),
        ("io,greenhouse,job-boards)/figma", "https://job-boards.greenhouse.io/figma", "200"),
        ("io,greenhouse)/blog", "https://www.greenhouse.io/blog", "200"),
        ("io,greenhouseother)/x", "https://greenhouseother.io/x", "200"),
        ("io,zzz)/last", "https://zzz.io/last", "200"),
    ]
    return [*filler, *boards]


async def test_the_index_is_bisected_by_range_and_only_matching_blocks_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(crawl_index, "PEEK", 256)
    monkeypatch.setattr(crawl_index, "CHUNK", 512)
    index = Index(_captures())
    async with mock_client(index.handler) as client:
        blocks = await crawl_index.blocks_for(client, CRAWL, "io,greenhouse")
        urls = [url async for url in crawl_index.urls_under(client, CRAWL, "io,greenhouse")]

    # 63 blocks in the index; the prefix touches three or four of them.
    assert 2 <= len(blocks) <= 4
    assert sorted(urls) == [
        "https://boards.greenhouse.io/stripe/jobs/1",
        # Shares the prefix; it names no board, so it is dropped when ids are read out.
        "https://greenhouseother.io/x",
        "https://job-boards.greenhouse.io/figma",
        "https://www.greenhouse.io/blog",
    ]
    seen = seen_in_urls(urls, f"commoncrawl:{CRAWL}", CRAWL)
    assert [(s.source, s.slug) for s in seen] == [("greenhouse", "figma"), ("greenhouse", "stripe")]


async def test_a_prefix_at_either_end_of_the_index_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(crawl_index, "PEEK", 256)
    monkeypatch.setattr(crawl_index, "CHUNK", 512)
    index = Index(_captures())
    async with mock_client(index.handler) as client:
        first = [url async for url in crawl_index.urls_under(client, CRAWL, "com,site000")]
        last = [url async for url in crawl_index.urls_under(client, CRAWL, "io,zzz")]
        none = [url async for url in crawl_index.urls_under(client, CRAWL, "org,nothing")]
    assert first == ["https://site000.com/page"]
    assert last == ["https://zzz.io/last"]
    assert none == []


# --- the commands --------------------------------------------------------------------------------


@db
def test_find_boards_loads_a_file_and_grow_registry_reports_the_width(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = tmp_path / "candidates.tsv"
    listed.write_text(
        "ats\tid\tcrawls_seen\tnewest_crawl\n"
        "greenhouse\tanthropic\t3\t2026-43\n"
        "lever-eu\tacme\t1\t2026-43\n"  # not a source Foray reads
    )
    call_command("find_boards", file=str(listed))
    candidate = BoardCandidate.objects.get()
    assert (candidate.ats_slug, candidate.seen, candidate.newest) == ("anthropic", 3, CRAWL)
    assert candidate.found_in == [f"commoncrawl:{CRAWL}"]

    # A licensed list in whatever shape it comes: every board URL in the file is a candidate.
    licensed = tmp_path / "config.yml"
    licensed.write_text(
        'companies:\n  - name: "Anthropic"\n'
        '    url: "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"\n'
        '  - name: "Docs"\n    url: "https://example.com/readme"\n'
    )
    call_command("find_boards", urls=str(licensed), found_in="list:someone/new-grad")
    assert BoardCandidate.objects.get().found_in == [
        f"commoncrawl:{CRAWL}",
        "list:someone/new-grad",
    ]

    company = make_company(slug="known", ats_slug="known")
    make_job(company=company, title="Software Engineer, New Grad", level="new_grad")
    make_job(company=company, title="Account Executive", level="entry")
    make_job(company=company, title="Staff Software Engineer", level="staff_plus")
    assert radar.early_career_software_roles() == 1

    seed = tmp_path / "seed.json"
    monkeypatch.setattr("apps.jobs.management.commands.grow_registry.SEED_PATH", seed)
    capsys.readouterr()
    call_command("grow_registry", limit=0, write_seed=True)
    out = capsys.readouterr().out
    assert "before: 1 active companies, 3 open postings, 1 early-career software roles" in out
    # Only boards seen somewhere public go into a seed anyone may use; this one was not.
    assert json.loads(seed.read_text()) == []

    Company.objects.filter(id=company.id).update(found_in=["seed", f"commoncrawl:{CRAWL}"])
    call_command("grow_registry", limit=0, write_seed=True)
    assert json.loads(seed.read_text()) == [
        {
            "name": company.name,
            "slug": "known",
            "sourceType": company.source_type,
            "atsSlug": "known",
            "foundIn": [f"commoncrawl:{CRAWL}"],
        }
    ]
