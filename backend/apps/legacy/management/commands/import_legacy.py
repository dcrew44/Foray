"""`pnpm db:import-legacy`: import the old database into this one (design §3.2).

    manage.py import_legacy [--replace] [--verify-only] [--rehearsal]

`--replace` truncates the imported tables first (refused after cutover). `--verify-only` re-runs
the §3.4 checks against an existing import and writes nothing. `--rehearsal` accepts a backup
marker of any age, for a rehearsal against a restored scratch copy.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.legacy.importer import (
    ImportFailure,
    ImportRefused,
    Options,
    Outcome,
    counts_table,
    run_import,
)


def print_outcome(command: BaseCommand, outcome: Outcome) -> None:
    command.stdout.write("Checks:")
    command.stdout.write(outcome.verification.report())
    command.stdout.write(outcome.untouched.summary())


class Command(BaseCommand):
    help = "Import the legacy Foray database and files into this database, all or nothing."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--replace", action="store_true", help="truncate imported tables first")
        parser.add_argument("--verify-only", action="store_true", help="only re-run the checks")
        parser.add_argument("--rehearsal", action="store_true", help="accept an older backup")

    def handle(self, *args: Any, **options: Any) -> None:
        opts = Options(
            replace=options["replace"],
            verify_only=options["verify_only"],
            rehearsal=options["rehearsal"],
        )
        if opts.verify_only and opts.replace:
            raise CommandError(
                "--verify-only writes nothing; it cannot be combined with --replace."
            )
        try:
            outcome = run_import(opts, out=self.stdout.write)
        except ImportRefused as exc:
            raise CommandError(f"Refused: {exc}") from None
        except ImportFailure as exc:
            self.stderr.write("\n".join(exc.lines))
            raise CommandError(
                "The import failed and was rolled back; nothing was written."
            ) from None

        if opts.verify_only:
            print_outcome(self, outcome)
            if not (outcome.verification.ok and outcome.untouched.ok):
                raise CommandError("Verification failed.")
            self.stdout.write(f"Verification passed in {outcome.seconds:.1f} s.")
            return

        record = outcome.record
        assert record is not None
        self.stdout.write(counts_table(record.counts))
        print_outcome(self, outcome)
        report = record.report
        for item in report.get("converted", []):
            self.stdout.write(f"Converted {item['table']} {item['id']}: {item['what']}")
        for item in report.get("reported", []):
            self.stdout.write(f"Reported {item['table']} {item['id']}: {item['what']}")
        notes = report.get("notes", {})
        hashes = notes.get("recomputed_hashes")
        if hashes:
            same_hash, same_key = hashes["content_hash_unchanged"], hashes["dedupe_key_unchanged"]
            self.stdout.write(
                f"Recomputed hashes: content_hash unchanged for {same_hash:,} and dedupe_key for "
                f"{same_key:,} of {hashes['jobs']:,} jobs."
            )
        self.stdout.write(
            f"Files: {len(record.checksums.get('files', {}))} in place, "
            f"{notes.get('files_copied', 0)} copied by this run."
        )
        self.stdout.write(
            f"Imported in {outcome.seconds:.1f} s. Record {record.id} "
            f"({'rehearsal' if opts.rehearsal else 'import'} of {record.legacy_db})."
        )
