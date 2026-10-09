"""`manage.py import_legacy`: the one-off, all-or-nothing import of the old database (design §3).

1. **Preflight** refuses, before anything but the guard queries has run on either database,
   when the configuration is incomplete, the two URLs name one database, the target looks like
   the legacy database or was not migrated by Django, the source lacks the legacy tables, no
   fresh backup marker exists, the target already has data (without `--replace`), or an import
   was marked cutover (with `--replace`).
2. **Fingerprints before**: every legacy table's `(count, sum(xmin))` and every legacy file.
3. **One snapshot** of the legacy side (`REPEATABLE READ READ ONLY`), read with psycopg.
4. **One target transaction**: rows are written in §3.3 order in batches of 1,000, timestamps as
   read; files are copied (temp, fsync, rename) and recorded; the §3.4 checks run inside; the
   fingerprints are re-read on fresh connections and compared. Any failure rolls everything
   back and deletes exactly the files this run created.
5. **Record**: a `legacy_imports` row with counts, checksums, fingerprints and the report, in the
   same transaction.

There are no data repairs: a row the new schema cannot hold is a hard failure that names it.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString

import psycopg
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import (
    DEFAULT_DB_ALIAS,
    DatabaseError,
    DataError,
    IntegrityError,
    connection,
    connections,
    models,
    transaction,
)
from django.db.models import Value
from django.utils import timezone
from psycopg.rows import dict_row

from apps.accounts.hashers import encode_imported
from apps.accounts.models import User
from apps.chat.models import Conversation, Message, Role
from apps.core.guards import assert_app_database
from apps.core.scoping import UserOwned
from apps.core.storage import LocalStorage, StorageError, sha256_hex
from apps.jobs.models import (
    ApiUsage,
    Company,
    CompanyStatus,
    DiscoveredVia,
    FetchStatus,
    Job,
    JobSource,
    Period,
    Remote,
    SourceType,
)
from apps.jobs.sources.dedupe import content_hash
from apps.legacy import mapping, source
from apps.legacy.backup import MARKER_MAX_AGE, Marker, MarkerError, fresh_marker, read_marker
from apps.legacy.models import ImportRecord, ImportStatus
from apps.legacy.verify import (
    CheckResult,
    Expectations,
    LegacySide,
    TargetSide,
    Verification,
    Verifier,
    check_untouched,
)
from apps.llm.models import LlmCall, Purpose
from apps.matching.models import JobMatch, Stage
from apps.resumes.models import Document, DocumentKind, Resume, ResumeKind
from apps.runs.catalogue import (
    ApprovalKind,
    ApprovalStatus,
    DecidedVia,
    Level,
    Mode,
    NextStep,
    RunKind,
    RunStatus,
)
from apps.runs.models import Approval, Event, Run

BATCH = 1000


class ImportRefused(Exception):
    """A preflight refusal: nothing was written."""


class ImportFailure(Exception):
    """A hard failure: everything was rolled back."""

    def __init__(self, lines: Sequence[str]) -> None:
        super().__init__("\n".join(lines))
        self.lines = list(lines)


# --- configuration -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    legacy_url: str
    legacy_db: str
    legacy_storage: Path
    storage_dir: Path
    backup_dir: Path
    target_db: str
    app_timezone: str

    @classmethod
    def from_settings(cls) -> Config:
        env = settings.ENV
        if not env.legacy_database_url:
            raise ImportRefused(
                "LEGACY_DATABASE_URL is not set. Point it at the old database (…/foray)."
            )
        if env.legacy_storage_dir is None:
            raise ImportRefused(
                "LEGACY_STORAGE_DIR is not set. Point it at the old data/ folder (absolute path)."
            )
        legacy_storage = Path(env.legacy_storage_dir)
        storage_dir = Path(settings.STORAGE_DIR).resolve()
        if not legacy_storage.is_dir():
            raise ImportRefused(f"LEGACY_STORAGE_DIR ({legacy_storage}) is not a directory.")
        legacy_root = legacy_storage.resolve()
        if storage_dir == legacy_root or storage_dir.is_relative_to(legacy_root):
            raise ImportRefused(
                "STORAGE_DIR is inside LEGACY_STORAGE_DIR; the legacy tree is never written."
            )
        if legacy_root.is_relative_to(storage_dir):
            raise ImportRefused("LEGACY_STORAGE_DIR is inside STORAGE_DIR; keep them apart.")
        target_db = str(connection.settings_dict["NAME"])
        legacy_db = source.database_name(env.legacy_database_url)
        if legacy_db == target_db:
            raise ImportRefused(
                f'LEGACY_DATABASE_URL and DATABASE_URL both name "{target_db}". The legacy URL '
                "must name the old database, DATABASE_URL the new one."
            )
        return cls(
            legacy_url=env.legacy_database_url,
            legacy_db=legacy_db,
            legacy_storage=legacy_storage,
            storage_dir=storage_dir,
            backup_dir=Path(settings.BACKUP_DIR),
            target_db=target_db,
            app_timezone=env.app_timezone,
        )


@dataclass(frozen=True)
class Options:
    replace: bool = False
    verify_only: bool = False
    rehearsal: bool = False


# --- guards --------------------------------------------------------------------------------------


def guard_target() -> None:
    """The target is the app database: not legacy-looking, and migrated by Django."""
    try:
        assert_app_database(connections[DEFAULT_DB_ALIAS])
    except ImproperlyConfigured as exc:
        raise ImportRefused(str(exc)) from None
    with connection.cursor() as cur:
        cur.execute(
            "SELECT CASE WHEN to_regclass('public.django_migrations') IS NULL THEN 0 "
            "ELSE (SELECT count(*) FROM django_migrations WHERE app = 'legacy') END"
        )
        row = cur.fetchone()
    if not row or not row[0]:
        raise ImportRefused(
            f'Database "{connection.settings_dict["NAME"]}" was not migrated by Django (no '
            "legacy rows in django_migrations). Run `manage.py migrate` against it first."
        )


def guard_source(cfg: Config) -> None:
    try:
        with source.session(cfg.legacy_url) as conn:
            source.assert_legacy_source(conn)
    except source.LegacyError as exc:
        raise ImportRefused(str(exc)) from None
    except psycopg.OperationalError as exc:
        raise ImportRefused(f"Could not open the legacy database: {exc}".strip()) from None


def target_tables_with_rows() -> list[str]:
    found: list[str] = []
    with connection.cursor() as cur:
        for name in mapping.IMPORTED_TABLES:
            cur.execute(f'SELECT EXISTS (SELECT 1 FROM "{name}")')
            row = cur.fetchone()
            if row and row[0]:
                found.append(name)
    return found


def cutover_marked() -> bool:
    return ImportRecord.objects.filter(cutover=True).exists()


# --- small helpers -------------------------------------------------------------------------------


def aware(value: datetime | None) -> datetime | None:
    """A naive legacy timestamp is UTC wall-clock (Better Auth's tables)."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def json_value(text: str | None) -> Any:
    """A `jsonb::text` value as the ORM writes it back: SQL NULL stays NULL, JSON `null` stays
    JSON `null` (`Value(None, JSONField())`), anything else is the parsed value."""
    if text is None:
        return None
    parsed = json.loads(text)
    if parsed is None:
        return Value(None, output_field=models.JSONField())
    return parsed


def require_choice(table: str, row_id: Any, column: str, value: Any, choices: Any) -> None:
    if value is not None and value not in choices.values:
        raise ImportFailure(
            [
                f"{table} row {row_id}: {column} = {value!r} is not a value the new schema has "
                f"({', '.join(choices.values)})"
            ]
        )


def chunked[T](items: Iterable[T], size: int = BATCH) -> Iterator[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def bulk_writer(model: type[models.Model]) -> Callable[[list[Any]], Any]:
    if issubclass(model, UserOwned):
        return model.objects.unscoped("the legacy import writes every user's rows").bulk_create
    return model._default_manager.bulk_create


# --- the run's log -------------------------------------------------------------------------------


@dataclass
class Log:
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    converted: list[dict[str, Any]] = field(default_factory=list)
    reported: list[dict[str, Any]] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    def count(self, table: str, *, legacy: int, imported: int, converted: int = 0) -> None:
        self.counts[table] = {"legacy": legacy, "imported": imported, "converted": converted}

    def report(self, table: str, row_id: Any, what: str) -> None:
        self.reported.append({"table": table, "id": str(row_id), "what": what})

    def convert(self, table: str, row_id: Any, what: str) -> None:
        self.converted.append({"table": table, "id": str(row_id), "what": what})


class Files:
    """Copies legacy files into `STORAGE_DIR`, remembering exactly what it created."""

    def __init__(self, legacy_root: Path, target_root: Path) -> None:
        self.legacy = LocalStorage(legacy_root)
        self.target = LocalStorage(target_root)
        self.created: list[Path] = []
        self.created_dirs: list[Path] = []
        self.copied: dict[str, str] = {}

    def copy(self, doc_id: Any, key: str, sha256: str) -> None:
        try:
            with self.legacy.open(key) as handle:
                data = handle.read()
        except FileNotFoundError:
            raise ImportFailure(
                [f"documents row {doc_id}: {key} is missing from LEGACY_STORAGE_DIR"]
            ) from None
        except (StorageError, OSError) as exc:
            raise ImportFailure([f"documents row {doc_id}: {exc}"]) from None
        digest = sha256_hex(data)
        if digest != sha256:
            raise ImportFailure(
                [f"documents row {doc_id}: {key} hashes to {digest}, not the recorded {sha256}"]
            )
        try:
            exists = self.target.exists(key)
        except StorageError as exc:
            raise ImportFailure([f"documents row {doc_id}: {exc}"]) from None
        dest = (self.target.root / key).resolve()
        if exists:
            if source.sha256_file(dest) != sha256:
                raise ImportFailure(
                    [f"documents row {doc_id}: STORAGE_DIR already holds a different {key}"]
                )
            self.copied[key] = sha256  # already in place (an earlier import of the same file)
            return
        missing_dirs = [
            p
            for p in dest.parents
            if p.is_relative_to(self.target.root) and p != self.target.root and not p.exists()
        ]
        self.target.put(key, data)
        self.created_dirs.extend(sorted(missing_dirs, key=lambda p: len(p.parts)))
        self.created.append(dest)
        self.copied[key] = sha256

    def rollback(self) -> None:
        for path in reversed(self.created):
            path.unlink(missing_ok=True)
        for directory in sorted(self.created_dirs, key=lambda p: len(p.parts), reverse=True):
            with contextlib.suppress(OSError):  # not empty: something else lives there now
                directory.rmdir()
        self.created.clear()
        self.created_dirs.clear()


# --- the import ----------------------------------------------------------------------------------


@dataclass
class Outcome:
    record: ImportRecord | None
    verification: Verification
    untouched: CheckResult
    log: Log
    seconds: float
    preflight_notes: dict[str, Any]


class Importer:
    def __init__(self, cfg: Config, *, out: Callable[[str], None] = print) -> None:
        self.cfg = cfg
        self.out = out
        self.log = Log()
        self.now = timezone.now()

    # -- reading ----------------------------------------------------------------------------

    def rows(self, conn: psycopg.Connection[Any], query: LiteralString) -> list[dict[str, Any]]:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query)
            return cur.fetchall()

    def stream(
        self, conn: psycopg.Connection[Any], name: str, query: LiteralString
    ) -> Iterator[dict[str, Any]]:
        with conn.cursor(name=name, row_factory=dict_row) as cur:
            cur.itersize = BATCH
            cur.execute(query)
            yield from cur

    def write(self, model: type[models.Model], table: str, objs: Iterable[Any]) -> int:
        """Bulk-insert in batches; a refused batch is retried row by row to name the row."""
        insert = bulk_writer(model)
        written = 0
        for batch in chunked(objs):
            try:
                with transaction.atomic():
                    insert(batch)
            except (IntegrityError, DataError) as exc:
                for obj in batch:
                    try:
                        with transaction.atomic():
                            insert([obj])
                    except (IntegrityError, DataError) as row_exc:
                        raise ImportFailure(
                            [f"{table} row {obj.pk}: {first_line(row_exc)}"]
                        ) from row_exc
                raise ImportFailure([f"{table}: {first_line(exc)}"]) from exc
            written += len(batch)
        return written

    # -- tables, in §3.3 order --------------------------------------------------------------

    def users(self, conn: psycopg.Connection[Any]) -> None:
        users = self.rows(conn, 'SELECT id, name, email, created_at, updated_at FROM "user"')
        credentials: dict[str, list[dict[str, Any]]] = {}
        for row in self.rows(
            conn,
            "SELECT id, user_id, password FROM account WHERE provider_id = 'credential' "
            "ORDER BY id",
        ):
            credentials.setdefault(row["user_id"], []).append(row)
        profiles = {
            row["user_id"]: row
            for row in self.rows(
                conn,
                "SELECT user_id, preferences::text AS preferences, "
                "interview_state::text AS interview_state, settings::text AS settings "
                "FROM profiles",
            )
        }
        objs: list[User] = []
        for row in users:
            accounts = credentials.get(row["id"], [])
            if len(accounts) > 1:
                raise ImportFailure(
                    [
                        f"user row {row['id']}: has {len(accounts)} credential "
                        "accounts; expected at most one"
                    ]
                )
            user = User(
                id=row["id"],
                email=row["email"].lower(),
                name=row["name"],
                is_active=True,
                last_login=None,
                timezone=self.cfg.app_timezone,
                created_at=aware(row["created_at"]),
                updated_at=aware(row["updated_at"]),
            )
            if accounts:
                try:
                    user.password = encode_imported(accounts[0]["password"] or "")
                except ValueError:
                    raise ImportFailure(
                        [
                            f"account row {accounts[0]['id']} (user {row['id']}): the password is "
                            "not a Better Auth scrypt hash (<32 hex>:<128 hex>)"
                        ]
                    ) from None
            else:
                user.set_unusable_password()
                self.log.report("users", row["id"], "no credential account: unusable password")
            profile = profiles.get(row["id"])
            user.preferences = json_value(profile["preferences"]) if profile else {}
            user.interview_state = json_value(profile["interview_state"]) if profile else {}
            user.settings = json_value(profile["settings"]) if profile else {}
            objs.append(user)
        written = self.write(User, "users", objs)
        self.log.count("users", legacy=len(users), imported=written)

    def companies(self, conn: psycopg.Connection[Any]) -> None:
        rows = self.rows(
            conn,
            "SELECT id, name, domain, slug, source_type::text AS source_type, ats_slug, "
            "careers_urls, tags, status::text AS status, last_fetched_at, last_fetch_status, "
            "last_fetch_job_count, needs_browser, discovered_via::text AS discovered_via, "
            "added_by_user_id, created_at, updated_at FROM companies ORDER BY id",
        )
        objs: list[Company] = []
        for row in rows:
            require_choice("companies", row["id"], "source_type", row["source_type"], SourceType)
            require_choice("companies", row["id"], "status", row["status"], CompanyStatus)
            require_choice(
                "companies", row["id"], "last_fetch_status", row["last_fetch_status"], FetchStatus
            )
            require_choice(
                "companies", row["id"], "discovered_via", row["discovered_via"], DiscoveredVia
            )
            objs.append(
                Company(
                    id=row["id"],
                    name=row["name"],
                    domain=row["domain"],
                    slug=row["slug"],
                    source_type=row["source_type"],
                    ats_slug=row["ats_slug"],
                    careers_urls=row["careers_urls"],
                    tags=row["tags"],
                    status=row["status"],
                    last_fetched_at=row["last_fetched_at"],
                    last_fetch_status=row["last_fetch_status"],
                    last_fetch_job_count=row["last_fetch_job_count"],
                    needs_browser=row["needs_browser"],
                    listing_cache={},
                    discovered_via=row["discovered_via"],
                    added_by_id=row["added_by_user_id"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
        self.log.count(
            "companies", legacy=len(rows), imported=self.write(Company, "companies", objs)
        )

    def jobs(self, conn: psycopg.Connection[Any]) -> None:
        same_hash = same_key = legacy_rows = 0

        def build() -> Iterator[Job]:
            nonlocal same_hash, same_key, legacy_rows
            for row in self.stream(
                conn,
                "legacy_jobs",
                "SELECT id, source::text AS source, external_id, company_id, company_name, "
                "title, locations, location_norm::text AS location_norm, remote::text AS remote, "
                "employment_type, salary_min::float8 AS salary_min, "
                "salary_max::float8 AS salary_max, salary_currency, salary_period, "
                "description_raw, description_text, url, apply_url, posted_at, raw::text AS raw, "
                "content_hash, dedupe_key, is_active, first_seen_at, last_seen_at, fetched_at "
                "FROM jobs ORDER BY id",
            ):
                legacy_rows += 1
                require_choice("jobs", row["id"], "source", row["source"], JobSource)
                require_choice("jobs", row["id"], "remote", row["remote"], Remote)
                require_choice("jobs", row["id"], "salary_period", row["salary_period"], Period)
                locations = list(row["locations"])
                new_hash = content_hash(
                    row["title"],
                    locations,
                    row["description_raw"],
                    row["salary_min"],
                    row["salary_max"],
                )
                new_key = mapping.job_dedupe_key(row["company_name"], row["title"], locations)
                same_hash += new_hash == row["content_hash"]
                same_key += new_key == row["dedupe_key"]
                yield Job(
                    id=row["id"],
                    source=row["source"],
                    external_id=row["external_id"],
                    company_id=row["company_id"],
                    company_name=row["company_name"],
                    title=row["title"],
                    locations=locations,
                    location_norm=json_value(row["location_norm"]),
                    remote=row["remote"],
                    employment_type=row["employment_type"],
                    salary_min=row["salary_min"],
                    salary_max=row["salary_max"],
                    salary_currency=row["salary_currency"],
                    salary_period=row["salary_period"],
                    description_raw=row["description_raw"],
                    description_text=row["description_text"],
                    url=row["url"],
                    apply_url=row["apply_url"],
                    posted_at=row["posted_at"],
                    raw=json_value(row["raw"]),
                    content_hash=new_hash,
                    dedupe_key=new_key,
                    is_active=row["is_active"],
                    first_seen_at=row["first_seen_at"],
                    last_seen_at=row["last_seen_at"],
                    fetched_at=row["fetched_at"],
                )

        written = self.write(Job, "jobs", build())
        self.log.count("jobs", legacy=legacy_rows, imported=written)
        self.log.notes["recomputed_hashes"] = {
            "jobs": legacy_rows,
            "content_hash_unchanged": same_hash,
            "dedupe_key_unchanged": same_key,
        }

    def job_matches(self, conn: psycopg.Connection[Any], resume_ids: set[str]) -> list[Any]:
        rows = self.rows(
            conn,
            "SELECT id, user_id, job_id, stage::text AS stage, stage_changed_at, fast_score, "
            "fast_eval::text AS fast_eval, deep_score, deep_eval::text AS deep_eval, "
            "user_rating, notes, tailored_resume_id, created_at, updated_at "
            "FROM job_matches ORDER BY id",
        )
        objs: list[JobMatch] = []
        tailored: list[tuple[Any, Any]] = []
        for row in rows:
            require_choice("job_matches", row["id"], "stage", row["stage"], Stage)
            ref = row["tailored_resume_id"]
            if ref is not None:
                if str(ref) in resume_ids:
                    tailored.append((row["id"], ref))
                else:
                    what = f"tailored_resume_id {ref} names no resume: set to NULL"
                    self.log.report("job_matches", row["id"], what)
            objs.append(
                JobMatch(
                    id=row["id"],
                    user_id=row["user_id"],
                    job_id=row["job_id"],
                    stage=row["stage"],
                    stage_changed_at=row["stage_changed_at"],
                    fast_score=row["fast_score"],
                    fast_eval=json_value(row["fast_eval"]),
                    deep_score=row["deep_score"],
                    deep_eval=json_value(row["deep_eval"]),
                    user_rating=row["user_rating"],
                    notes=row["notes"],
                    tailored_resume=None,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
        written = self.write(JobMatch, "job_matches", objs)
        self.log.count("job_matches", legacy=len(rows), imported=written)
        return tailored

    def documents(self, conn: psycopg.Connection[Any], files: Files) -> None:
        rows = self.rows(
            conn,
            "SELECT id, user_id, kind::text AS kind, filename, mime, size_bytes, storage_path, "
            "sha256, text_extracted, created_at FROM documents ORDER BY id",
        )
        objs: list[Document] = []
        for row in rows:
            require_choice("documents", row["id"], "kind", row["kind"], DocumentKind)
            objs.append(
                Document(
                    id=row["id"],
                    user_id=row["user_id"],
                    kind=row["kind"],
                    filename=row["filename"],
                    mime=row["mime"],
                    size_bytes=row["size_bytes"],
                    storage_path=row["storage_path"],
                    sha256=row["sha256"],
                    render_hash=None,
                    text_extracted=row["text_extracted"],
                    created_at=row["created_at"],
                )
            )
        written = self.write(Document, "documents", objs)
        for row in rows:
            files.copy(row["id"], row["storage_path"], row["sha256"])
        self.log.count("documents", legacy=len(rows), imported=written)
        self.log.notes["files_copied"] = len(files.created)

    def resumes(self, conn: psycopg.Connection[Any], tailored: list[tuple[Any, Any]]) -> None:
        rows = self.rows(
            conn,
            "SELECT id, user_id, kind::text AS kind, job_match_id, version, data::text AS data, "
            "rendered_document_id, changes::text AS changes, verification::text AS verification, "
            "created_at, updated_at FROM resumes ORDER BY id",
        )
        objs: list[Resume] = []
        for row in rows:
            require_choice("resumes", row["id"], "kind", row["kind"], ResumeKind)
            objs.append(
                Resume(
                    id=row["id"],
                    user_id=row["user_id"],
                    kind=row["kind"],
                    match_id=row["job_match_id"],
                    version=row["version"],
                    data=json_value(row["data"]),
                    source_document=None,
                    rendered_document_id=row["rendered_document_id"],
                    changes=json_value(row["changes"]),
                    verification=json_value(row["verification"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
        written = self.write(Resume, "resumes", objs)
        self.log.count("resumes", legacy=len(rows), imported=written)
        # The second pass: a plain UPDATE, so `updated_at` keeps its legacy value.
        plain = JobMatch.objects.unscoped("the legacy import links tailored resumes")
        for match_id, resume_id in tailored:
            plain.filter(id=match_id).update(tailored_resume_id=resume_id)

    def conversations(self, conn: psycopg.Connection[Any]) -> set[str]:
        rows = self.rows(
            conn,
            "SELECT id, user_id, title, created_at, updated_at FROM conversations ORDER BY id",
        )
        objs = [
            Conversation(
                id=row["id"],
                user_id=row["user_id"],
                title=row["title"],
                turn_started_at=None,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
        written = self.write(Conversation, "conversations", objs)
        self.log.count("conversations", legacy=len(rows), imported=written)
        return {str(row["id"]) for row in rows}

    def messages(self, conn: psycopg.Connection[Any], event_ids: set[int]) -> None:
        rows = self.rows(
            conn,
            "SELECT id, conversation_id, user_id, role, parts::text AS parts, "
            "metadata::text AS metadata, created_at FROM messages ORDER BY created_at, id",
        )
        objs: list[Message] = []
        for seq, row in enumerate(rows, start=1):
            require_choice("messages", row["id"], "role", row["role"], Role)
            metadata = json_value(row["metadata"])
            event = None
            if isinstance(metadata, dict) and metadata.get("kind") == "run_update":
                event = mapping.run_update_event("run_update", metadata.get("eventId"), event_ids)
                if event is None:
                    self.log.report(
                        "messages",
                        row["id"],
                        f"run_update eventId {metadata.get('eventId')!r} names no "
                        "event: run_update_event set to NULL",
                    )
            objs.append(
                Message(
                    seq=seq,
                    conversation_id=row["conversation_id"],
                    user_id=row["user_id"],
                    message_id=row["id"],
                    role=row["role"],
                    parts=json_value(row["parts"]),
                    metadata=metadata,
                    run_update_event_id=event,
                    created_at=row["created_at"],
                    updated_at=row["created_at"],
                )
            )
        written = self.write(Message, "messages", objs)
        self.log.count("messages", legacy=len(rows), imported=written)

    def runs(self, conn: psycopg.Connection[Any], conversation_ids: set[str]) -> None:
        rows = self.rows(
            conn,
            "SELECT id, user_id, kind::text AS kind, mode::text AS mode, status::text AS status, "
            "input::text AS input, stats::text AS stats, output::text AS output, error, "
            "conversation_id, started_at, finished_at, created_at, updated_at "
            "FROM runs ORDER BY id",
        )
        objs: list[Run] = []
        converted = 0
        for row in rows:
            require_choice("runs", row["id"], "kind", row["kind"], RunKind)
            require_choice("runs", row["id"], "mode", row["mode"], Mode)
            require_choice("runs", row["id"], "status", row["status"], RunStatus)
            status, error, finished = row["status"], row["error"], row["finished_at"]
            if status in mapping.ACTIVE_RUN_STATUSES:
                self.log.convert("runs", row["id"], f"{status} -> failed (interrupted)")
                status, error, finished = RunStatus.FAILED, mapping.INTERRUPTED, self.now
                converted += 1
            conversation = row["conversation_id"]
            if conversation is not None and str(conversation) not in conversation_ids:
                self.log.report(
                    "runs",
                    row["id"],
                    f"conversation_id {conversation} names no conversation: set to NULL",
                )
                conversation = None
            objs.append(
                Run(
                    id=row["id"],
                    user_id=row["user_id"],
                    kind=row["kind"],
                    mode=row["mode"],
                    status=status,
                    input=json_value(row["input"]),
                    stats=json_value(row["stats"]),
                    output=json_value(row["output"]),
                    error=error,
                    conversation_id=conversation,
                    dedupe_key=None,
                    started_at=row["started_at"],
                    finished_at=finished,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
        written = self.write(Run, "runs", objs)
        self.log.count("runs", legacy=len(rows), imported=written, converted=converted)

    def approvals(self, conn: psycopg.Connection[Any]) -> None:
        rows = self.rows(
            conn,
            "SELECT id, user_id, run_id, kind::text AS kind, title, payload::text AS payload, "
            "status::text AS status, decision::text AS decision, "
            "decided_via::text AS decided_via, decided_at, continuation->>'queue' AS next_step, "
            "expires_at, created_at FROM approvals ORDER BY id",
        )
        objs: list[Approval] = []
        converted = 0
        for row in rows:
            require_choice("approvals", row["id"], "kind", row["kind"], ApprovalKind)
            require_choice("approvals", row["id"], "status", row["status"], ApprovalStatus)
            require_choice("approvals", row["id"], "decided_via", row["decided_via"], DecidedVia)
            if row["next_step"] is None:
                raise ImportFailure([f"approvals row {row['id']}: continuation has no queue"])
            require_choice("approvals", row["id"], "continuation.queue", row["next_step"], NextStep)
            status, via = row["status"], row["decided_via"]
            if status == ApprovalStatus.PENDING:
                self.log.convert("approvals", row["id"], "pending -> expired (expiry)")
                status, via = ApprovalStatus.EXPIRED, DecidedVia.EXPIRY
                converted += 1
            objs.append(
                Approval(
                    id=row["id"],
                    user_id=row["user_id"],
                    run_id=row["run_id"],
                    kind=row["kind"],
                    title=row["title"],
                    payload=json_value(row["payload"]),
                    status=status,
                    decision=json_value(row["decision"]),
                    decided_via=via,
                    decided_at=row["decided_at"],
                    next_step=row["next_step"],
                    expires_at=row["expires_at"],
                    created_at=row["created_at"],
                )
            )
        written = self.write(Approval, "approvals", objs)
        self.log.count("approvals", legacy=len(rows), imported=written, converted=converted)

    def events(self, conn: psycopg.Connection[Any], approval_ids: set[str]) -> None:
        rows = self.rows(
            conn,
            "SELECT id, user_id, run_id, level::text AS level, kind, message, data::text AS data, "
            "approval_id, created_at FROM agent_events ORDER BY id",
        )
        objs: list[Event] = []
        for row in rows:
            require_choice("agent_events", row["id"], "level", row["level"], Level)
            approval = row["approval_id"]
            if approval is not None and str(approval) not in approval_ids:
                what = f"approval_id {approval} names no approval: set to NULL"
                self.log.report("events", row["id"], what)
                approval = None
            objs.append(
                Event(
                    id=row["id"],
                    user_id=row["user_id"],
                    run_id=row["run_id"],
                    approval_id=approval,
                    level=row["level"],
                    kind=row["kind"],
                    message=row["message"],
                    data=json_value(row["data"]),
                    created_at=row["created_at"],
                )
            )
        written = self.write(Event, "events", objs)
        self.log.count("events", legacy=len(rows), imported=written)

    def llm_calls(self, conn: psycopg.Connection[Any]) -> None:
        rows = self.rows(
            conn,
            "SELECT id, user_id, run_id, purpose::text AS purpose, provider, model, "
            "input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, cost_usd, "
            "latency_ms, error, created_at FROM llm_calls ORDER BY id",
        )
        objs: list[LlmCall] = []
        for row in rows:
            require_choice("llm_calls", row["id"], "purpose", row["purpose"], Purpose)
            objs.append(
                LlmCall(
                    id=row["id"],
                    user_id=row["user_id"],
                    run_id=row["run_id"],
                    conversation=None,
                    purpose=row["purpose"],
                    provider=row["provider"],
                    model=row["model"],
                    input_tokens=row["input_tokens"],
                    output_tokens=row["output_tokens"],
                    cached_input_tokens=row["cached_input_tokens"],
                    reasoning_tokens=row["reasoning_tokens"],
                    cost_usd=row["cost_usd"],
                    latency_ms=row["latency_ms"],
                    error=row["error"],
                    label=None,
                    created_at=row["created_at"],
                )
            )
        written = self.write(LlmCall, "llm_calls", objs)
        self.log.count("llm_calls", legacy=len(rows), imported=written)

    def api_usage(self, conn: psycopg.Connection[Any]) -> None:
        rows = self.rows(
            conn, "SELECT id, provider, period, requests, updated_at FROM api_usage ORDER BY id"
        )
        objs: list[ApiUsage] = []
        for row in rows:
            if row["id"] != f"{row['provider']}:{row['period']}":
                raise ImportFailure(
                    [
                        f"api_usage row {row['id']}: the id does not match "
                        f"{row['provider']}:{row['period']}"
                    ]
                )
            objs.append(
                ApiUsage(
                    provider=row["provider"],
                    period=row["period"],
                    requests=row["requests"],
                    updated_at=row["updated_at"],
                )
            )
        written = self.write(ApiUsage, "api_usage", objs)
        self.log.count("api_usage", legacy=len(rows), imported=written)

    # -- the whole run ----------------------------------------------------------------------

    def write_all(self, conn: psycopg.Connection[Any], files: Files) -> Expectations:
        """Every table, each after the tables its foreign keys name. Django creates foreign
        keys `DEFERRABLE INITIALLY DEFERRED`; they are made immediate first, so a dangling
        reference fails at its own batch, which then names the row."""
        expect = Expectations.read(LegacySide(conn))
        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.users(conn)
        self.companies(conn)
        self.jobs(conn)
        tailored = self.job_matches(conn, expect.resume_ids)
        self.documents(conn, files)
        self.resumes(conn, tailored)
        conversation_ids = self.conversations(conn)
        self.runs(conn, conversation_ids)
        self.approvals(conn)
        self.events(conn, expect.approval_ids)
        self.messages(conn, expect.event_ids)  # after events: run_update_event names one
        self.llm_calls(conn)
        self.api_usage(conn)
        with connection.cursor() as cur:
            for table, column in (("events", "id"), ("messages", "seq")):
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), "
                    f"coalesce(max({column}), 1), max({column}) IS NOT NULL) FROM {table}"
                )
        return expect


def truncate_imported() -> None:
    tables = ", ".join(f'"{name}"' for name in mapping.IMPORTED_TABLES)
    with connection.cursor() as cur:
        cur.execute(f"TRUNCATE {tables} CASCADE")


def latest_record() -> ImportRecord | None:
    return (
        ImportRecord.objects.filter(status=ImportStatus.OK)
        .order_by("-finished_at", "-started_at")
        .first()
    )


def preflight(cfg: Config, opts: Options, *, now: datetime) -> tuple[Marker | None, dict[str, Any]]:
    """Every refusal of §3.2, guard queries first. Returns the backup marker to compare with."""
    guard_target()
    guard_source(cfg)
    notes: dict[str, Any] = {}
    if opts.verify_only:
        record = latest_record()
        if record is None:
            raise ImportRefused("No import has been recorded in this database; nothing to verify.")
        marker_name = (record.checksums or {}).get("marker", {}).get("file")
        marker: Marker | None = None
        if marker_name and (cfg.backup_dir / marker_name).is_file():
            try:
                marker = read_marker(cfg.backup_dir / marker_name)
            except MarkerError as exc:
                raise ImportRefused(str(exc)) from None
        return marker, notes
    try:
        marker = fresh_marker(
            cfg.backup_dir, now=now, max_age=None if opts.rehearsal else MARKER_MAX_AGE
        )
    except MarkerError as exc:
        raise ImportRefused(str(exc)) from None
    if opts.replace and cutover_marked():
        raise ImportRefused(
            "An import has been marked as the cutover; --replace would discard real data since "
            "then. Refusing."
        )
    busy = target_tables_with_rows()
    if busy and not opts.replace:
        raise ImportRefused(
            f"{cfg.target_db} already has data; pass --replace or run reset_app_db "
            f"(rows in {', '.join(busy)})"
        )
    with source.session(cfg.legacy_url) as conn:
        notes["queue_jobs"] = source.active_queue_jobs(conn)
    return marker, notes


def run_import(opts: Options, *, out: Callable[[str], None] = print) -> Outcome:
    started = time.monotonic()
    now = timezone.now()
    cfg = Config.from_settings()
    marker, notes = preflight(cfg, opts, now=now)
    for job in notes.get("queue_jobs", []):
        out(
            f"Note: legacy queue job still {job['state']}: {job['queue']} ×{job['jobs']} "
            "(its run is converted below)"
        )
    if opts.verify_only:
        return verify_only(cfg, marker, out=out, started=started)
    assert marker is not None

    importer = Importer(cfg, out=out)
    out(f"Importing {cfg.legacy_db} into {cfg.target_db} (backup {marker.path.name}).")
    fingerprints_before = source.fingerprints(cfg.legacy_url)
    files_before = source.file_fingerprints(cfg.legacy_storage)
    stats_before = source.stat_counters(cfg.legacy_url)
    files = Files(cfg.legacy_storage, cfg.storage_dir)

    with connection.cursor() as cur:
        cur.execute("SET idle_in_transaction_session_timeout = 0")
    try:
        with source.session(cfg.legacy_url, snapshot=True) as snap, transaction.atomic():
            if opts.replace:
                guard_target()
                truncate_imported()
            expect = importer.write_all(snap, files)
            verification = Verifier(
                LegacySide(snap), TargetSide(), storage_dir=cfg.storage_dir, expectations=expect
            ).run()
            if not verification.ok:
                raise ImportFailure(["The import's checks failed:", verification.report()])
            legacy_counts = source.table_counts(snap)
            untouched = check_untouched(
                fingerprints_before=fingerprints_before,
                fingerprints_after=source.fingerprints(cfg.legacy_url),
                files_before=files_before,
                files_after=source.file_fingerprints(cfg.legacy_storage),
                legacy_counts=legacy_counts,
                marker=marker,
            )
            if not untouched.ok:
                raise ImportFailure(
                    ["The legacy side changed during the import:", untouched.summary()]
                )
            seconds = time.monotonic() - started
            record = ImportRecord.objects.create(
                started_at=importer.now,
                finished_at=timezone.now(),
                status=ImportStatus.OK,
                legacy_db=cfg.legacy_db,
                counts=importer.log.counts,
                checksums={
                    "ids": verification.digests.get("ids", {}),
                    "timestamps": verification.digests.get("timestamps", {}),
                    "files": verification.digests.get("files", {}),
                    "fingerprints": fingerprints_before,
                    "legacy_files": files_before,
                    "marker": marker.summary(),
                },
                report={
                    "rehearsal": opts.rehearsal,
                    "replace": opts.replace,
                    "seconds": round(seconds, 1),
                    "converted": importer.log.converted,
                    "reported": importer.log.reported,
                    "notes": importer.log.notes,
                    "queue_jobs": notes.get("queue_jobs", []),
                    "checks": [
                        *verification.as_json(),
                        {"check": 9, "name": untouched.name, "ok": True, "problems": []},
                    ],
                    "pg_stat_before": stats_before,
                    "pg_stat_after": source.stat_counters(cfg.legacy_url),
                    "storage_dir": str(cfg.storage_dir),
                    "copied": [str(path.relative_to(files.target.root)) for path in files.created],
                },
            )
    except BaseException:
        files.rollback()
        raise
    finally:
        try:
            with connection.cursor() as cur:
                cur.execute("RESET idle_in_transaction_session_timeout")
        except DatabaseError:
            pass  # the session is gone; the setting went with it
    return Outcome(record, verification, untouched, importer.log, time.monotonic() - started, notes)


def verify_only(
    cfg: Config, marker: Marker | None, *, out: Callable[[str], None], started: float
) -> Outcome:
    """§3.4 against an existing import, without writing. Check 9 compares today's legacy
    fingerprints and files with those the import recorded."""
    record = latest_record()
    assert record is not None
    with source.session(cfg.legacy_url, snapshot=True) as snap:
        verification = Verifier(LegacySide(snap), TargetSide(), storage_dir=cfg.storage_dir).run()
        legacy_counts = source.table_counts(snap)
    recorded = record.checksums or {}
    untouched = check_untouched(
        fingerprints_before=recorded.get("fingerprints", {}),
        fingerprints_after=source.fingerprints(cfg.legacy_url),
        files_before=recorded.get("legacy_files", {}),
        files_after=source.file_fingerprints(cfg.legacy_storage),
        legacy_counts=legacy_counts,
        marker=marker,
    )
    return Outcome(None, verification, untouched, Log(), time.monotonic() - started, {})


def counts_table(counts: dict[str, dict[str, int]]) -> str:
    lines = [f"{'table':<16}{'legacy':>10}{'imported':>10}{'converted':>11}"]
    for table in mapping.IMPORTED_TABLES:
        row = counts.get(table)
        if row is None:
            continue
        lines.append(
            f"{table:<16}{row['legacy']:>10,}{row['imported']:>10,}{row.get('converted', 0):>11,}"
        )
    return "\n".join(lines)
