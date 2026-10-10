"""This process's clock, moved, for the skew tests (step H2).

Stored timestamps are the database's (`apps.core.clock`). A skew test moves Python's clock far
from the database's and checks that nothing stored, and no boundary, follows it.

Patching `django.utils.timezone.now` alone cannot show a regression to a Python-clock default:
Django keeps the function object on the field when the model class is built, so a field declared
`default=timezone.now` would still call the real clock. `python_clock` therefore also swaps the
default of every model field whose default is `timezone.now` (and the field's cached
`_get_default`), and puts everything back on exit.

    with python_clock(skewed(timedelta(minutes=-90))):
        ...
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

__all__ = ["python_clock", "python_clock_defaults", "skewed"]

_REAL_NOW = timezone.now


def skewed(delta: timedelta) -> Callable[[], datetime]:
    """A clock `delta` away from the real one."""
    return lambda: _REAL_NOW() + delta


def python_clock_defaults() -> list[str]:
    """`table.column` of every model field whose default is this process's clock."""
    return [
        f"{model._meta.db_table}.{field.column}"
        for model in django_apps.get_models()
        for field in model._meta.concrete_fields
        if field.default is _REAL_NOW
    ]


@contextmanager
def python_clock(clock: Callable[[], datetime]) -> Iterator[None]:
    """Make `clock` this process's present: `timezone.now()` and every `default=timezone.now`
    field. Other threads (`db()`) see it too: the patch is process-wide."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(timezone, "now", clock)
        for model in django_apps.get_models():
            for field in model._meta.concrete_fields:
                if field.default is _REAL_NOW:
                    patch.setattr(field, "default", clock)
                    # `Field._get_default` is a cached_property holding the old callable.
                    patch.setitem(field.__dict__, "_get_default", clock)
        yield
