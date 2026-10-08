"""Fixed-window throttles shared by every process (design §4.5).

A counter is one `rate_limits` row per `(key, window_start)`, where the window start is
`date_bin(window, now(), 'epoch')`. `throttle()` bumps it with one upsert and refuses above the
limit, so the API and the worker share counts with no Redis. Run it in autocommit (outside
`transaction.atomic()`): a rollback would otherwise undo the count.

| Key | Limit |
|---|---|
| `signin-fail:<email>:<client_key>` | 10 failures / 15 min (see below) |
| `signup:<client_key>` | 5 / hour |
| `chat:<user>` | 30 turns / 10 min |
| `upload:<user>` | 20 / hour |
| `company-add:<user>` | 30 / hour (the endpoint and the chat tools) |
| `evaluate:<user>` | 60 / hour |
| `tailor:<user>` | 30 / hour |

The sign-in key is read with `check()` **before** the password is checked, so a locked key gets
429 without hashing, even for the right password; only a failure counts (`throttle()`).

`client_key(request)` trusts `X-Forwarded-For` only when `TRUSTED_PROXY_HOPS >= 1`: Next's rewrite
adds no such header and passes a client-supplied one through, so by default every client is
`"local"`. `maintenance.prune` calls `prune()` to drop counters older than a day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.http import HttpRequest
from django.utils import timezone

from apps.core.errors import ApiError
from apps.core.models import RateLimit

TOO_MANY = "Too many attempts. Try again in a few minutes."


@dataclass(frozen=True, slots=True)
class Limit:
    limit: int
    window_s: int


SIGNIN_FAILURES = Limit(10, 15 * 60)
SIGNUP = Limit(5, 3600)
CHAT = Limit(30, 10 * 60)
UPLOAD = Limit(20, 3600)
COMPANY_ADD = Limit(30, 3600)
EVALUATE = Limit(60, 3600)
TAILOR = Limit(30, 3600)

_BUMP = """
INSERT INTO rate_limits (key, window_start, count)
VALUES (%(key)s, date_bin(%(window)s::interval, now(), 'epoch'), 1)
ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limits.count + 1
RETURNING count
"""

_READ = """
SELECT count FROM rate_limits
WHERE key = %(key)s AND window_start = date_bin(%(window)s::interval, now(), 'epoch')
"""


def _window(window_s: int) -> str:
    if window_s <= 0:
        raise ValueError("a throttle window must be positive")
    return f"{window_s} seconds"  # cast with ::interval in the SQL


def throttle(key: str, limit: int, window_s: int) -> int:
    """Count one attempt against `key`; raise 429 when this one goes over `limit` in the current
    window. Returns the count including this attempt."""
    with connection.cursor() as cursor:
        cursor.execute(_BUMP, {"key": key, "window": _window(window_s)})
        row = cursor.fetchone()
    count = int(row[0]) if row else 0
    if count > limit:
        raise ApiError(429, TOO_MANY)
    return count


def check(key: str, limit: int, window_s: int) -> int:
    """Read `key`'s count in the current window without counting; raise 429 when it has already
    reached `limit`. The sign-in key is checked this way before the password is."""
    count = current(key, window_s)
    if count >= limit:
        raise ApiError(429, TOO_MANY)
    return count


def current(key: str, window_s: int) -> int:
    with connection.cursor() as cursor:
        cursor.execute(_READ, {"key": key, "window": _window(window_s)})
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def client_key(request: HttpRequest) -> str:
    """Who is asking, for per-client keys: the n-th `X-Forwarded-For` entry from the right when
    `TRUSTED_PROXY_HOPS = n >= 1`, else `"local"`. A header with fewer entries than the trusted
    proxies append cannot have come through them, so it counts as `"local"` too."""
    hops = settings.ENV.trusted_proxy_hops
    if hops < 1:
        return "local"
    forwarded = request.headers.get("X-Forwarded-For", "")
    entries = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
    if len(entries) < hops:
        return "local"
    return entries[-hops]


def prune(older_than: timedelta = timedelta(days=1)) -> int:
    """Delete counters whose window started more than `older_than` ago. Returns how many."""
    deleted, _ = RateLimit.objects.filter(window_start__lt=timezone.now() - older_than).delete()
    return deleted
