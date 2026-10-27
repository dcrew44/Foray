"""`pnpm llm:bakeoff --purpose score --models a:b,c:d [--reference r] [--reasoning e] [--out f]`
(§8.9).

Compares models for one purpose on fixed fixtures and prints a table in the shape of
`console.table`: cost, cost per 500 items, per-call latency (median and 90th percentile), the
reasoning share of output tokens, schema failures, agreement with a reference model and the
fixture's own checks. Its calls are ledgered with `label='bakeoff'` and no user. The score fixture
reads 20 jobs from the app database, the evaluate fixture 8 engineering postings. The reference
runs first; the models then run side by side, each calling sequentially, so a model's latency is
its own and not a queue's. `--out` writes every output as JSON, to read the answers side by side.

A model may carry its own effort, `deepseek:deepseek-flash@low`, so one table can set a model's
efforts against each other; `--reasoning` sets the rest.

Reasoning effort is worth varying on its own: on DeepSeek Flash, reasoning was 92% of a scoring
call's output tokens, so effort moves the bill more than the choice of model does.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import asdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, NoReturn, cast, get_args

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from apps.llm import pricing
from apps.llm.bakeoff import FIXTURES, Fixture, FixtureResult, percentile
from apps.llm.budget import BudgetExceeded
from apps.llm.context import llm_context
from apps.llm.pricing import is_fake
from apps.llm.registry import ModelNotConfiguredError
from foray.env import MODEL_REF_PATTERN, PURPOSES, Effort

USAGE = "usage: pnpm llm:bakeoff --purpose <purpose> --models a:b,c:d [--reference a:b]"
COLUMNS = (
    "model",
    "cost",
    "per500",
    "p50",
    "p90",
    "reasoningShare",
    "schemaFail",
    "agreement",
    "checks",
)


def to_fixed(value: float, digits: int) -> str:
    """JS `Number.prototype.toFixed`: the exact binary value, rounded half up."""
    quantum = Decimal(1).scaleb(-digits)
    return f"{Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP):f}"


def js_round(value: float) -> int:
    """JS `Math.round`: halves round toward +infinity."""
    return math.floor(value + 0.5)


def console_table(rows: list[dict[str, str]]) -> str:
    """Node's `console.table` for rows of strings: an index column, values quoted."""
    headers = ["(index)", *COLUMNS]
    body = [[str(index), *(f"'{row[c]}'" for c in COLUMNS)] for index, row in enumerate(rows)]
    widths = [max(len(cell) for cell in column) + 2 for column in zip(headers, *body, strict=True)]

    def line(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * width for width in widths) + right

    def cells(values: list[str]) -> str:
        return "│" + "│".join(f" {v}".ljust(w) for v, w in zip(values, widths, strict=True)) + "│"

    out = [line("┌", "┬", "┐"), cells(headers), line("├", "┼", "┤")]
    out += [cells(values) for values in body]
    out.append(line("└", "┴", "┘"))
    return "\n".join(out)


def split_spec(spec: str) -> tuple[str, Effort | None]:
    """`provider:model[@effort]` as the ref and its effort (None: `--reasoning`, else the
    purpose's own)."""
    ref, _, effort = spec.partition("@")
    return ref, cast(Effort, effort) if effort else None


def valid_spec(spec: str) -> bool:
    ref, _, effort = spec.partition("@")
    return re.match(MODEL_REF_PATTERN, ref) is not None and (
        not effort or effort in get_args(Effort)
    )


def seconds(ms: int) -> str:
    return f"{to_fixed(ms / 1000, 1)}s"


def table_row(
    model: str, result: FixtureResult, agreement: float | None, checks: str
) -> dict[str, str]:
    total = len(result.outputs) + result.schema_failures
    cost = pricing.cost_of(split_spec(model)[0], result.tokens)
    share = result.tokens.reasoning_tokens / max(result.tokens.output_tokens, 1) * 100
    return {
        "model": model,
        "cost": f"${to_fixed(cost, 4)}",
        # The number that actually decides: one search scores hundreds.
        "per500": f"${to_fixed(cost / total * 500 if total > 0 else 0, 2)}",
        "p50": seconds(percentile(result.call_ms, 0.5)),
        "p90": seconds(percentile(result.call_ms, 0.9)),
        "reasoningShare": f"{js_round(share)}%",
        "schemaFail": f"{result.schema_failures}/{total}",
        "agreement": "—" if agreement is None else to_fixed(agreement, 2),
        "checks": checks,
    }


class Command(BaseCommand):
    help = "Compare models for one purpose on fixed fixtures (cost, latency, schema failures)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--purpose")
        parser.add_argument("--models", default="")
        parser.add_argument("--reference")
        parser.add_argument("--reasoning", choices=get_args(Effort))
        parser.add_argument("--out", type=Path)

    def fail(self, message: str, code: int) -> NoReturn:
        self.stderr.write(message)
        raise SystemExit(code)

    def handle(self, *args: Any, **options: Any) -> None:
        purpose = options["purpose"]
        models = [m for m in (options["models"] or "").split(",") if m]
        reference = options["reference"]
        if not purpose or not models:
            self.fail(USAGE, 2)
        if purpose not in PURPOSES:
            self.fail(f"Not an LLM purpose: {purpose}. Choose from {', '.join(PURPOSES)}.", 2)
        specs = [*models, *([reference] if reference else [])]
        bad = [m for m in specs if not valid_spec(m)]
        if bad:
            self.fail(f'Not a "provider:model" reference: {", ".join(bad)}.', 2)
        real = [m for m in specs if not is_fake(split_spec(m)[0])]
        if settings.ENV.llm_fake and real:
            self.fail(
                f"LLM_FAKE is on, so llm_bakeoff only runs fake models (fake:score), not "
                f"{', '.join(real)}. Unset LLM_FAKE to compare real models.",
                1,
            )

        unpriced = [m for m in models if not pricing.is_priced(split_spec(m)[0])]
        if unpriced:
            self.stderr.write(
                f"warning: no price for {', '.join(unpriced)} — their cost column will read 0. "
                "Add them to apps/llm/pricing.py."
            )
        fixture = FIXTURES.get(purpose)
        if fixture is None:
            self.fail(
                f'No fixtures for "{purpose}" yet. They land with that purpose\'s prompt; '
                "see the comments in FIXTURES (apps/llm/bakeoff.py).",
                1,
            )
        try:
            rows, outputs = asyncio.run(
                self.bakeoff(fixture, models, reference, options["reasoning"])
            )
        except ModelNotConfiguredError as exc:
            self.fail(str(exc), 1)
        except BudgetExceeded as exc:
            # The guard refused before calling the provider (§8.6): no table, since a partial
            # one would rank models on a sample the budget cut short.
            self.fail(exc.user_message, 1)
        if options["out"] is not None:
            options["out"].write_text(json.dumps(outputs, indent=2, default=str) + "\n")
        self.stdout.write(console_table(rows))
        self.stdout.flush()

    async def bakeoff(
        self, fixture: Fixture, models: list[str], reference: str | None, effort: Effort | None
    ) -> tuple[list[dict[str, str]], dict[str, list[Any]]]:
        rows: list[dict[str, str]] = []
        outputs: dict[str, list[Any]] = {}
        with llm_context(label="bakeoff"):
            reference_result = await self.run_one(fixture, reference, effort) if reference else None
            reference_outputs = reference_result.outputs if reference_result else None
            if reference and reference_result:
                outputs[f"reference {reference}"] = reference_result.outputs
            results: list[FixtureResult] = await asyncio.gather(
                *(self.run_one(fixture, model, effort) for model in models)
            )
            for model, result in zip(models, results, strict=True):
                agreement = (
                    fixture.agreement(result.outputs, reference_outputs)
                    if reference_outputs is not None
                    else None
                )
                checks = fixture.checks(result.outputs, reference_outputs)
                rows.append(table_row(model, result, agreement, checks))
                outputs[model] = result.outputs
        return rows, {name: [asdict(item) for item in items] for name, items in outputs.items()}

    async def run_one(self, fixture: Fixture, spec: str, effort: Effort | None) -> FixtureResult:
        ref, own = split_spec(spec)
        return await fixture.run(ref, own or effort)
