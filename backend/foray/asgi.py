"""ASGI entry point: `uvicorn foray.asgi:application` (never runserver: WSGI buffers streams).

Before it serves anything, the API refuses to boot against a legacy-looking database (§3.1).
uvicorn imports this module inside its event loop, where Django refuses synchronous database
access, so the one check runs on a short-lived connection in a helper thread.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "foray.settings")

application = get_asgi_application()


def _check_database() -> None:
    from django.db import connections

    from apps.core.guards import assert_app_database

    try:
        assert_app_database(connections["default"])
    finally:
        connections.close_all()


with ThreadPoolExecutor(max_workers=1, thread_name_prefix="boot-check") as _pool:
    _pool.submit(_check_database).result()


class _SkipLivePolls(logging.Filter):
    """The live poll runs every 1.5 s; its access-log lines would bury everything else (§13.4)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (
            isinstance(args, tuple) and len(args) >= 3 and str(args[2]).startswith("/api/live")
        )


logging.getLogger("uvicorn.access").addFilter(_SkipLivePolls())
