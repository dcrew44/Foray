"""Column conventions shared by every app (design §2.1).

- **Enums are `TextChoices` plus a database CHECK.** `choices_check("status", RunStatus,
  table="runs")` is `CHECK (status IN (...))` named `runs_status_valid`, so a typo is a write
  error and adding a member is an ordinary migration that swaps the CHECK. A NULL passes, as SQL
  CHECKs do, so nullable enum columns need nothing extra.
- **Scores and their evaluations are written together.** `pairing_checks("fast")` requires
  `fast_score` and `fast_eval` to be both NULL or both set.
- **JSON columns are plain `JSONField`s read through a pydantic model.** Every such model
  inherits `JsonModel` (or `DefaultedJsonModel`): `Model.read(value)` is lenient and never
  raises, and `model.dump()` is what gets written (`model_dump(mode="json", by_alias=True)`).
  Key spelling stays exactly as today: models whose stored keys are camelCase set
  `model_config = CAMEL`; the rest keep their snake_case field names.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Self

from django.db import models
from django.db.models import Q
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

type ChoiceValues = type[models.Choices] | Iterable[str]

# A JSON number as JavaScript writes it: 150000 stays an int and 124.128 a float, so a value read
# and written back keeps its spelling.
JsNumber = int | float  # a plain alias, so JSON schemas inline it as `number`


def _values(choices: ChoiceValues) -> list[str]:
    if isinstance(choices, type) and issubclass(choices, models.Choices):
        return [str(value) for value in choices.values]
    return [str(value) for value in choices]


def choices_check(field: str, choices: ChoiceValues, *, table: str) -> models.CheckConstraint:
    """`CHECK (<field> IN (<values>))`, named `<table>_<field>_valid`."""
    return models.CheckConstraint(
        condition=Q(**{f"{field}__in": _values(choices)}),
        name=f"{table}_{field}_valid",
    )


def choices_checks(table: str, **fields: ChoiceValues) -> list[models.CheckConstraint]:
    """One `choices_check` per enum column: `*choices_checks("runs", kind=RunKind, ...)`."""
    return [choices_check(field, choices, table=table) for field, choices in fields.items()]


def pairing_checks(prefix: str) -> list[models.CheckConstraint]:
    """`<prefix>_score` and `<prefix>_eval` are both NULL or both set (`jm_<prefix>_paired`)."""
    score, evaluation = f"{prefix}_score", f"{prefix}_eval"
    return [
        models.CheckConstraint(
            condition=Q(**{f"{score}__isnull": True, f"{evaluation}__isnull": True})
            | Q(**{f"{score}__isnull": False, f"{evaluation}__isnull": False}),
            name=f"jm_{prefix}_paired",
        )
    ]


# --- JSON columns --------------------------------------------------------------------------------

CAMEL = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)


class JsonModel(BaseModel):
    # Unknown keys are ignored on read, as zod strips them; imported rows keep theirs in the
    # database because nothing rewrites history (§2.3).
    model_config = ConfigDict(extra="ignore")

    @classmethod
    def read(cls, value: Any) -> Self | None:
        """The stored value, or None when it is missing or unreadable. Never raises."""
        if value is None:
            return None
        try:
            return cls.model_validate(value)
        except ValueError, TypeError:  # pydantic's ValidationError is a ValueError
            return None

    @classmethod
    def read_list(cls, value: Any) -> list[Self]:
        """A stored list, keeping the readable items in order. Never raises."""
        if not isinstance(value, list):
            return []
        items = (cls.read(item) for item in value)
        return [item for item in items if item is not None]

    def dump(self, **kwargs: Any) -> dict[str, Any]:
        """What gets written: JSON-safe values under their stored (aliased) keys."""
        return self.model_dump(mode="json", by_alias=True, **kwargs)


class DefaultedJsonModel(JsonModel):
    # Every field has a default. A missing or unreadable value reads as the defaults: today's
    # `safeParse(value ?? {})` with a fallback, so a stored object written by an older version
    # still reads.

    @classmethod
    def read(cls, value: Any) -> Self:
        """The stored value, or the defaults when it is missing or unreadable. Never raises."""
        try:
            return cls.model_validate({} if value is None else value)
        except ValueError, TypeError:  # pydantic's ValidationError is a ValueError
            return cls()
