"""Matching endpoints (design §5.1, §5.3). Stub routers with their final prefixes; later steps
add starting a search and the match reads and changes."""

from apps.core.router import router

searches = router(tags=["searches"])
matches = router(tags=["matches"])

ROUTERS = {"/searches": searches, "/matches": matches}
