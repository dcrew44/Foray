"""`manage.py facts_benchmark [--postings file] [--labels file] [--show]`: how often
`facts.read_facts` agrees with hand labels (#56).

`tests/fixtures/facts/labels.json` holds 210 postings labelled by hand against
`tests/fixtures/facts/RUBRIC.md`, in three sets: `bench` (the 100 the patterns were tuned on),
`second` (50 more seen while tuning) and `fresh` (60 labelled afterwards and measured once
before any further change). The posting texts are other people's and are not in this
repository: they are read from `--postings` (JSON lines of `{id, title, text}`; the private
notes repository keeps a copy at `notes/benchmarks/posting-facts/postings.jsonl`, which is the
default when it is there), and otherwise from the mirror by id. A posting found in neither is
counted as missing.

Exits 1 when level or years agreement on any set with postings falls below `--min` (90).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.jobs import facts
from apps.jobs.models import Job
from foray.env import REPO_ROOT

LABELS = REPO_ROOT / "backend" / "tests" / "fixtures" / "facts" / "labels.json"
NOTES_POSTINGS = REPO_ROOT / "notes" / "benchmarks" / "posting-facts" / "postings.jsonl"
FIELDS = ("level", "years_min", "work_auth", "clearance")
GATED = ("level", "years_min")


class Command(BaseCommand):
    help = "Measure the posting-facts reader against the hand labels."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--labels", default=str(LABELS))
        parser.add_argument("--postings", help="JSON lines of {id, title, text}")
        parser.add_argument("--show", action="store_true", help="list every disagreement")
        parser.add_argument("--min", type=float, default=90.0, help="percent for level and years")

    def handle(self, *args: Any, **options: Any) -> None:
        labels: list[dict[str, Any]] = json.loads(Path(options["labels"]).read_text())
        texts = self._texts(options["postings"], [label["id"] for label in labels])
        failed: list[str] = []
        for group in dict.fromkeys(label["set"] for label in labels):
            rows = [label for label in labels if label["set"] == group and label["id"] in texts]
            missing = sum(1 for label in labels if label["set"] == group) - len(rows)
            if not rows:
                self.stdout.write(f"{group}: no posting texts found ({missing} missing)")
                continue
            right = dict.fromkeys(FIELDS, 0)
            for label in rows:
                read = facts.read_facts(label["title"], texts[label["id"]])
                for name in FIELDS:
                    got = getattr(read, name)
                    got = got if got is None or name == "years_min" else str(got)
                    if got == label[name]:
                        right[name] += 1
                    elif options["show"]:
                        unsure = " (labeller unsure)" if label["unsure"] else ""
                        self.stdout.write(
                            f"  {group} {name}: {label['title'][:60]!r} "
                            f"label {label[name]}, read {got}{unsure}"
                        )
            parts = [f"{name} {right[name]}/{len(rows)}" for name in FIELDS]
            note = f" ({missing} missing)" if missing else ""
            self.stdout.write(f"{group}: {', '.join(parts)}{note}")
            failed += [
                f"{group} {name}"
                for name in GATED
                if right[name] / len(rows) * 100 < options["min"]
            ]
        if failed:
            raise CommandError(f"below {options['min']:g}%: {', '.join(failed)}")

    def _texts(self, postings: str | None, ids: list[str]) -> dict[str, str]:
        path = Path(postings) if postings else NOTES_POSTINGS
        if postings and not path.exists():
            raise CommandError(f"{path} does not exist")
        texts: dict[str, str] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                texts[row["id"]] = row.get("text") or ""
        absent = [job_id for job_id in ids if job_id not in texts]
        for job_id, text in Job.objects.filter(id__in=absent).values_list("id", "description_text"):
            texts[job_id] = (text or "")[:14000]
        return texts
