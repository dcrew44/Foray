"""Every router comes from here, so every response is camelCase (design §5.1, §5.2).

ninja 1.7.1 sets `Operation.by_alias = by_alias or False` and dumps responses and schemas with
that explicit value, which overrides pydantic's `serialize_by_alias=True`
(`ninja/operation.py:145,426,444`). Only a `Router(by_alias=True)` gives camelCase on the wire.
`tests/test_register.py` keeps `Router(` out of every other module.
"""

from __future__ import annotations

from typing import Any

from ninja import Router, Schema
from pydantic import ConfigDict
from pydantic.alias_generators import to_camel


def router(**kwargs: Any) -> Router:
    return Router(by_alias=True, **kwargs)


class ApiModel(Schema):
    """The base of every API schema: snake_case in Python, camelCase on the wire. Inputs accept
    both spellings."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
    )
