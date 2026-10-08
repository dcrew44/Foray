"""Page endpoints: one read-only view model per Next page (design §5.3). A stub router with its
final prefix; later steps add /pages/home, /pages/matches and the rest."""

from apps.core.router import router

pages = router(tags=["pages"])

ROUTERS = {"/pages": pages}
