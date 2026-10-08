"""Conversation endpoints (design §5.1, §5.3). A stub router with its final prefix; a later step
adds listing, reading and deleting conversations. The chat stream itself is `chat.views`."""

from apps.core.router import router

conversations = router(tags=["conversations"])

ROUTERS = {"/conversations": conversations}
