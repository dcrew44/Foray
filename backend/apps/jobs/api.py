"""Company endpoints (design §5.1, §5.3). A stub router with its final prefix; a later step adds
adding, pausing and refreshing companies."""

from apps.core.router import router

companies = router(tags=["companies"])

ROUTERS = {"/companies": companies}
