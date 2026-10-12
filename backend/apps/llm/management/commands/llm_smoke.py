"""`pnpm llm:smoke [purpose ...]`: one structured call per configured purpose (design §8.9).

Proves the LLM layer end to end: every configured purpose reaches its provider, returns
schema-valid output, and leaves a priced ledger row (`label='smoke'`, no user). The output lines
and exit code are those of `scripts/llm-smoke.ts`. Costs real money, a fraction of a cent, unless
`LLM_FAKE=1` (rows labelled `fake`, $0).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from pydantic import BaseModel, Field

from apps.core.jsonio import js_json
from apps.llm import pricing, registry
from apps.llm.context import llm_context
from apps.llm.structured import structured_result
from foray.env import PURPOSES

INSTRUCTIONS = "Answer factual questions. Respond only with the requested fields."
PROMPT = "Which city is the Golden Gate Bridge in?"
LEDGER_HINT = (
    "select purpose, model, input_tokens, cached_input_tokens, output_tokens, cost_usd "
    "from llm_calls order by created_at desc;"
)


class Answer(BaseModel):
    city: str = Field(description="The city named in the question.")
    confident: bool


def error_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


class Command(BaseCommand):
    help = "Call every configured LLM purpose once and check the ledger rows."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("purposes", nargs="*", metavar="purpose", help="default: all")

    def handle(self, *args: Any, **options: Any) -> None:
        requested = list(options["purposes"])
        unknown = [p for p in requested if p not in PURPOSES]
        if unknown:
            raise CommandError(
                f"Not an LLM purpose: {', '.join(unknown)}. Choose from {', '.join(PURPOSES)}."
            )
        failures = asyncio.run(self.smoke(requested))
        if failures:
            raise SystemExit(1)

    def say(self, line: str) -> None:
        self.stdout.write(line)
        self.stdout.flush()

    async def smoke(self, requested: list[str]) -> int:
        providers = registry.configured_providers()
        self.say(
            f"providers with credentials: {', '.join(providers)}"
            if providers
            else "no provider credentials found — set one in .env (see .env.example)"
        )
        failures = 0
        with llm_context(label="smoke"):
            for configured in registry.configured_purposes():
                purpose, ref = configured.purpose, configured.ref
                if requested and purpose not in requested:
                    continue
                if ref is None:
                    self.say(f"✗ {purpose:<9} {configured.error}")
                    failures += 1
                    continue
                started = time.perf_counter()
                try:
                    result = await structured_result(
                        purpose, Answer, instructions=INSTRUCTIONS, prompt=PROMPT
                    )
                except Exception as exc:
                    failures += 1
                    self.say(f"✗ {purpose:<9} {ref}: {error_text(exc)}")
                    continue
                elapsed = round((time.perf_counter() - started) * 1000)
                usage = result.usage
                retried = "  (retried)" if result.retried else ""
                unpriced = (
                    "" if pricing.is_priced(ref) else "  (unpriced — add it to apps/llm/pricing.py)"
                )
                self.say(
                    f"✓ {purpose:<9} {ref:<32} {elapsed}ms  "
                    f"in={usage.input_tokens} cached={usage.cache_read_tokens} "
                    f"out={usage.output_tokens}{retried}{unpriced}"
                    f"  → {js_json(result.output.model_dump(mode='json'))}"
                )
        self.say(
            "\nall configured purposes answered. Check the ledger:\n  " + LEDGER_HINT
            if failures == 0
            else f"\n{failures} purpose(s) failed."
        )
        return failures
