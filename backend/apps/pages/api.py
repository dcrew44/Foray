"""Page endpoints: one read-only view model per Next page (design §5.3, §6.1).

Each server component makes one call here (plus `/api/me` from the layout), with the
`X-Foray-SSR` header, so the sliding session never writes during a render (§4.1).

- `GET /api/pages/home`: `{user, onboarding, counts, best, spend, waiting, suggestions,
  headline}`;
- `GET /api/pages/matches?view=&stage=&closed=`: `{counts, matches, ready, view, stage,
  showClosed}`;
- `GET /api/pages/matches/{id}`: `{match, job, tailored, hasBaseResume}`, or 404;
- `GET /api/pages/activity`: `{runs: RunSummary[]}`;
- `GET /api/pages/chat`: `{suggestions}`;
- `GET /api/pages/companies`: `{companies: CompanyRow[], active, openPostings}`;
- `GET /api/pages/profile`: `{hasResume, ready, resume, preferences, progress, slots, base,
  render}`;
- `GET /api/pages/settings`: `{user, settings, review, spend, byPurpose, byModel, byDay,
  monthStart, aggregator, purposes, providers}`.

`onboarding` is `accounts.profile.OnboardingState` (`map/_gaps.md` §3.7 without `hasProfile`);
the profile and settings pages have no `hasProfile` either, because preferences and settings live
on `users` and can always be saved. A malformed, unknown or another user's match id is the same
404, which the page turns into `notFound()`.
"""

from __future__ import annotations

import uuid
from typing import cast

from django.http import HttpRequest

from apps.accounts.models import User
from apps.core.errors import ApiError, messages
from apps.core.router import router
from apps.pages import build
from apps.pages.schemas import (
    ActivityPage,
    ChatPage,
    CompaniesPage,
    HomePage,
    MatchDetailPage,
    MatchesPage,
    ProfilePage,
    SettingsPage,
)

pages = router(tags=["pages"])

ROUTERS = {"/pages": pages}

MATCH_GONE = "That match no longer exists."


def _user(request: HttpRequest) -> User:
    return cast(User, request.user)


@pages.get("/home", response=HomePage, summary="Home page")
def home(request: HttpRequest) -> HomePage:
    return build.home_page(_user(request))


@pages.get("/matches", response=MatchesPage, summary="Matches page")
def matches(
    request: HttpRequest,
    view: str | None = None,
    stage: str | None = None,
    closed: str | None = None,
) -> MatchesPage:
    """`view=list` or a real `stage` shows the list; `closed=1` adds closed matches to the board.
    Anything else is ignored, as the page ignored it."""
    return build.matches_page(_user(request), view=view, stage=stage, closed=closed)


@pages.get("/matches/{id}", response=MatchDetailPage, summary="Match page")
@messages(not_found=MATCH_GONE)
def match(request: HttpRequest, id: uuid.UUID) -> MatchDetailPage:
    page = build.match_page(_user(request), id)
    if page is None:
        raise ApiError(404, MATCH_GONE)
    return page


@pages.get("/activity", response=ActivityPage, summary="Activity page")
def activity(request: HttpRequest) -> ActivityPage:
    return build.activity_page(_user(request))


@pages.get("/chat", response=ChatPage, summary="New chat page")
def chat(request: HttpRequest) -> ChatPage:
    return build.chat_page(_user(request))


@pages.get("/companies", response=CompaniesPage, summary="Companies page")
def companies(request: HttpRequest) -> CompaniesPage:
    return build.companies_page()


@pages.get("/profile", response=ProfilePage, summary="Profile page")
def profile(request: HttpRequest) -> ProfilePage:
    return build.profile_page(_user(request))


@pages.get("/settings", response=SettingsPage, summary="Settings page")
def settings(request: HttpRequest) -> SettingsPage:
    return build.settings_page(_user(request))
