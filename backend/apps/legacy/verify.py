"""The import's hard checks (design §3.4). They run inside the import transaction, before
commit, and again from `verify_import` / `import_legacy --verify-only`.

Both sides are read with SQL: the legacy side in the importer's one `REPEATABLE READ READ ONLY`
snapshot, the target through Django's connection (inside the import transaction when there is
one). JSON columns are compared as Postgres renders `jsonb` (`col::text`), which is canonical on
both sides: key order normalised, numbers as stored. That is stricter than comparing parsed
values, so a float that did not survive the trip would show.

Checks 1-10 are §3.4's. Check 11 covers the columns of the remaining tables (companies, events,
messages, ledger, quotas), so every mapping rule of §3.3 is proved by a check, not only by tests.
Check 9 (legacy untouched) needs fingerprints taken around the import, so the caller runs it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString

import psycopg
from django.db import connection as django_connection
from django.db.backends.base.base import BaseDatabaseWrapper
from psycopg.rows import dict_row

from apps.accounts.hashers import BetterAuthScryptHasher, encode_imported
from apps.accounts.schemas import InterviewState, Preferences, Settings
from apps.core.storage import LocalStorage, StorageError
from apps.jobs.sources.dedupe import content_hash
from apps.legacy import mapping
from apps.legacy.backup import Marker
from apps.legacy.source import diff_mappings, sha256_file

PASSWORD_SHAPE = re.compile(r"^ba_scrypt\$[0-9a-f]{32}\$[0-9a-f]{128}$")
NOT_THE_PASSWORD = "definitely-not-the-password"
SAMPLE_SIZE = 200
MAX_DETAIL = 12  # lines of diff per failing check


# --- reading the two sides -----------------------------------------------------------------------


class Side:
    def rows(
        self, query: LiteralString, params: Sequence[Any] | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


class LegacySide(Side):
    """The legacy snapshot (a psycopg connection inside its read-only transaction)."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn

    def rows(
        self, query: LiteralString, params: Sequence[Any] | None = None
    ) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchall()


class TargetSide(Side):
    """The app database, through Django's connection."""

    def __init__(self, conn: BaseDatabaseWrapper | None = None) -> None:
        self.conn = conn or django_connection

    def rows(
        self, query: LiteralString, params: Sequence[Any] | None = None
    ) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            columns = [col[0] for col in cur.description or ()]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


# --- results -------------------------------------------------------------------------------------


@dataclass
class CheckResult:
    number: int
    name: str
    problems: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    def fail(self, line: str) -> None:
        self.problems.append(line)

    def extend(self, lines: Iterable[str]) -> None:
        self.problems.extend(lines)

    def summary(self) -> str:
        head = f"{'PASS' if self.ok else 'FAIL'}  {self.number:>2}. {self.name}"
        if self.ok:
            return head
        shown = self.problems[:MAX_DETAIL]
        more = len(self.problems) - len(shown)
        tail = [f"        {line}" for line in shown]
        if more:
            tail.append(f"        … and {more} more")
        return "\n".join([head, *tail])


@dataclass
class Verification:
    results: list[CheckResult]
    digests: dict[str, Any]

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    def report(self) -> str:
        return "\n".join(result.summary() for result in self.results)

    def as_json(self) -> list[dict[str, Any]]:
        return [
            {"check": r.number, "name": r.name, "ok": r.ok, "problems": r.problems[:50]}
            for r in self.results
        ]


# --- rendering -----------------------------------------------------------------------------------


def render_ts(value: datetime | None) -> str:
    """ISO 8601 UTC with microseconds; a naive value is UTC wall-clock (Better Auth's tables)."""
    if value is None:
        return ""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha256_lines(lines: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def sha1_text(value: str | None) -> str:
    if value is None:
        return "NULL"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def float_key(value: float | None) -> str:
    return "NULL" if value is None else repr(float(value))


def compare_keyed(
    label: str,
    legacy: dict[Any, Any],
    target: dict[Any, Any],
) -> list[str]:
    """Lines for keys missing on either side and values that differ."""
    lines: list[str] = []
    for key in sorted(legacy.keys() - target.keys(), key=str):
        lines.append(f"{label} {key}: missing from the target")
    for key in sorted(target.keys() - legacy.keys(), key=str):
        lines.append(f"{label} {key}: not in the legacy database")
    for key in sorted(legacy.keys() & target.keys(), key=str):
        if legacy[key] != target[key]:
            lines.append(f"{label} {key}: legacy {legacy[key]!r} != imported {target[key]!r}")
    return lines


# --- the expectations a legacy snapshot implies --------------------------------------------------


@dataclass
class Expectations:
    """What the import must have made of the legacy rows (§3.3), read from the snapshot."""

    converted_runs: set[str]
    pending_approvals: set[str]
    resume_ids: set[str]
    conversation_ids: set[str]
    event_ids: set[int]
    approval_ids: set[str]

    @classmethod
    def read(cls, legacy: Side) -> Expectations:
        def ids(query: LiteralString) -> set[str]:
            return {str(row["id"]) for row in legacy.rows(query)}

        return cls(
            converted_runs=ids(
                "SELECT id FROM runs WHERE status::text IN "
                "('queued', 'running', 'awaiting_approval')"
            ),
            pending_approvals=ids("SELECT id FROM approvals WHERE status::text = 'pending'"),
            resume_ids=ids("SELECT id FROM resumes"),
            conversation_ids=ids("SELECT id FROM conversations"),
            event_ids={int(row["id"]) for row in legacy.rows("SELECT id FROM agent_events")},
            approval_ids=ids("SELECT id FROM approvals"),
        )


# --- the checks ----------------------------------------------------------------------------------


class Verifier:
    def __init__(
        self,
        legacy: Side,
        target: Side,
        *,
        storage_dir: Path,
        expectations: Expectations | None = None,
    ) -> None:
        self.legacy = legacy
        self.target = target
        self.storage_dir = storage_dir
        self.expect = expectations or Expectations.read(legacy)
        self.digests: dict[str, Any] = {}

    def run(self) -> Verification:
        checks: list[tuple[int, str, Callable[[CheckResult], None]]] = [
            (1, "Counts", self.counts),
            (2, "Id-set checksums", self.id_sets),
            (3, "Money", self.money),
            (4, "Matches", self.matches),
            (5, "Jobs", self.jobs),
            (6, "Resumes and documents", self.resumes_and_documents),
            (7, "Users", self.users),
            (8, "Runs and approvals", self.runs_and_approvals),
            (10, "Timestamps", self.timestamps),
            (11, "Other columns", self.other_columns),
        ]
        results: list[CheckResult] = []
        for number, name, check in checks:
            result = CheckResult(number, name)
            try:
                check(result)
            except Exception as exc:  # a check that cannot run is a failed check
                result.fail(f"the check could not run: {type(exc).__name__}: {exc}")
            results.append(result)
        return Verification(results, self.digests)

    # 1 ---------------------------------------------------------------------------------------

    def counts(self, result: CheckResult) -> None:
        counts: dict[str, dict[str, int]] = {}
        for spec in mapping.TABLES:
            legacy = self.legacy.rows(spec.legacy_count_sql)[0]["n"]
            target = self.target.rows(spec.target_count_sql)[0]["n"]
            counts[spec.name] = {"legacy": int(legacy), "imported": int(target)}
            if legacy != target:
                result.fail(f"{spec.name}: legacy {legacy} rows, imported {target}")
        counts["runs"]["converted"] = len(self.expect.converted_runs)
        counts["approvals"]["converted"] = len(self.expect.pending_approvals)
        self.digests["counts"] = counts

    # 2 ---------------------------------------------------------------------------------------

    def id_sets(self, result: CheckResult) -> None:
        digests: dict[str, str] = {}
        for spec in mapping.TABLES:
            legacy = sorted(str(row["id"]) for row in self.legacy.rows(spec.legacy_ids_sql))
            target = sorted(str(row["id"]) for row in self.target.rows(spec.target_ids_sql))
            digests[spec.name] = sha256_lines(target)
            if sha256_lines(legacy) != digests[spec.name]:
                gone = sorted(set(legacy) - set(target))[:5]
                extra = sorted(set(target) - set(legacy))[:5]
                result.fail(f"{spec.name}: ids differ (missing {gone}, unexpected {extra})")
        self.digests["ids"] = digests

    # 3 ---------------------------------------------------------------------------------------

    def money(self, result: CheckResult) -> None:
        total = "SELECT coalesce(sum(cost_usd), 0) AS total, count(*) AS n FROM llm_calls"
        legacy_total = self.legacy.rows(total)[0]
        target_total = self.target.rows(total)[0]
        if (legacy_total["total"], legacy_total["n"]) != (target_total["total"], target_total["n"]):
            result.fail(
                f"ledger: legacy {legacy_total['n']} rows ${legacy_total['total']}, "
                f"imported {target_total['n']} rows ${target_total['total']}"
            )
        by_model = (
            "SELECT purpose::text AS purpose, provider, model, count(*) AS n, "
            "sum(cost_usd) AS usd FROM llm_calls GROUP BY 1, 2, 3"
        )
        by_month = (
            "SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM') AS month, count(*) AS n, "
            "sum(cost_usd) AS usd FROM llm_calls GROUP BY 1"
        )

        def keyed(rows: list[dict[str, Any]], *keys: str) -> dict[Any, Any]:
            return {tuple(row[k] for k in keys): (row["n"], row["usd"]) for row in rows}

        result.extend(
            compare_keyed(
                "spend (purpose, provider, model)",
                keyed(self.legacy.rows(by_model), "purpose", "provider", "model"),
                keyed(self.target.rows(by_model), "purpose", "provider", "model"),
            )
        )
        result.extend(
            compare_keyed(
                "spend per UTC month",
                keyed(self.legacy.rows(by_month), "month"),
                keyed(self.target.rows(by_month), "month"),
            )
        )
        result.extend(
            compare_keyed(
                "api_usage",
                {
                    (row["provider"], row["period"]): row["requests"]
                    for row in self.legacy.rows("SELECT provider, period, requests FROM api_usage")
                },
                {
                    (row["provider"], row["period"]): row["requests"]
                    for row in self.target.rows("SELECT provider, period, requests FROM api_usage")
                },
            )
        )
        result.info["ledger_usd"] = str(target_total["total"])

    # 4 ---------------------------------------------------------------------------------------

    def matches(self, result: CheckResult) -> None:
        columns: LiteralString = (
            "id::text AS id, user_id, job_id, stage::text AS stage, stage_changed_at, fast_score, "
            "deep_score, user_rating, notes, fast_eval::text AS fast_eval, "
            "deep_eval::text AS deep_eval"
        )
        legacy_rows = self.legacy.rows(
            "SELECT " + columns + ", tailored_resume_id::text AS tailored FROM job_matches"
        )
        target_rows = self.target.rows(
            "SELECT " + columns + ", tailored_resume_id::text AS tailored FROM job_matches"
        )

        def shape(row: dict[str, Any], *, legacy: bool) -> tuple[Any, ...]:
            tailored = row["tailored"]
            if legacy and tailored not in self.expect.resume_ids:
                tailored = None  # a dangling soft reference becomes NULL (§3.3)
            return (
                row["user_id"],
                row["job_id"],
                row["stage"],
                render_ts(row["stage_changed_at"]),
                row["fast_score"],
                row["deep_score"],
                row["user_rating"],
                row["notes"],
                row["fast_eval"],
                row["deep_eval"],
                tailored,
            )

        result.extend(
            compare_keyed(
                "match",
                {row["id"]: shape(row, legacy=True) for row in legacy_rows},
                {row["id"]: shape(row, legacy=False) for row in target_rows},
            )
        )
        legacy_stages = Counter(row["stage"] for row in legacy_rows)
        target_stages = Counter(row["stage"] for row in target_rows)
        if legacy_stages != target_stages:
            result.fail(f"stages: legacy {dict(legacy_stages)} != imported {dict(target_stages)}")
        result.info["stages"] = dict(sorted(target_stages.items()))

    # 5 ---------------------------------------------------------------------------------------

    def jobs(self, result: CheckResult) -> None:
        by_source = (
            "SELECT source::text AS source, is_active, count(*) AS n FROM jobs GROUP BY 1, 2"
        )
        result.extend(
            compare_keyed(
                "jobs (source, is_active)",
                {(r["source"], r["is_active"]): r["n"] for r in self.legacy.rows(by_source)},
                {(r["source"], r["is_active"]): r["n"] for r in self.target.rows(by_source)},
            )
        )
        legacy_salaries = {
            row["id"]: (float_key(row["lo"]), float_key(row["hi"]))
            for row in self.legacy.rows(
                "SELECT id, salary_min::float8 AS lo, salary_max::float8 AS hi FROM jobs"
            )
        }
        target_salaries = {
            row["id"]: (float_key(row["lo"]), float_key(row["hi"]))
            for row in self.target.rows("SELECT id, salary_min AS lo, salary_max AS hi FROM jobs")
        }
        result.extend(compare_keyed("job salary", legacy_salaries, target_salaries))

        referenced = {
            row["job_id"] for row in self.legacy.rows("SELECT DISTINCT job_id FROM job_matches")
        }
        ordered = sorted(legacy_salaries, key=lambda i: hashlib.sha256(i.encode()).hexdigest())
        sample = sorted(set(ordered[:SAMPLE_SIZE]) | referenced)
        result.info["sampled"] = len(sample)
        if not sample:
            return
        legacy_rows = self._jobs_by_id(self.legacy, sample, legacy=True)
        target_rows = self._jobs_by_id(self.target, sample, legacy=False)
        result.extend(compare_keyed("job", legacy_rows, target_rows))
        for job_id, row in self._job_hash_inputs(sample).items():
            expected = (
                content_hash(
                    row["title"],
                    row["locations"],
                    row["description_raw"],
                    row["salary_min"],
                    row["salary_max"],
                ),
                mapping.job_dedupe_key(row["company_name"], row["title"], row["locations"]),
            )
            stored = (row["content_hash"], row["dedupe_key"])
            if expected != stored:
                result.fail(f"job {job_id}: content_hash/dedupe_key not recomputed")

    def _jobs_by_id(self, side: Side, ids: list[str], *, legacy: bool) -> dict[str, Any]:
        salary: LiteralString = (
            "salary_min::float8 AS lo, salary_max::float8 AS hi"
            if legacy
            else "salary_min AS lo, salary_max AS hi"
        )
        rows = side.rows(
            "SELECT id, source::text AS source, external_id, company_id::text AS company_id, "
            "company_name, title, locations, location_norm::text AS location_norm, "
            "remote::text AS remote, employment_type, " + salary + ", salary_currency, "
            "salary_period, description_raw, description_text, url, apply_url, posted_at, "
            "raw::text AS raw, is_active, first_seen_at, last_seen_at, fetched_at "
            "FROM jobs WHERE id = ANY(%s)",
            [ids],
        )
        return {
            row["id"]: (
                row["source"],
                row["external_id"],
                row["company_id"],
                row["company_name"],
                row["title"],
                list(row["locations"]),
                row["location_norm"],
                row["remote"],
                row["employment_type"],
                float_key(row["lo"]),
                float_key(row["hi"]),
                row["salary_currency"],
                row["salary_period"],
                sha1_text(row["description_raw"]),
                sha1_text(row["description_text"]),
                row["url"],
                row["apply_url"],
                render_ts(row["posted_at"]),
                sha1_text(row["raw"]),
                row["is_active"],
                render_ts(row["first_seen_at"]),
                render_ts(row["last_seen_at"]),
                render_ts(row["fetched_at"]),
            )
            for row in rows
        }

    def _job_hash_inputs(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        rows = self.target.rows(
            "SELECT id, company_name, title, locations, description_raw, salary_min, salary_max, "
            "content_hash, dedupe_key FROM jobs WHERE id = ANY(%s)",
            [ids],
        )
        return {row["id"]: row for row in rows}

    # 6 ---------------------------------------------------------------------------------------

    def resumes_and_documents(self, result: CheckResult) -> None:
        columns: LiteralString = (
            "id::text AS id, user_id, kind::text AS kind, version, data::text AS data, "
            "changes::text AS changes, verification::text AS verification, "
            "rendered_document_id::text AS rendered"
        )
        legacy = {
            row["id"]: (
                *(
                    row[k]
                    for k in (
                        "user_id",
                        "kind",
                        "version",
                        "data",
                        "changes",
                        "verification",
                        "rendered",
                    )
                ),
                row["match"],
                None,
            )
            for row in self.legacy.rows(
                "SELECT " + columns + ", job_match_id::text AS match FROM resumes"
            )
        }
        target = {
            row["id"]: (
                *(
                    row[k]
                    for k in (
                        "user_id",
                        "kind",
                        "version",
                        "data",
                        "changes",
                        "verification",
                        "rendered",
                    )
                ),
                row["match"],
                row["source"],
            )
            for row in self.target.rows(
                "SELECT " + columns + ", match_id::text AS match, "
                "source_document_id::text AS source FROM resumes"
            )
        }
        result.extend(compare_keyed("resume", legacy, target))

        documents = (
            "SELECT id::text AS id, user_id, kind::text AS kind, filename, mime, size_bytes, "
            "storage_path, sha256, text_extracted FROM documents"
        )

        def doc_shape(row: dict[str, Any]) -> tuple[Any, ...]:
            return (
                row["user_id"],
                row["kind"],
                row["filename"],
                row["mime"],
                row["size_bytes"],
                row["storage_path"],
                row["sha256"],
                sha1_text(row["text_extracted"]),
            )

        target_docs = self.target.rows(documents)
        result.extend(
            compare_keyed(
                "document",
                {row["id"]: doc_shape(row) for row in self.legacy.rows(documents)},
                {row["id"]: doc_shape(row) for row in target_docs},
            )
        )
        hashes = self.target.rows(
            "SELECT count(*) AS n FROM documents WHERE render_hash IS NOT NULL"
        )
        if hashes[0]["n"]:
            result.fail(f"{hashes[0]['n']} imported documents have a render_hash")

        store = LocalStorage(self.storage_dir)
        files: dict[str, str] = {}
        for row in target_docs:
            key = row["storage_path"]
            try:
                exists = store.exists(key)
            except StorageError as exc:
                result.fail(f"document {row['id']}: {exc}")
                continue
            if not exists:
                result.fail(f"document {row['id']}: {key} is missing from STORAGE_DIR")
                continue
            path = (store.root / key).resolve()
            digest, size = sha256_file(path), path.stat().st_size
            files[key] = digest
            if digest != row["sha256"]:
                result.fail(f"document {row['id']}: {key} hashes to {digest}, not {row['sha256']}")
            if size != row["size_bytes"]:
                result.fail(
                    f"document {row['id']}: {key} has {size} bytes, not {row['size_bytes']}"
                )
        result.info["files"] = len(files)
        self.digests["files"] = dict(sorted(files.items()))

    # 7 ---------------------------------------------------------------------------------------

    def users(self, result: CheckResult) -> None:
        legacy_users = {
            row["id"]: row for row in self.legacy.rows('SELECT id, email, name FROM "user"')
        }
        credentials: dict[str, list[str | None]] = {}
        for row in self.legacy.rows(
            "SELECT user_id, password FROM account WHERE provider_id = 'credential'"
        ):
            credentials.setdefault(row["user_id"], []).append(row["password"])
        profiles = {
            row["user_id"]: row
            for row in self.legacy.rows(
                "SELECT user_id, preferences::text AS preferences, "
                "interview_state::text AS interview_state, settings::text AS settings "
                "FROM profiles"
            )
        }
        empty = self.target.rows("SELECT '{}'::jsonb::text AS v")[0]["v"]
        target_users = {
            row["id"]: row
            for row in self.target.rows(
                "SELECT id, email, name, password, preferences::text AS preferences, "
                "interview_state::text AS interview_state, settings::text AS settings "
                "FROM users"
            )
        }
        hasher = BetterAuthScryptHasher()
        for user_id in sorted(legacy_users.keys() & target_users.keys()):
            old, new = legacy_users[user_id], target_users[user_id]
            if new["email"] != old["email"].lower() or new["name"] != old["name"]:
                result.fail(f"user {user_id}: email or name differs")
            stored = credentials.get(user_id, [])
            if stored:
                try:
                    expected = encode_imported(stored[0] or "")
                except ValueError:
                    expected = None
                if not PASSWORD_SHAPE.match(new["password"]):
                    result.fail(f"user {user_id}: the password is not an imported ba_scrypt hash")
                elif new["password"] != expected:
                    result.fail(f"user {user_id}: the password differs from account.password")
                elif hasher.verify(NOT_THE_PASSWORD, new["password"]):
                    result.fail(f"user {user_id}: a wrong password verifies")
            elif not new["password"].startswith("!"):
                result.fail(f"user {user_id}: has no credential account but a usable password")
            profile = profiles.get(user_id)
            for column, model in (
                ("preferences", Preferences),
                ("interview_state", InterviewState),
                ("settings", Settings),
            ):
                expected = profile[column] if profile else empty
                if new[column] != expected:
                    result.fail(f"user {user_id}: {column} differs from the legacy profile")
                try:
                    model.model_validate(json.loads(new[column]))
                except ValueError as exc:
                    result.fail(f"user {user_id}: {column} only reads by falling back: {exc}")
        missing = legacy_users.keys() ^ target_users.keys()
        if missing:
            result.fail(f"users present on one side only: {sorted(missing)[:5]}")

    # 8 ---------------------------------------------------------------------------------------

    def runs_and_approvals(self, result: CheckResult) -> None:
        run_columns: LiteralString = (
            "id::text AS id, user_id, kind::text AS kind, mode::text AS mode, "
            "status::text AS status, input::text AS input, stats::text AS stats, "
            "output::text AS output, error, conversation_id::text AS conversation"
        )
        legacy_runs = {r["id"]: r for r in self.legacy.rows("SELECT " + run_columns + " FROM runs")}
        target_runs = {
            r["id"]: r
            for r in self.target.rows(
                "SELECT " + run_columns + ", dedupe_key, finished_at FROM runs"
            )
        }

        def run_shape(row: dict[str, Any], *, legacy: bool) -> tuple[Any, ...]:
            status, error, conversation = row["status"], row["error"], row["conversation"]
            if legacy:
                if row["id"] in self.expect.converted_runs:
                    status, error = "failed", mapping.INTERRUPTED
                if conversation not in self.expect.conversation_ids:
                    conversation = None
            return (
                row["user_id"],
                row["kind"],
                row["mode"],
                status,
                row["input"],
                row["stats"],
                row["output"],
                error,
                conversation,
            )

        result.extend(
            compare_keyed(
                "run",
                {k: run_shape(v, legacy=True) for k, v in legacy_runs.items()},
                {k: run_shape(v, legacy=False) for k, v in target_runs.items()},
            )
        )
        for run_id, row in target_runs.items():
            if row["dedupe_key"] is not None:
                result.fail(f"run {run_id}: dedupe_key is set")
            if run_id in self.expect.converted_runs and row["finished_at"] is None:
                result.fail(f"run {run_id}: converted without finished_at")
        active = self.target.rows(
            "SELECT count(*) AS n FROM runs WHERE status IN "
            "('queued', 'running', 'awaiting_approval')"
        )[0]["n"]
        if active:
            result.fail(f"{active} imported runs are still active")

        approval_columns: LiteralString = (
            "id::text AS id, user_id, run_id::text AS run_id, kind::text AS kind, title, "
            "payload::text AS payload, status::text AS status, decision::text AS decision, "
            "decided_via::text AS via"
        )
        legacy_approvals = self.legacy.rows(
            "SELECT " + approval_columns + ", continuation->>'queue' AS next_step FROM approvals"
        )
        target_approvals = self.target.rows(
            "SELECT " + approval_columns + ", next_step FROM approvals"
        )

        def approval_shape(row: dict[str, Any], *, legacy: bool) -> tuple[Any, ...]:
            status, via = row["status"], row["via"]
            if legacy and row["id"] in self.expect.pending_approvals:
                status, via = "expired", "expiry"
            return (
                row["user_id"],
                row["run_id"],
                row["kind"],
                row["title"],
                row["payload"],
                status,
                row["decision"],
                via,
                row["next_step"],
            )

        result.extend(
            compare_keyed(
                "approval",
                {r["id"]: approval_shape(r, legacy=True) for r in legacy_approvals},
                {r["id"]: approval_shape(r, legacy=False) for r in target_approvals},
            )
        )
        pending = self.target.rows("SELECT count(*) AS n FROM approvals WHERE status = 'pending'")
        if pending[0]["n"]:
            result.fail(f"{pending[0]['n']} imported approvals are still pending")

    # 10 --------------------------------------------------------------------------------------

    def timestamps(self, result: CheckResult) -> None:
        digests: dict[str, str] = {}
        for spec in mapping.TABLES:
            legacy = self._timestamp_lines(spec, self.legacy, spec.legacy_ts_sql)
            target = self._timestamp_lines(spec, self.target, spec.target_ts_sql)
            digests[spec.name] = sha256_lines(target[key] for key in sorted(target))
            if sha256_lines(legacy[key] for key in sorted(legacy)) != digests[spec.name]:
                result.extend(compare_keyed(f"{spec.name} timestamps", legacy, target))
        self.digests["timestamps"] = digests

    def _timestamp_lines(
        self, spec: mapping.TableSpec, side: Side, query: LiteralString
    ) -> dict[str, str]:
        lines: dict[str, str] = {}
        for row in side.rows(query):
            key = str(row["id"])
            values = [render_ts(row[f"t{i}"]) for i in range(len(spec.timestamps))]
            if spec.name == "runs" and key in self.expect.converted_runs:
                values[spec.converted_ts_index] = "converted"
            lines[key] = "|".join([key, *values])
        return lines

    # 11 --------------------------------------------------------------------------------------

    def other_columns(self, result: CheckResult) -> None:
        company_columns: LiteralString = (
            "id::text AS id, name, domain, slug, source_type::text AS source_type, ats_slug, "
            "careers_urls, tags, status::text AS status, last_fetch_status, "
            "last_fetch_job_count, needs_browser, discovered_via::text AS discovered_via"
        )
        legacy_companies = {
            r["id"]: tuple(r.values())
            for r in self.legacy.rows(
                "SELECT " + company_columns + ", added_by_user_id AS added_by FROM companies"
            )
        }
        target_companies = {
            r["id"]: tuple(r.values())
            for r in self.target.rows(
                "SELECT " + company_columns + ", added_by_id AS added_by FROM companies"
            )
        }
        result.extend(compare_keyed("company", legacy_companies, target_companies))
        caches = self.target.rows(
            "SELECT count(*) AS n FROM companies WHERE listing_cache <> '{}'::jsonb"
        )
        if caches[0]["n"]:
            result.fail(f"{caches[0]['n']} imported companies have a listing_cache")

        event_columns: LiteralString = (
            "id, user_id, run_id::text AS run_id, level::text AS level, kind, message, "
            "data::text AS data, approval_id::text AS approval"
        )

        def event_shape(row: dict[str, Any], *, legacy: bool) -> tuple[Any, ...]:
            approval = row["approval"]
            if legacy and approval not in self.expect.approval_ids:
                approval = None
            return (
                row["user_id"],
                row["run_id"],
                row["level"],
                row["kind"],
                row["message"],
                row["data"],
                approval,
            )

        result.extend(
            compare_keyed(
                "event",
                {
                    r["id"]: event_shape(r, legacy=True)
                    for r in self.legacy.rows("SELECT " + event_columns + " FROM agent_events")
                },
                {
                    r["id"]: event_shape(r, legacy=False)
                    for r in self.target.rows("SELECT " + event_columns + " FROM events")
                },
            )
        )

        conversations = "SELECT id::text AS id, user_id, title FROM conversations"
        result.extend(
            compare_keyed(
                "conversation",
                {r["id"]: (r["user_id"], r["title"]) for r in self.legacy.rows(conversations)},
                {r["id"]: (r["user_id"], r["title"]) for r in self.target.rows(conversations)},
            )
        )
        turns = self.target.rows(
            "SELECT count(*) AS n FROM conversations WHERE turn_started_at IS NOT NULL"
        )
        if turns[0]["n"]:
            result.fail(f"{turns[0]['n']} imported conversations have a turn in progress")

        legacy_messages = self.legacy.rows(
            "SELECT conversation_id::text || '/' || id AS key, user_id, role, "
            "parts::text AS parts, metadata::text AS metadata, metadata->>'kind' AS meta_kind, "
            "metadata->>'eventId' AS event_id, created_at FROM messages "
            "ORDER BY created_at, id"
        )
        expected_messages: dict[str, tuple[Any, ...]] = {}
        for seq, row in enumerate(legacy_messages, start=1):
            event = mapping.run_update_event(
                row["meta_kind"], row["event_id"], self.expect.event_ids
            )
            expected_messages[row["key"]] = (
                seq,
                row["user_id"],
                row["role"],
                row["parts"],
                row["metadata"],
                event,
                render_ts(row["created_at"]),
            )
        result.extend(
            compare_keyed(
                "message",
                expected_messages,
                {
                    r["key"]: (
                        r["seq"],
                        r["user_id"],
                        r["role"],
                        r["parts"],
                        r["metadata"],
                        r["event"],
                        render_ts(r["updated_at"]),
                    )
                    for r in self.target.rows(
                        "SELECT conversation_id::text || '/' || message_id AS key, seq, user_id, "
                        "role, parts::text AS parts, metadata::text AS metadata, "
                        "run_update_event_id AS event, updated_at FROM messages"
                    )
                },
            )
        )

        ledger_columns: LiteralString = (
            "id::text AS id, user_id, run_id::text AS run_id, purpose::text AS purpose, provider, "
            "model, input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, "
            "cost_usd, latency_ms, error"
        )
        result.extend(
            compare_keyed(
                "llm_call",
                {
                    r["id"]: (*tuple(r.values())[1:], None, None)
                    for r in self.legacy.rows("SELECT " + ledger_columns + " FROM llm_calls")
                },
                {
                    r["id"]: tuple(r.values())[1:]
                    for r in self.target.rows(
                        "SELECT " + ledger_columns + ", conversation_id, label FROM llm_calls"
                    )
                },
            )
        )


def check_untouched(
    *,
    fingerprints_before: dict[str, Any],
    fingerprints_after: dict[str, Any],
    files_before: dict[str, Any],
    files_after: dict[str, Any],
    legacy_counts: dict[str, int],
    marker: Marker | None,
) -> CheckResult:
    """Check 9: the legacy tables and files did not change, and the legacy row counts are the
    backup marker's."""
    result = CheckResult(9, "Legacy untouched")
    result.extend(
        f"table {line}" for line in diff_mappings(fingerprints_before, fingerprints_after)
    )
    result.extend(f"file {line}" for line in diff_mappings(files_before, files_after))
    if marker is None:
        result.fail("the backup marker the import used is no longer in BACKUP_DIR")
    else:
        result.extend(
            f"rows in {line} (backup marker {marker.path.name} -> now)"
            for line in diff_mappings(marker.counts, legacy_counts)
        )
    return result
