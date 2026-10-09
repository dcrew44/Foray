"""The legacy → new table mapping shared by the importer and the checks (design §3.3).

`TABLES` lists the imported tables in write order, each with how its identity and its timestamp
columns read on either side, so the counts (check 1), the id-set checksums (check 2) and the
timestamp checksums (check 10) are computed the same way everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, LiteralString

from apps.jobs.sources.dedupe import dedupe_key
from apps.jobs.sources.normalize import primary_location

INTERRUPTED = "Interrupted by the move to the new backend. Start it again."
ACTIVE_RUN_STATUSES = ("queued", "running", "awaiting_approval")


@dataclass(frozen=True)
class TableSpec:
    name: str  # the new table, and the name the report uses
    legacy_table: LiteralString  # as written in SQL
    target_table: LiteralString
    legacy_id: LiteralString  # SQL for the row's identity on each side
    target_id: LiteralString
    timestamps: tuple[LiteralString, ...]  # the legacy timestamp columns, same names on both sides
    converted_ts_index: int = -1  # runs: the column a conversion sets (finished_at)

    def _select(self, table: LiteralString, ident: LiteralString) -> LiteralString:
        columns = "".join(f", {col} AS t{i}" for i, col in enumerate(self.timestamps))
        return f"SELECT {ident} AS id{columns} FROM {table}"  # pyright: ignore[reportReturnType]

    @property
    def legacy_ts_sql(self) -> LiteralString:
        return self._select(self.legacy_table, self.legacy_id)

    @property
    def target_ts_sql(self) -> LiteralString:
        return self._select(self.target_table, self.target_id)

    @property
    def legacy_ids_sql(self) -> LiteralString:
        return "SELECT " + self.legacy_id + " AS id FROM " + self.legacy_table

    @property
    def target_ids_sql(self) -> LiteralString:
        return "SELECT " + self.target_id + " AS id FROM " + self.target_table

    @property
    def legacy_count_sql(self) -> LiteralString:
        return "SELECT count(*) AS n FROM " + self.legacy_table

    @property
    def target_count_sql(self) -> LiteralString:
        return "SELECT count(*) AS n FROM " + self.target_table


TABLES: tuple[TableSpec, ...] = (
    TableSpec("users", '"user"', "users", "id", "id", ("created_at", "updated_at")),
    TableSpec(
        "companies",
        "companies",
        "companies",
        "id::text",
        "id::text",
        ("last_fetched_at", "created_at", "updated_at"),
    ),
    TableSpec(
        "jobs",
        "jobs",
        "jobs",
        "id",
        "id",
        ("posted_at", "first_seen_at", "last_seen_at", "fetched_at"),
    ),
    TableSpec(
        "job_matches",
        "job_matches",
        "job_matches",
        "id::text",
        "id::text",
        ("stage_changed_at", "created_at", "updated_at"),
    ),
    TableSpec("documents", "documents", "documents", "id::text", "id::text", ("created_at",)),
    TableSpec(
        "resumes", "resumes", "resumes", "id::text", "id::text", ("created_at", "updated_at")
    ),
    TableSpec(
        "conversations",
        "conversations",
        "conversations",
        "id::text",
        "id::text",
        ("created_at", "updated_at"),
    ),
    TableSpec(
        "messages",
        "messages",
        "messages",
        "conversation_id::text || '/' || id",
        "conversation_id::text || '/' || message_id",
        ("created_at",),
    ),
    TableSpec(
        "runs",
        "runs",
        "runs",
        "id::text",
        "id::text",
        ("started_at", "finished_at", "created_at", "updated_at"),
        converted_ts_index=1,
    ),
    TableSpec(
        "approvals",
        "approvals",
        "approvals",
        "id::text",
        "id::text",
        ("decided_at", "expires_at", "created_at"),
    ),
    TableSpec("events", "agent_events", "events", "id::text", "id::text", ("created_at",)),
    TableSpec("llm_calls", "llm_calls", "llm_calls", "id::text", "id::text", ("created_at",)),
    TableSpec(
        "api_usage", "api_usage", "api_usage", "id", "provider || ':' || period", ("updated_at",)
    ),
)

IMPORTED_TABLES: tuple[str, ...] = tuple(spec.name for spec in TABLES)


def job_dedupe_key(company_name: str, title: str, locations: list[str]) -> str:
    """`dedupe_key` exactly as the adapters compute it: company name, title and the primary
    location's city (or its raw text)."""
    location = primary_location(list(locations))
    return dedupe_key(company_name, title, (location.city or location.raw) if location else None)


def run_update_event(meta_kind: Any, event_id: Any, event_ids: set[int]) -> int | None:
    """The event a legacy run-update notice points at, when that event was imported."""
    if meta_kind != "run_update" or event_id is None or isinstance(event_id, bool):
        return None
    try:
        number = int(str(event_id))
    except ValueError:
        return None
    return number if number in event_ids else None
