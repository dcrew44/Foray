"""Accounts endpoints (design §4.4, §5.1). Stub routers with their final prefixes; step 3A adds
the operations: sign-up, sign-in, sign-out and config under /auth, GET /me, PUT /preferences and
PATCH /settings."""

from apps.core.router import router

auth = router(tags=["auth"])
me = router(tags=["me"])
preferences = router(tags=["preferences"])
settings = router(tags=["settings"])

ROUTERS = {"/auth": auth, "/me": me, "/preferences": preferences, "/settings": settings}
