"""Response schemas several apps answer with (design §5.2).

ninja merges every operation's schemas into `components.schemas` by name, keeping whichever came
last (`ninja/openapi/schema.py`, `add_schema_definitions`). A shape more than one app returns is
therefore defined once, here, and imported: two classes with one name would silently replace
each other in `openapi.json` the day one of them changes. `tests/core/test_openapi_names.py`
fails on a component name that two types produce.
"""

from __future__ import annotations

from apps.core.router import ApiModel

__all__ = ["OkMessageOut"]


# A mutation's answer: `{ok, message}`, the message being the sentence the UI shows. (No class
# docstring: pydantic would publish it as the component's description.)
class OkMessageOut(ApiModel):
    ok: bool
    message: str
