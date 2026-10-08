"""`POST /api/chat`, the one async endpoint (design §7.3). A stub until the chat step lands, so
the URLconf imports from the first commit."""

from django.http import HttpRequest, HttpResponse


async def chat_view(request: HttpRequest) -> HttpResponse:
    return HttpResponse("Chat is not built yet.", status=503, content_type="text/plain")
