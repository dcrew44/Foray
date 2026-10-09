"""The legacy import (design §3, §3.6; R16).

Each test imports its own copy of the seeded legacy database (`conftest.legacy`) into pytest's
test database. Covered: every mapping rule of §3.3, the §3.4 checks passing and failing on one
tampered row, the refusals of §3.2, `--replace` checksums, hard failures that roll everything
back, the legacy side left untouched (and a concurrent legacy write failing the import),
verbatim timestamps, swapped URLs refused before anything but the guard queries, and the golden
Better Auth password signing in and upgrading.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from django.contrib.auth import authenticate
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections, transaction
from django.db.backends.postgresql.base import DatabaseWrapper

from apps.accounts.hashers import encode_imported
from apps.accounts.models import User
from apps.chat.models import Conversation, Message
from apps.core.guards import LEGACY_MARKERS_SQL
from apps.jobs.models import ApiUsage, Company, Job
from apps.jobs.sources.dedupe import content_hash
from apps.legacy import importer, mapping, source
from apps.legacy.importer import ImportFailure, ImportRefused, Options, Outcome, run_import
from apps.legacy.models import ImportRecord
from apps.legacy.verify import LegacySide, TargetSide, Verification, Verifier
from apps.llm.models import LlmCall
from apps.matching.models import JobMatch
from apps.resumes.models import Document, Resume
from apps.runs.models import Approval, Event, Run
from tests.factories import make_company
from tests.legacy import seed
from tests.legacy.conftest import LegacyEnv, url_of

pytestmark = pytest.mark.django_db

TARGET_TABLES = (
    User,
    Company,
    Job,
    JobMatch,
    Document,
    Resume,
    Conversation,
    Message,
    Run,
    Approval,
    Event,
    LlmCall,
    ApiUsage,
)


def run(**options: Any) -> Outcome:
    return run_import(Options(**options), out=lambda line: None)


def verify(legacy: LegacyEnv) -> Verification:
    with source.session(legacy.url, snapshot=True) as snap:
        return Verifier(LegacySide(snap), TargetSide(), storage_dir=legacy.target_storage).run()


def failing(verification: Verification) -> set[int]:
    return {result.number for result in verification.results if not result.ok}


def target_one(query: str, params: Any = None) -> Any:
    with connection.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    assert row is not None
    return row[0]


def legacy_one(legacy: LegacyEnv, query: str, params: Any = None) -> Any:
    return legacy.execute(query, params)[0][0]


def target_is_empty() -> bool:
    return not any(model._default_manager.exists() for model in TARGET_TABLES)


def stored_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*")] if root.exists() else []


def reported(outcome: Outcome) -> set[tuple[str, str]]:
    return {(item["table"], item["id"]) for item in outcome.log.reported}


# --- every mapping rule (§3.3) -------------------------------------------------------------------


def test_every_mapping_rule(legacy: LegacyEnv) -> None:
    outcome = run()
    assert outcome.verification.ok, outcome.verification.report()
    assert outcome.untouched.ok, outcome.untouched.summary()

    # users = user + credential account + profiles
    user = User.objects.get(id=seed.USER)
    assert user.email == seed.EMAIL.lower() == "dana.reyes@example.test"
    assert user.name == "Dana Reyes"
    assert user.password == encode_imported(seed.GOLDEN["hash"])
    assert user.password.startswith("ba_scrypt$") and len(user.password) == 171
    assert user.created_at == seed.ts(2) and user.updated_at == seed.ts(3)  # naive UTC -> aware
    assert user.timezone == "UTC" and user.is_active and user.last_login is None
    for column in ("preferences", "interview_state", "settings"):
        assert target_one(f"SELECT {column}::text FROM users WHERE id = %s", [seed.USER]) == (
            legacy_one(
                legacy, f"SELECT {column}::text FROM profiles WHERE user_id = %s", [seed.USER]
            )
        )
    assert user.settings == {"scoreThreshold": 60, "defaultMode": "autopilot"}
    other = User.objects.get(id=seed.OTHER)
    assert not other.has_usable_password()
    assert (other.preferences, other.interview_state, other.settings) == ({}, {}, {})
    assert ("users", seed.OTHER) in reported(outcome)
    assert not Session.objects.exists()  # sessions are not imported: one sign-in at cutover

    # companies: verbatim, added_by, empty listing cache
    retool = Company.objects.get(slug="retool")
    assert (retool.source_type, retool.ats_slug, retool.added_by_id) == (
        "careers_page",
        None,
        seed.USER,
    )
    assert retool.careers_urls == ["https://retool.com/careers"] and retool.needs_browser
    assert all(c.listing_cache == {} for c in Company.objects.all())
    assert Company.objects.get(slug="fresh").last_fetched_at is None
    assert Company.objects.get(slug="acme").tags == ["ai", "infra"]

    # jobs: float8 salaries, recomputed hashes, verbatim text and payload
    job = Job.objects.get(id=seed.SALARY_JOB)
    assert (job.salary_min, job.salary_max) == (124.128, 130.5)
    assert Job.objects.get(id=seed.SMALL_SALARY_JOB).salary_min == 7.982
    assert (
        job.description_raw == "<p>Café — naïve résumé 😀 &amp; more</p>\n<ul><li>Python</li></ul>"
    )
    assert job.raw == {
        "id": 101,
        "title": "Senior Backend Engineer (Payments)",
        "pay": 124.128,
        "nested": {"z": 1, "a": [True, None, 1.5e-7]},
    }
    assert target_one("SELECT raw::text FROM jobs WHERE id = %s", [seed.SALARY_JOB]) == (
        legacy_one(legacy, "SELECT raw::text FROM jobs WHERE id = %s", [seed.SALARY_JOB])
    )
    for row in Job.objects.all():
        assert row.content_hash == content_hash(
            row.title, row.locations, row.description_raw, row.salary_min, row.salary_max
        )
        assert row.dedupe_key == mapping.job_dedupe_key(row.company_name, row.title, row.locations)
    web = Job.objects.get(id=seed.WEB_JOB)
    assert web.source == "web" and web.posted_at is None and web.company_id is not None
    assert Job.objects.get(id="jsearch:some-employer:xyz==").company_id is None
    assert not hasattr(Job, "canonical_job_id")
    hashes = outcome.log.notes["recomputed_hashes"]
    assert hashes["jobs"] == Job.objects.count() == 6 + seed.FILLER_JOBS
    assert hashes["content_hash_unchanged"] == 0  # the seed stored stale hashes

    # job_matches: verbatim; tailored_resume set in a second pass; a dangling reference is NULL
    shortlisted = JobMatch.objects.unscoped("test").get(id=seed.MATCH["shortlisted"])
    assert str(shortlisted.tailored_resume_id) == seed.RESUME["tailored"]
    assert shortlisted.updated_at == seed.ts(14, micro=1)  # the second pass kept updated_at
    assert shortlisted.deep_eval is not None and shortlisted.deep_score == 84
    applied = JobMatch.objects.unscoped("test").get(id=seed.MATCH["applied"])
    assert applied.tailored_resume_id is None
    assert applied.notes == "Called the recruiter.\n\nFollow up."
    assert ("job_matches", seed.MATCH["applied"]) in reported(outcome)
    assert target_one(
        "SELECT fast_eval::text FROM job_matches WHERE id = %s", [seed.MATCH["found"]]
    ) == legacy_one(
        legacy, "SELECT fast_eval::text FROM job_matches WHERE id = %s", [seed.MATCH["found"]]
    )

    # documents: render_hash NULL; files copied byte for byte
    for key, doc_id in seed.DOC.items():
        doc = Document.objects.unscoped("test").get(id=doc_id)
        assert doc.render_hash is None and doc.storage_path == seed.FILES[key].key
        assert (legacy.target_storage / doc.storage_path).read_bytes() == seed.FILES[key].data
    assert not (legacy.target_storage / "backups").exists()  # only documents' files are copied

    # resumes: job_match_id -> match, render_engine dropped, no source document
    tailored = Resume.objects.unscoped("test").get(id=seed.RESUME["tailored"])
    assert str(tailored.match_id) == seed.MATCH["shortlisted"]
    assert str(tailored.rendered_document_id) == seed.DOC["pdf"]
    assert tailored.source_document_id is None and not hasattr(Resume, "render_engine")
    assert (tailored.verification or {})["baseResumeId"] == seed.RESUME["base2"]

    # conversations and messages: id -> message_id, seq in created_at order, notices linked
    messages = list(Message.objects.unscoped("test").order_by("seq"))
    assert [m.message_id for m in messages] == [
        "msgUser0001",
        "msgAsst0002",
        "msgNote0003",
        "msgNote0004",
        "msgNote0005",
    ]
    assert [m.seq for m in messages] == [1, 2, 3, 4, 5]
    by_id = {m.message_id: m for m in messages}
    assert by_id["msgNote0003"].run_update_event_id == seed.NOTICE_EVENT
    assert by_id["msgNote0004"].run_update_event_id is None  # a follow_up, not a run_update
    assert by_id["msgNote0005"].run_update_event_id is None
    assert ("messages", "msgNote0005") in reported(outcome)
    assert all(m.updated_at == m.created_at for m in messages)
    assert by_id["msgAsst0002"].parts[1]["output"] == {"runId": seed.RUN["search"]}
    assert Conversation.objects.unscoped("test").get(id=seed.QUIET_CONVERSATION).title is None

    # runs: JSON byte for byte; active runs converted; conversation FK; no dedupe key
    search = Run.objects.unscoped("test").get(id=seed.RUN["search"])
    assert search.input == seed.SEARCH_INPUT
    for column in ("input", "stats", "output"):
        assert target_one(
            f"SELECT {column}::text FROM runs WHERE id = %s", [seed.RUN["search"]]
        ) == (
            legacy_one(
                legacy, f"SELECT {column}::text FROM runs WHERE id = %s", [seed.RUN["search"]]
            )
        )
    assert str(search.conversation_id) == seed.CONVERSATION
    for key in ("discovery", "refresh", "waiting"):
        converted = Run.objects.unscoped("test").get(id=seed.RUN[key])
        assert converted.status == "failed" and converted.error == mapping.INTERRUPTED
        assert converted.finished_at is not None and converted.finished_at > seed.ts(22)
        assert converted.updated_at == seed.ts(22, micro=9)
    assert Run.objects.unscoped("test").get(id=seed.RUN["waiting"]).conversation_id is None
    assert ("runs", seed.RUN["waiting"]) in reported(outcome)
    assert (
        Run.objects.unscoped("test").get(id=seed.RUN["render"]).error == "Chromium did not start."
    )
    assert not Run.objects.unscoped("test").filter(dedupe_key__isnull=False).exists()
    assert {r.kind for r in Run.objects.unscoped("test").all()} == {
        "search",
        "tailor",
        "resume_render",
        "rank_deep",
        "company_discovery",
        "company_refresh",
        "profile_import",
    }

    # approvals: continuation.queue -> next_step; pending -> expired by expiry
    pending = Approval.objects.unscoped("test").get(id=seed.APPROVAL["pending"])
    assert (pending.status, pending.decided_via, pending.decided_at) == ("expired", "expiry", None)
    assert pending.next_step == "search.evaluate"
    approved = Approval.objects.unscoped("test").get(id=seed.APPROVAL["approved"])
    assert (approved.status, approved.decided_via, approved.next_step) == (
        "approved",
        "ui",
        "search.shortlist",
    )
    assert approved.decision == {"approved": True, "itemIds": [seed.MATCH["found"]]}

    # events: ids verbatim, approval kept only when it exists, legacy kinds kept
    events = {e.id: e for e in Event.objects.unscoped("test").all()}
    assert sorted(events) == seed.EVENT_IDS
    assert str(events[43].approval_id) == seed.APPROVAL["approved"]
    assert events[52].approval_id is None and ("events", "52") in reported(outcome)
    assert events[46].kind == "tailor.done" and events[49].run_id is None

    # llm_calls: verbatim, NULL user kept, exact cost, no conversation or label
    calls = {str(c.id): c for c in LlmCall.objects.unscoped("test").all()}
    assert calls[seed.uid(806)].user_id is None
    assert calls[seed.uid(802)].cost_usd == Decimal("1.234567")
    assert all(c.conversation_id is None and c.label is None for c in calls.values())
    assert sum(c.cost_usd for c in calls.values()) == Decimal("2.471503")

    # api_usage: "jsearch:2026-09" -> (provider, period)
    usage = ApiUsage.objects.get()
    assert (usage.provider, usage.period, usage.requests) == ("jsearch", "2026-09", 25)

    # sequences continue after the imported ids
    new_event = Event.objects.create_for(seed.USER, kind="worker.ping", message="after")
    assert new_event.id == max(seed.EVENT_IDS) + 1
    new_message = Message.objects.create_for(
        seed.USER, conversation_id=seed.CONVERSATION, message_id="after", role="user", parts=[]
    )
    assert new_message.seq == 6

    # the record
    record = outcome.record
    assert record is not None and record.status == "ok" and not record.cutover
    assert record.legacy_db == legacy.name
    assert record.counts["runs"] == {"legacy": 8, "imported": 8, "converted": 3}
    assert record.counts["approvals"] == {"legacy": 5, "imported": 5, "converted": 1}
    assert record.counts["jobs"]["imported"] == 6 + seed.FILLER_JOBS
    assert len(record.report["converted"]) == 4
    assert record.checksums["marker"]["file"].endswith(".ok")
    assert set(record.checksums["files"]) == {f.key for f in seed.FILES.values()}
    assert sorted(record.report["copied"]) == sorted(f.key for f in seed.FILES.values())


# --- verification (§3.4) -------------------------------------------------------------------------

TAMPERING: list[tuple[str, str | None, list[Any], int]] = [
    ("count", "DELETE FROM api_usage", [], 1),
    (
        "ledger",
        "UPDATE llm_calls SET cost_usd = cost_usd + 0.000001 WHERE id = %s",
        [seed.uid(803)],
        3,
    ),
    ("match", "UPDATE job_matches SET notes = 'edited' WHERE id = %s", [seed.MATCH["found"]], 4),
    ("salary", "UPDATE jobs SET salary_min = 124.13 WHERE id = %s", [seed.SALARY_JOB], 5),
    (
        "resume",
        'UPDATE resumes SET data = data || \'{"summary": "x"}\' WHERE id = %s',
        [seed.RESUME["base1"]],
        6,
    ),
    ("file", None, [], 6),
    (
        "preferences",
        'UPDATE users SET preferences = \'{"seniority": "senior"}\' WHERE id = %s',
        [seed.USER],
        7,
    ),
    (
        "run input",
        "UPDATE runs SET input = input || '{\"x\": 1}' WHERE id = %s",
        [seed.RUN["search"]],
        8,
    ),
    (
        "timestamp",
        "UPDATE llm_calls SET created_at = created_at - interval '1 microsecond' WHERE id = %s",
        [seed.uid(800)],
        10,
    ),
    ("event", "UPDATE events SET message = 'edited' WHERE id = %s", [41], 11),
]


def test_verification_passes_and_fails_on_one_tampered_row(legacy: LegacyEnv) -> None:
    run()
    assert verify(legacy).ok
    outcome = run(verify_only=True)
    assert outcome.verification.ok and outcome.untouched.ok
    copied = legacy.target_storage / seed.FILES["upload"].key
    for label, statement, params, check in TAMPERING:
        with transaction.atomic():
            if statement is None:  # a copied file that no longer matches its document
                copied.write_bytes(b"tampered")
            else:
                with connection.cursor() as cur:
                    cur.execute(statement, params)
            result = verify(legacy)
            copied.write_bytes(seed.FILES["upload"].data)
            assert check in failing(result), (label, result.report())
            if label in ("match", "salary", "event", "timestamp", "file"):
                assert failing(result) == {check}, (label, result.report())
            transaction.set_rollback(True)
    assert verify(legacy).ok


def test_verify_only_reports_a_tampered_import(legacy: LegacyEnv) -> None:
    run()
    JobMatch.objects.unscoped("test").filter(id=seed.MATCH["offer"]).update(user_rating=-5)
    outcome = run(verify_only=True)
    assert failing(outcome.verification) == {4}
    with pytest.raises(CommandError, match="Verification failed"):
        call_command("import_legacy", "--verify-only", stdout=StringIO())


# --- refusals (§3.2) -----------------------------------------------------------------------------


def test_refuses_without_a_backup_marker(legacy: LegacyEnv) -> None:
    for path in legacy.backups.iterdir():
        path.unlink()
    with pytest.raises(ImportRefused, match="No backup marker"):
        run()
    assert target_is_empty()


def test_refuses_a_stale_marker_except_in_a_rehearsal(legacy: LegacyEnv) -> None:
    for path in legacy.backups.iterdir():
        path.unlink()
    legacy.write_marker(created_at=datetime.now(UTC) - timedelta(hours=25))
    with pytest.raises(ImportRefused, match="older than 24 hours"):
        run()
    assert target_is_empty()
    outcome = run(rehearsal=True)
    assert outcome.record is not None and outcome.record.report["rehearsal"] is True


def test_refuses_a_marker_whose_dump_does_not_match(legacy: LegacyEnv) -> None:
    dump = next(legacy.backups.glob("*.dump"))
    dump.write_bytes(dump.read_bytes() + b"!")
    with pytest.raises(ImportRefused, match="does not match the digest"):
        run()


def test_refuses_a_non_empty_target(legacy: LegacyEnv) -> None:
    make_company()
    name = connection.settings_dict["NAME"]
    with pytest.raises(
        ImportRefused, match=f"{name} already has data; pass --replace or run reset_app_db"
    ):
        run()
    assert not User.objects.exists() and Company.objects.count() == 1
    with pytest.raises(CommandError, match="already has data"):
        call_command("import_legacy", stdout=StringIO())


def test_replace_gives_identical_checksums(legacy: LegacyEnv) -> None:
    first = run().record
    second = run(replace=True).record
    assert first is not None and second is not None and first.id != second.id
    assert second.checksums == first.checksums
    assert second.counts == first.counts
    assert Job.objects.count() == 6 + seed.FILLER_JOBS
    assert ImportRecord.objects.count() == 2


def test_replace_is_refused_after_cutover(legacy: LegacyEnv) -> None:
    run()
    out = StringIO()
    call_command("mark_cutover", stdout=out)
    assert "as the cutover" in out.getvalue()
    assert ImportRecord.objects.get().cutover
    with pytest.raises(ImportRefused, match="marked as the cutover"):
        run(replace=True)
    with pytest.raises(ImportRefused, match="already has data"):
        run()


def test_refuses_incomplete_configuration(legacy: LegacyEnv, settings: Any) -> None:
    env = settings.ENV
    settings.ENV = env.model_copy(update={"legacy_database_url": None})
    with pytest.raises(ImportRefused, match="LEGACY_DATABASE_URL is not set"):
        run()
    settings.ENV = env.model_copy(update={"legacy_storage_dir": None})
    with pytest.raises(ImportRefused, match="LEGACY_STORAGE_DIR is not set"):
        run()
    settings.ENV = env.model_copy(
        update={"legacy_database_url": url_of(str(connection.settings_dict["NAME"]))}
    )
    with pytest.raises(ImportRefused, match="both name"):
        run()
    settings.ENV = env
    settings.STORAGE_DIR = legacy.storage / "copies"
    with pytest.raises(ImportRefused, match="the legacy tree is never written"):
        run()
    assert target_is_empty()


def test_refuses_unmigrated_target(legacy: LegacyEnv) -> None:
    with connection.cursor() as cur:
        cur.execute("DELETE FROM django_migrations WHERE app = 'legacy'")
    with pytest.raises(ImportRefused, match="was not migrated by Django"):
        run()
    assert target_is_empty()


@contextmanager
def default_connection_on(dbname: str) -> Iterator[DatabaseWrapper]:
    """Point Django's default alias at another database for the block (as `DATABASE_URL`
    would), restoring the test connection afterwards."""
    settings_dict = cast(Any, {**connection.settings_dict, "NAME": dbname, "OPTIONS": {}})
    wrapper = DatabaseWrapper(settings_dict, alias="default")
    original = connections["default"]
    wrapper.ensure_connection()  # connection setup queries are not the importer's
    connections["default"] = wrapper
    try:
        yield wrapper
    finally:
        connections["default"] = original
        wrapper.close()


def test_refuses_swapped_urls(legacy: LegacyEnv, settings: Any, monkeypatch: Any) -> None:
    app_db = str(connection.settings_dict["NAME"])
    settings.ENV = settings.ENV.model_copy(update={"legacy_database_url": url_of(app_db)})
    legacy_before = source.fingerprints(legacy.url)
    opened: list[str] = []
    real_connect = source.connect
    monkeypatch.setattr(
        source, "connect", lambda url, **kw: opened.append(url) or real_connect(url, **kw)
    )
    statements: list[str] = []

    def record(execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any) -> Any:
        statements.append(sql)
        return execute(sql, params, many, context)

    with (
        default_connection_on(legacy.name) as wrapper,
        cast(AbstractContextManager[None], wrapper.execute_wrapper(record)),  # django-types
    ):
        with pytest.raises(ImportRefused, match="looks like the legacy Foray database"):
            run()
        with pytest.raises(ImportRefused, match="looks like the legacy Foray database"):
            run(replace=True)
    assert statements == [LEGACY_MARKERS_SQL, LEGACY_MARKERS_SQL]  # the guard, nothing else
    assert opened == []  # the source (the app database) was never even opened
    assert source.fingerprints(legacy.url) == legacy_before
    assert target_is_empty()


def test_refuses_a_source_without_legacy_tables(legacy: LegacyEnv, settings: Any) -> None:
    # The source is another app-shaped database: the guard refuses it before any write.
    settings.ENV = settings.ENV.model_copy(update={"legacy_database_url": url_of("postgres")})
    with pytest.raises(ImportRefused, match="does not look like the legacy Foray database"):
        run()
    assert target_is_empty()


# --- hard failures roll everything back (§3.3) ---------------------------------------------------


def _mismatch_file(env: LegacyEnv) -> None:
    (env.storage / seed.FILES["pdf"].key).write_bytes(b"%PDF-1.4 something else\n")


def _missing_file(env: LegacyEnv) -> None:
    (env.storage / seed.FILES["pdf"].key).unlink()


HARD_FAILURES: list[tuple[str, Callable[[LegacyEnv], Any], str]] = [
    ("mismatched file", _mismatch_file, "hashes to"),
    ("missing file", _missing_file, "is missing from LEGACY_STORAGE_DIR"),
    (
        "source_type none",
        lambda env: env.execute(
            "INSERT INTO companies (name, slug, source_type, discovered_via) "
            "VALUES ('Nowhere', 'nowhere', 'none', 'seed')"
        ),
        "source_type = 'none'",
    ),
    (
        "malformed password",
        lambda env: env.execute("UPDATE account SET password = 'not-a-hash' WHERE id = 'acc-1'"),
        "not a Better Auth scrypt hash",
    ),
    (
        "dropped approval kind",
        lambda env: env.execute(
            "UPDATE approvals SET kind = 'tailor' WHERE id = %s", [seed.APPROVAL["approved"]]
        ),
        "kind = 'tailor'",
    ),
    (
        "system message",
        lambda env: env.execute("UPDATE messages SET role = 'system' WHERE id = 'msgUser0001'"),
        "role = 'system'",
    ),
    (
        "unknown continuation",
        lambda env: env.execute(
            'UPDATE approvals SET continuation = \'{"queue": "tailor.run"}\' WHERE id = %s',
            [seed.APPROVAL["approved"]],
        ),
        "continuation.queue = 'tailor.run'",
    ),
    (
        "a CHECK the new schema adds",
        lambda env: env.execute("UPDATE companies SET slug = 'Acme Corp' WHERE slug = 'acme'"),
        "companies_slug_shape",
    ),
]


@pytest.mark.parametrize(
    ("label", "corrupt", "message"), HARD_FAILURES, ids=lambda v: v if isinstance(v, str) else ""
)
def test_hard_failures_roll_everything_back(
    legacy: LegacyEnv, label: str, corrupt: Callable[[LegacyEnv], Any], message: str
) -> None:
    corrupt(legacy)
    with pytest.raises(ImportFailure) as failed:
        run()
    assert message in str(failed.value)
    assert "not-a-hash" not in str(failed.value)  # a password value is never printed
    assert not ImportRecord.objects.exists()
    assert target_is_empty()
    assert [p for p in stored_files(legacy.target_storage) if p.is_file()] == []
    assert [p for p in stored_files(legacy.target_storage) if p.is_dir()] == []


# --- R16: the legacy side is never modified ------------------------------------------------------


def test_import_leaves_legacy_untouched(legacy: LegacyEnv, monkeypatch: Any) -> None:
    # A write to a legacy table from another connection between the two fingerprint reads
    # fails the import and rolls it back: no record, no rows, no copied files.
    real_run = Verifier.run

    def run_then_write(self: Verifier) -> Verification:
        result = real_run(self)
        legacy.execute("UPDATE jobs SET title = title WHERE id = %s", [seed.SALARY_JOB])
        return result

    monkeypatch.setattr(Verifier, "run", run_then_write)
    with pytest.raises(ImportFailure, match="The legacy side changed during the import"):
        run()
    assert not ImportRecord.objects.exists()
    assert target_is_empty()
    assert [p for p in stored_files(legacy.target_storage) if p.is_file()] == []
    monkeypatch.undo()

    # A clean import leaves every table fingerprint and every file (mtime and hash) as it was.
    tables_before = source.fingerprints(legacy.url)
    files_before = source.file_fingerprints(legacy.storage)
    outcome = run()
    assert outcome.untouched.ok
    assert source.fingerprints(legacy.url) == tables_before
    assert source.file_fingerprints(legacy.storage) == files_before
    assert outcome.record is not None
    assert outcome.record.checksums["fingerprints"] == tables_before
    # The legacy session itself cannot write.
    with source.session(legacy.url) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("UPDATE jobs SET title = title WHERE id = %s", [seed.SALARY_JOB])


def test_timestamps_verbatim(legacy: LegacyEnv) -> None:
    run()
    assert User.objects.get(id=seed.USER).created_at == seed.ts(2)
    job = Job.objects.get(id=seed.SALARY_JOB)
    assert (job.posted_at, job.first_seen_at, job.last_seen_at, job.fetched_at) == (
        seed.ts(11),
        seed.ts(11, micro=1),
        seed.ts(20, micro=2),
        seed.ts(20, micro=3),
    )
    match = JobMatch.objects.unscoped("test").get(id=seed.MATCH["offer"])
    assert (match.stage_changed_at, match.created_at, match.updated_at) == (
        seed.ts(13, micro=4),
        seed.ts(13),
        seed.ts(14, micro=4),
    )
    run_row = Run.objects.unscoped("test").get(id=seed.RUN["tailor"])
    assert (run_row.started_at, run_row.finished_at, run_row.created_at, run_row.updated_at) == (
        seed.ts(21),
        seed.ts(22),
        seed.ts(21, micro=0),
        seed.ts(22, micro=9),
    )
    call = LlmCall.objects.unscoped("test").get(id=seed.uid(801))
    assert call.created_at == datetime(2025, 9, 29, 23, 59, 59, 999999, tzinfo=UTC)
    assert Event.objects.unscoped("test").get(id=50).created_at == seed.ts(25, micro=9)
    assert Approval.objects.unscoped("test").get(id=seed.APPROVAL["rejected"]).decided_at == (
        seed.ts(24, micro=7)
    )
    assert ApiUsage.objects.get().updated_at == seed.ts(26)

    with transaction.atomic():
        Event.objects.unscoped("test").filter(id=47).update(created_at=seed.ts(25, micro=999))
        assert failing(verify(legacy)) == {10}
        transaction.set_rollback(True)
    with transaction.atomic():
        User.objects.filter(id=seed.USER).update(updated_at=seed.ts(3) + timedelta(hours=1))
        assert failing(verify(legacy)) == {10}
        transaction.set_rollback(True)
    assert verify(legacy).ok


# --- passwords -----------------------------------------------------------------------------------


def test_golden_password_signs_in_and_upgrades(legacy: LegacyEnv) -> None:
    run()
    assert authenticate(email=seed.EMAIL, password="definitely-not-the-password") is None
    assert User.objects.get(id=seed.USER).password.startswith("ba_scrypt$")
    user = authenticate(email=seed.EMAIL, password=seed.GOLDEN["password"])
    assert user is not None and user.pk == seed.USER
    assert User.objects.get(id=seed.USER).password.startswith("pbkdf2_sha256$")
    assert authenticate(email=seed.EMAIL.lower(), password=seed.GOLDEN["password"]) is not None
    # The checks describe the database as imported: after the first sign-in they fail (the
    # password and users.updated_at changed), which is why verify_import runs before it.
    assert failing(verify(legacy)) == {7, 10}


def test_verify_import_password_prompt(legacy: LegacyEnv, monkeypatch: Any) -> None:
    from apps.legacy.management.commands import verify_import

    run()
    monkeypatch.setattr(verify_import, "interactive", lambda: True)
    monkeypatch.setattr(verify_import.getpass, "getpass", lambda prompt: seed.GOLDEN["password"])
    out = StringIO()
    call_command("verify_import", "--password-prompt", stdout=out)
    assert "Verification passed" in out.getvalue()
    assert out.getvalue().rstrip().endswith(verify_import.MATCHES)
    assert User.objects.get(id=seed.USER).password.startswith("ba_scrypt$")  # nothing upgraded

    monkeypatch.setattr(verify_import.getpass, "getpass", lambda prompt: "wrong password")
    out = StringIO()
    with pytest.raises(SystemExit):
        call_command("verify_import", "--password-prompt", stdout=out)
    assert out.getvalue().rstrip().endswith(verify_import.DOES_NOT_MATCH)

    monkeypatch.setattr(verify_import, "interactive", lambda: False)
    with pytest.raises(CommandError, match="Run this in a terminal"):
        call_command("verify_import", "--password-prompt", stdout=StringIO())


def test_import_command_reports_counts_checks_and_conversions(legacy: LegacyEnv) -> None:
    out = StringIO()
    call_command("import_legacy", stdout=out)
    text = out.getvalue()
    assert "table               legacy  imported  converted" in text
    assert f"jobs{6 + seed.FILLER_JOBS:>22,}" in text
    for number in range(1, 12):
        assert f"PASS  {number:>2}." in text
    assert f"Converted runs {seed.RUN['discovery']}: queued -> failed (interrupted)" in text
    assert f"Reported users {seed.OTHER}: no credential account" in text
    assert "Files: 3 in place, 3 copied by this run." in text
    assert json.loads(json.dumps(ImportRecord.objects.get().report))["rehearsal"] is False


def test_json_null_and_naive_timestamps() -> None:
    # The importer keeps SQL NULL and JSON null apart, and reads naive timestamps as UTC.
    assert importer.json_value(None) is None
    assert importer.json_value("null").value is None
    assert importer.json_value('{"a": 1}') == {"a": 1}
    assert importer.aware(datetime(2025, 1, 2, 3, 4, 5)) == datetime(
        2025, 1, 2, 3, 4, 5, tzinfo=UTC
    )
