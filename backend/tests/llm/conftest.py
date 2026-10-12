"""Fixtures for the LLM layer's tests (design §12.1).

Per-process state is reset around every test here: the budget's cached month-to-date spend, the
limiter's buckets, the pricing module's warned-once set and the fake overrides.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from django.conf import settings

from apps.llm import budget, fake, limiter, pricing


@pytest.fixture(autouse=True)
def fresh_llm_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    budget.reset_cache()
    monkeypatch.setattr(limiter, "_BUCKETS", {})
    monkeypatch.setattr(pricing, "_warned", set())
    monkeypatch.setattr(fake, "_OVERRIDES", {})
    monkeypatch.setattr(fake, "_discovered", True)  # tests register what they need
    yield
    budget.reset_cache()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Change `settings.ENV` for one test: `env(llm_fake=True, llm_fake_delay_ms=50)`."""

    def update(**values: Any) -> Any:
        changed = settings.ENV.model_copy(update=values)
        monkeypatch.setattr(settings, "ENV", changed)
        return changed

    return update
