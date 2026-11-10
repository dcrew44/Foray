"""`manage.py match_benchmark --purpose score|evaluate --models a:b,c:d@effort [--postings f]`
(#2): how often a model's verdict is the label's, on forty postings read against the sample
candidate (`apps/llm/benchmark.py`).

Real model calls, ledgered as `bakeoff` with no user: forty fast scores cost a few cents
(gpt-5.6-luna: $0.03), forty deep reviews a dollar or two on a strong model. A model named here
is called for real whatever `LLM_FAKE` says, as in `llm_bakeoff`, so with `LLM_FAKE` on the
command refuses any model but the fakes (`--models fake:score`): a rehearsal must not spend.
`--out` writes every answer beside its label.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.llm import bakeoff, benchmark, pricing
from apps.llm.budget import BudgetExceeded
from apps.llm.context import llm_context
from apps.llm.management.commands.llm_bakeoff import split_spec, valid_spec
from foray.env import Effort


class Command(BaseCommand):
    help = "Measure a model's match verdicts against hand labels."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--purpose", choices=("score", "evaluate"), default="score")
        parser.add_argument(
            "--models", required=True, help="provider:model[@effort], comma-separated"
        )
        parser.add_argument("--postings", help="a JSONL file of the labelled postings' text")
        parser.add_argument("--out", help="write every answer beside its label, as JSON")

    def handle(self, *args: Any, **options: Any) -> None:
        postings = Path(options["postings"]) if options["postings"] else None
        labelled = benchmark.load(postings)
        if not labelled:
            raise CommandError(
                "None of the labelled postings' text was found. Pass --postings, or clone the "
                "notes repository (notes/benchmarks/match-quality/postings.jsonl)."
            )
        labels = {item.id: item.label for item in labelled}
        self.stdout.write(
            f"{len(labelled)} labelled postings: "
            + ", ".join(f"{sum(1 for v in labels.values() if v == b)} {b}" for b in benchmark.BANDS)
        )
        answers: dict[str, Any] = {}
        specs = [spec.strip() for spec in options["models"].split(",") if spec.strip()]
        bad = [spec for spec in specs if not valid_spec(spec)]
        if bad or not specs:
            raise CommandError(
                f"Not a model: {', '.join(bad) or '(none given)'}. Use provider:model"
            )
        real = [spec for spec in specs if not pricing.is_fake(split_spec(spec)[0])]
        if settings.ENV.llm_fake and real:
            raise CommandError(
                f"LLM_FAKE is on, and {', '.join(real)} would be called for real. Use "
                f"--models fake:{options['purpose']} to try the command for nothing, or unset "
                "LLM_FAKE to measure a model."
            )
        for ref, effort in map(split_spec, specs):
            try:
                result = asyncio.run(self._run(options["purpose"], ref, effort, labelled))
            except BudgetExceeded as refused:
                raise CommandError(str(refused)) from None
            scores = {item.job_id: item.score for item in result.outputs}
            found = benchmark.agreement(labels, scores)
            name = f"{ref}@{effort}" if effort else ref
            cost = pricing.cost_of(ref, result.tokens)
            self.stdout.write(
                f"\n{name}: ${cost:.4f}, {result.schema_failures} failed of {len(labelled)}"
            )
            self.stdout.write(
                f"  the label's band exactly: {found.share(found.exact, found.judged)}; "
                f"within one band: {found.share(found.within_one, found.judged)}"
            )
            self.stdout.write(
                f"  kept (at {benchmark.KEEP_AT}+) of those worth applying to: "
                f"{found.share(found.kept, found.worth)}; "
                f"archived of those to skip: {found.share(found.archived, found.skips)}"
            )
            self.stdout.write("  label \\ model   " + "  ".join(f"{b:>6}" for b in benchmark.BANDS))
            for label in reversed(benchmark.BANDS):
                row = found.confusion[label]
                self.stdout.write(
                    f"  {label:<15}" + "  ".join(f"{row.get(b, 0):>6}" for b in benchmark.BANDS)
                )
            answers[name] = [
                {"id": job_id, "label": labels[job_id], "score": score}
                for job_id, score in scores.items()
            ]
        if options["out"]:
            Path(options["out"]).write_text(json.dumps(answers, indent=1) + "\n")
            self.stdout.write(f"\nwrote {options['out']}")

    async def _run(
        self, purpose: str, ref: str, effort: Effort | None, labelled: list[benchmark.Labelled]
    ) -> bakeoff.FixtureResult:
        with llm_context(label="bakeoff"):
            if purpose == "score":
                return await bakeoff.run_scoring_fixture(
                    ref, effort, sample=benchmark.score_sample(labelled)
                )
            return await bakeoff.run_evaluation_fixture(
                ref, effort, sample=benchmark.eval_sample(labelled)
            )
