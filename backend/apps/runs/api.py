"""Runs endpoints (design §5.1, §5.3). Stub routers with their final prefixes; later steps add
the live snapshot, run details and events, cancel, approval decisions and the worker ping."""

from apps.core.router import router

live = router(tags=["live"])
runs = router(tags=["runs"])
approvals = router(tags=["approvals"])
worker = router(tags=["worker"])

ROUTERS = {"/live": live, "/runs": runs, "/approvals": approvals, "/worker": worker}
