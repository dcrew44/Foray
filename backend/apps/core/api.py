"""`GET /api/health`, and `GET /api/health/stream` under DEBUG (design §5.3).

Both answer without a session. The stream sends three SSE ticks a second apart; step 1 uses it to
check that nothing between the browser and Django buffers or compresses a stream.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, StreamingHttpResponse

from apps.core.router import ApiModel, router

health = router(tags=["health"])

ROUTERS = {"/health": health}

TICKS = 3
TICK_INTERVAL_S = 1.0


class HealthOut(ApiModel):
    ok: bool


@health.get("", response=HealthOut, auth=None, summary="Health")
def get_health(request: HttpRequest) -> dict[str, bool]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return {"ok": True}


async def _ticks() -> AsyncIterator[bytes]:
    for n in range(TICKS):
        if n:
            await asyncio.sleep(TICK_INTERVAL_S)
        yield f"data: {json.dumps({'n': n})}\n\n".encode()


if settings.DEBUG:

    @health.get("/stream", auth=None, include_in_schema=False)
    async def get_health_stream(request: HttpRequest) -> StreamingHttpResponse:
        # foray.middleware.stream_headers adds no-cache, no-transform and X-Accel-Buffering.
        return StreamingHttpResponse(_ticks(), content_type="text/event-stream")
