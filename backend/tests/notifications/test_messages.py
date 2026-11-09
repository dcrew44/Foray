"""What a notification says, when it may be sent, and how it is published (#59). No database:
the messages are built from unsaved rows, the clock is given, and ntfy is a mock transport.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
import pytest

from apps.jobs.models import Job
from apps.matching.models import JobMatch
from apps.notifications import compose, ntfy
from apps.notifications.compose import Message, age_phrase, match_message, one_line
from apps.notifications.quiet import clock, quiet_until, zone
from foray.env import ConfigurationError, build_env
from tests.factories import sample_deep_eval
from tests.jobs.conftest import mock_client
from tests.notifications.conftest import TOPIC, FakeNtfy

NOW = datetime(2026, 11, 9, 21, 0, tzinfo=UTC)
LA = ZoneInfo("America/Los_Angeles")


# --- the words -----------------------------------------------------------------------------------


def a_match(*, deep: int | None = 86, **job: Any) -> JobMatch:
    fields = {
        "company_name": "Acme",
        "title": "Software Engineer, Payments",
        "posted_at": NOW - timedelta(minutes=40),
        "first_seen_at": NOW - timedelta(minutes=5),
        **job,
    }
    return JobMatch(
        id=uuid.UUID("0198e6a0-0000-7000-8000-000000000001"),
        job=Job(id="greenhouse:acme:1", **fields),
        deep_score=deep,
        deep_eval=sample_deep_eval(deep) if deep is not None else None,
    )


def test_a_match_says_the_company_the_title_the_verdict_and_how_long_it_has_been_up() -> None:
    assert match_message(a_match(), NOW) == Message(
        title="Acme: Software Engineer, Payments",
        body="Strong fit. Posted 40 minutes ago.",
        click="http://localhost:3000/matches/0198e6a0-0000-7000-8000-000000000001",
        tags=("briefcase",),
    )
    assert match_message(a_match(deep=70), NOW).body == "Worth applying. Posted 40 minutes ago."


def test_a_posting_with_no_date_says_when_it_was_first_seen() -> None:
    careers_page = a_match(posted_at=None, first_seen_at=NOW - timedelta(hours=3))
    assert match_message(careers_page, NOW).body == "Strong fit. First seen 3 hours ago."
    # No deep review (not how a watch announces, but the row allows it): the age alone.
    assert match_message(a_match(deep=None), NOW).body == "Posted 40 minutes ago."


@pytest.mark.parametrize(
    ("age", "phrase"),
    [
        (timedelta(seconds=-90), "just now"),  # a board whose clock runs ahead
        (timedelta(seconds=59), "just now"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(minutes=59, seconds=59), "59 minutes ago"),
        (timedelta(hours=1), "1 hour ago"),
        (timedelta(hours=47, minutes=59), "47 hours ago"),
        (timedelta(hours=48), "2 days ago"),
        (timedelta(days=30), "30 days ago"),
    ],
)
def test_ages_read_as_a_person_would_say_them(age: timedelta, phrase: str) -> None:
    assert age_phrase(age) == phrase


def test_third_party_text_is_put_on_one_line_and_cut() -> None:
    assert one_line("  Staff\r\n Engineer\t(Platform)\x00\x1b ", 80) == "Staff Engineer (Platform)"
    assert one_line("a" * 200, 20) == "a" * 19 + "…"
    crowded = a_match(company_name="Acme\nHoldings " + "x" * 100, title="Engineer\n\n" + "y" * 300)
    title = match_message(crowded, NOW).title
    assert "\n" not in title and title.startswith("Acme Holdings x")
    assert len(title) <= compose.COMPANY_MAX + 2 + compose.TITLE_MAX


def test_the_rest_go_in_one_line_that_opens_the_matches_page() -> None:
    assert compose.summary_message(1).title == "And 1 more new match"
    more = compose.summary_message(3)
    assert (more.title, more.click) == ("And 3 more new matches", "http://localhost:3000/matches")


# --- quiet hours ---------------------------------------------------------------------------------

TEN_PM, EIGHT_AM = time(22, 0), time(8, 0)


def la(*when: int) -> datetime:
    return datetime(*when, tzinfo=LA).astimezone(UTC)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("now", "until"),
    [
        (la(2026, 11, 9, 21, 59), None),  # the evening, before they start
        (la(2026, 11, 9, 22, 0), la(2026, 11, 10, 8, 0)),  # the start is inside
        (la(2026, 11, 9, 23, 30), la(2026, 11, 10, 8, 0)),
        (la(2026, 11, 10, 0, 0), la(2026, 11, 10, 8, 0)),  # past midnight: ends today
        (la(2026, 11, 10, 7, 59), la(2026, 11, 10, 8, 0)),
        (la(2026, 11, 10, 8, 0), None),  # the end is outside
        (la(2026, 11, 10, 14, 0), None),
    ],
)
def test_quiet_hours_across_midnight(now: datetime, until: datetime | None) -> None:
    assert quiet_until(now, TEN_PM, EIGHT_AM, LA) == until


def test_quiet_hours_within_one_day_and_none_at_all() -> None:
    siesta = (time(13, 0), time(15, 30))
    assert quiet_until(la(2026, 11, 9, 12, 59), *siesta, LA) is None
    assert quiet_until(la(2026, 11, 9, 13, 0), *siesta, LA) == la(2026, 11, 9, 15, 30)
    assert quiet_until(la(2026, 11, 9, 15, 30), *siesta, LA) is None
    # The same time twice means no quiet hours, at any hour.
    for hour in range(24):
        assert quiet_until(la(2026, 11, 9, hour, 0), TEN_PM, TEN_PM, LA) is None


def test_quiet_hours_are_read_on_the_users_clock() -> None:
    """21:00 UTC is 13:00 in Los Angeles and 06:00 the next morning in Tokyo."""
    assert quiet_until(NOW, TEN_PM, EIGHT_AM, LA) is None
    tokyo = ZoneInfo("Asia/Tokyo")
    assert quiet_until(NOW, TEN_PM, EIGHT_AM, tokyo) == datetime(2026, 11, 9, 23, 0, tzinfo=UTC)


def test_quiet_hours_end_on_the_wall_clock_when_the_clocks_change() -> None:
    # The night of 13 to 14 March 2027 the clocks go forward in Los Angeles: 08:00 is PDT (UTC-7).
    until = quiet_until(la(2027, 3, 13, 23, 0), TEN_PM, EIGHT_AM, LA)
    assert until == datetime(2027, 3, 14, 15, 0, tzinfo=UTC)
    # The hour that comes twice (1 November 2026, 01:00 to 02:00): never a time already past.
    second_one_thirty = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)  # 01:30 PST, the second time
    assert quiet_until(second_one_thirty, time(0, 0), time(1, 45), LA) is None


def test_stored_times_and_zones_read_leniently() -> None:
    assert clock("08:05") == time(8, 5)
    assert zone("America/Los_Angeles") == LA
    assert zone("Not/AZone") == ZoneInfo("UTC") and zone(None) == ZoneInfo("UTC")


# --- publishing ----------------------------------------------------------------------------------

HELLO = Message(title="Acme: Engineer", body="Strong fit.", click="http://localhost:3000/m/1")


def publish(server: FakeNtfy, **client: Any) -> None:
    async def go() -> None:
        async with mock_client(server.handler, **client) as http:
            await ntfy.publish(http, TOPIC, HELLO)

    asyncio.run(go())


def test_publishing_posts_ntfys_json_to_the_configured_server() -> None:
    server = FakeNtfy()
    publish(server)
    [request] = server.requests
    assert (request.method, str(request.url)) == ("POST", "https://ntfy.sh/")
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "topic": TOPIC,
        "title": "Acme: Engineer",
        "message": "Strong fit.",
        "click": "http://localhost:3000/m/1",
    }
    # Tags are sent only when there are some.
    assert ntfy.payload(TOPIC, compose.trial_message())["tags"] == ["white_check_mark"]


@pytest.mark.parametrize(
    ("status", "error", "sentence"),
    [
        (500, "internal server error", "ntfy.sh answered 500: internal server error."),
        (413, None, "ntfy.sh answered 413: refused."),
        (
            429,
            "limit reached: too many requests",
            "ntfy.sh is limiting how many notifications it accepts. Try again in a while.",
        ),
        (
            403,
            "forbidden",
            "ntfy.sh refused this topic: it is reserved or needs a sign-in. Pick another name.",
        ),
    ],
)
def test_a_refusal_is_one_attempt_and_one_sentence(
    status: int, error: str | None, sentence: str
) -> None:
    server = FakeNtfy()
    server.refuse(status, error)
    with pytest.raises(ntfy.PublishError) as refused:
        publish(server)
    assert str(refused.value) == sentence
    # No retry inside the client, even for a 500 or a 429: a repeated POST could deliver twice.
    assert len(server.requests) == 1


def test_an_unreachable_server_and_a_private_one_say_so() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    server = FakeNtfy(answer=down)
    with pytest.raises(ntfy.PublishError) as unreachable:
        publish(server)
    assert str(unreachable.value) == "Could not reach ntfy.sh. Check the connection and try again."
    assert len(server.requests) == 1

    # The guarded client refuses a server that resolves to a private address, before any request.
    private = FakeNtfy()
    with pytest.raises(ntfy.PublishError) as blocked:
        publish(private, addresses={"ntfy.sh": ["10.0.0.5"]})
    assert str(blocked.value).startswith("Foray will not connect to ntfy.sh: it resolves to a")
    assert private.requests == []


def test_a_200_that_is_not_ntfys_answer_is_not_a_delivery() -> None:
    """A redirect to another origin is followed with a plain GET, which answers a web page."""
    page = FakeNtfy(answer=lambda request: httpx2.Response(200, text="<html>ntfy web</html>"))
    with pytest.raises(ntfy.PublishError) as odd:
        publish(page)
    assert str(odd.value) == "ntfy.sh answered, but not as an ntfy server does. Check NTFY_SERVER."


# --- NTFY_SERVER ---------------------------------------------------------------------------------


def test_the_server_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_env().ntfy_server == "https://ntfy.sh"
    monkeypatch.setenv("NTFY_SERVER", "https://push.example.org/")
    assert build_env().ntfy_server == "https://push.example.org"


@pytest.mark.parametrize(
    "value",
    [
        "ntfy.sh",  # no scheme
        "ftp://ntfy.sh",
        "https://ntfy.example.org:8443",  # the guarded client only reaches 80 and 443
        "https://user:secret@ntfy.example.org",
        "https://ntfy.example.org/?auth=secret",
    ],
)
def test_a_server_the_client_could_not_use_is_refused_at_boot(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTFY_SERVER", value)
    with pytest.raises(ConfigurationError) as refused:
        build_env()
    [line] = refused.value.lines
    assert line.startswith("NTFY_SERVER is invalid") and "Fix: set it to your ntfy server" in line
    assert "secret" not in line  # values are never printed
