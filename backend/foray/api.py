"""The one NinjaAPI, mounted at /api/ (design §5.1).

Every router is mounted here with its final prefix in step 1, so this file never changes again:
each app's `api.py` owns a `ROUTERS` mapping of prefix to router (all built by
`apps.core.router.router()`, so responses are camelCase), and later steps only add operations.
Extra OpenAPI components come from the `apps.core.openapi` registry, so this module imports no
model a later step writes.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.renderers import JSONRenderer
from ninja.security import SessionAuth

from apps.accounts import api as accounts_api
from apps.chat import api as chat_api
from apps.core import api as core_api
from apps.core import errors, openapi
from apps.jobs import api as jobs_api
from apps.matching import api as matching_api
from apps.pages import api as pages_api
from apps.resumes import api as resumes_api
from apps.runs import api as runs_api

# Order matters only for the docs page.
ROUTER_MODULES = (
    accounts_api,
    runs_api,
    matching_api,
    jobs_api,
    resumes_api,
    chat_api,
    pages_api,
    core_api,
)


class CompactJSONRenderer(JSONRenderer):
    """`JSON.stringify` spacing (`{"ok":true}`), UTF-8 kept as is. Datetimes still go through
    ninja's encoder: ISO 8601 with `Z`."""

    json_dumps_params = {"separators": (",", ":"), "ensure_ascii": False}


class ForayAPI(NinjaAPI):
    def create_response(
        self,
        request: HttpRequest,
        data: Any,
        *,
        status: int | None = None,
        temporal_response: HttpResponse | None = None,
    ) -> HttpResponse:
        """Every JSON answer to a GET is `Cache-Control: no-store` (§5.2): Django is the only
        source of truth, and pages are always dynamic."""
        response = super().create_response(
            request, data, status=status, temporal_response=temporal_response
        )
        if request.method == "GET" and not response.has_header("Cache-Control"):
            response["Cache-Control"] = "no-store"
        return response

    def get_openapi_schema(
        self, *, path_prefix: str | None = None, path_params: dict[str, Any] | None = None
    ) -> Any:
        """ninja's schema plus every model registered with `core.openapi.register_schema`."""
        schema = super().get_openapi_schema(path_prefix=path_prefix, path_params=path_params)
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for name, component in openapi.extra_schemas().items():
            components.setdefault(name, component)
        return schema


api = ForayAPI(
    title="Foray",
    version="2",
    # ninja's SessionAuth defaults to csrf=True and then runs CsrfViewMiddleware itself;
    # Foray relies on SameSite=Lax plus the Origin check instead (§4.3).
    auth=SessionAuth(csrf=False),
    urls_namespace="api",
    docs_url="/docs" if settings.DEBUG else None,
    renderer=CompactJSONRenderer(),
)
errors.install(api)

for _module in ROUTER_MODULES:
    for _prefix, _router in _module.ROUTERS.items():
        api.add_router(_prefix, _router)
