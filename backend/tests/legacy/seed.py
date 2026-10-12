"""Synthetic legacy rows for the import tests (design §3.6).

Written into a database built from `schema.sql` (the Drizzle SQL), they cover every legacy table
and every enum value the new schema keeps, plus the cases §3.6 names:

- an active run, a pending approval and a run-update message;
- a run whose `input` has camelCase and unknown keys;
- a credential account whose hash is the golden Better Auth hash from step 0;
- a ledger row with NULL `user_id`;
- salaries `124.128` and `7.982`;
- a careers-page company and a `web:` job;
- dangling soft references (a tailored resume, a conversation, an approval, an event);
- old timestamps with microseconds, in naive UTC on Better Auth's tables.

Values the new schema dropped (`source_type='none'`, approval kinds `tailor`/`company_add`, the
`system` role) are left to the hard-failure tests, which insert them into their own copy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

GOLDENS = Path(__file__).resolve().parents[1] / "goldens"
GOLDEN = next(
    g
    for g in json.loads((GOLDENS / "auth" / "better_auth_hashes.json").read_text())
    if g["label"] == "ascii"
)

USER = "LegacyUserAaaaaaaaaaaaaaaaaaaaa1"  # 32 alphanumerics, like Better Auth's ids
OTHER = "LegacyUserBbbbbbbbbbbbbbbbbbbbb2"  # signs in with Google only: no credential account
EMAIL = "Dana.Reyes@Example.test"
FILLER_JOBS = 1_150  # more than one write batch, and more than the 200-job sample


def ts(day: int, hour: int = 12, micro: int = 123456) -> datetime:
    return datetime(2025, 1, day, hour, 4, 5, micro, tzinfo=UTC)


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def J(value: Any) -> str:
    return json.dumps(value)


# Fixed ids the tests refer to.
COMPANY = {
    name: uid(100 + i) for i, name in enumerate(("acme", "leverco", "ashbyco", "retool", "fresh"))
}
MATCH = {
    stage: uid(200 + i)
    for i, stage in enumerate(
        ("found", "shortlisted", "applied", "interviewing", "offer", "rejected", "archived")
    )
}
OTHER_MATCH = uid(210)
DOC = {"upload": uid(300), "pdf": uid(301), "linkedin": uid(302)}
RESUME = {"base1": uid(400), "base2": uid(401), "tailored": uid(402)}
DANGLING = uid(999)
CONVERSATION = uid(500)
QUIET_CONVERSATION = uid(501)
RUN = {
    name: uid(600 + i)
    for i, name in enumerate(
        ("search", "tailor", "render", "rank", "discovery", "refresh", "waiting", "import")
    )
}
APPROVAL = {
    name: uid(700 + i)
    for i, name in enumerate(("pending", "approved", "rejected", "auto", "expired"))
}
EVENT_IDS = list(range(41, 53))
NOTICE_EVENT = 44

SALARY_JOB = "greenhouse:acme:101"
SMALL_SALARY_JOB = "greenhouse:acme:102"
WEB_JOB = "web:retool:" + hashlib.sha1(b"https://retool.com/careers/7").hexdigest()

SEARCH_INPUT = {
    "prompt": "Find backend roles",
    "hints": {"roles": ["Backend Engineer"], "companies": None},
    "candidateJobIds": [SALARY_JOB, SMALL_SALARY_JOB],
    "skipRefresh": True,
    "futureKey": {"nestedValue": [1, 2.5, None, "ünïcode"]},
}


@dataclass(frozen=True)
class SeedFile:
    key: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


FILES = {
    "upload": SeedFile(f"{USER}/resume_upload/1790376662469-Dana_Reyes.pdf", b"%PDF-1.4 upload\n"),
    "pdf": SeedFile(f"{USER}/resume_pdf/1790378193309-Dana_Reyes_-_Acme.pdf", b"%PDF-1.4 tailor\n"),
    "linkedin": SeedFile(f"{USER}/linkedin_pdf/1790370000000-Profile.pdf", b"%PDF-1.4 linkedin\n"),
}


def write_files(root: Path) -> None:
    for item in FILES.values():
        path = root / item.key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.data)
    # Files that belong to the legacy tree but to no document (backups and so on).
    other = root / "backups" / "2026-09-25-persona" / "rows.sql"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("-- not imported\n")


def _insert(conn: psycopg.Connection[Any], table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = list(rows[0])
    query = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
    )
    with conn.cursor() as cur:
        cur.executemany(query, [[row[c] for c in columns] for row in rows])  # pyright: ignore[reportArgumentType]


def seed(conn: psycopg.Connection[Any]) -> None:
    naive = lambda d: ts(d).replace(tzinfo=None)  # noqa: E731 (Better Auth's columns are naive)
    _insert(
        conn,
        '"user"',
        [
            {
                "id": USER,
                "name": "Dana Reyes",
                "email": EMAIL,
                "email_verified": True,
                "image": None,
                "created_at": naive(2),
                "updated_at": naive(3),
            },
            {
                "id": OTHER,
                "name": "Sam Ortiz",
                "email": "sam@example.test",
                "email_verified": False,
                "image": "https://example.test/sam.png",
                "created_at": naive(4),
                "updated_at": naive(4),
            },
        ],
    )
    _insert(
        conn,
        "account",
        [
            {
                "id": "acc-1",
                "account_id": USER,
                "provider_id": "credential",
                "user_id": USER,
                "password": GOLDEN["hash"],
                "created_at": naive(2),
                "updated_at": naive(2),
            },
            {
                "id": "acc-2",
                "account_id": "google-sam",
                "provider_id": "google",
                "user_id": OTHER,
                "password": None,
                "created_at": naive(4),
                "updated_at": naive(4),
            },
        ],
    )
    _insert(
        conn,
        "session",
        [
            {
                "id": f"sess-{i}",
                "expires_at": naive(20),
                "token": f"token-{i}",
                "created_at": naive(5),
                "updated_at": naive(5),
                "ip_address": None,
                "user_agent": "dev-session script",
                "user_id": USER,
            }
            for i in range(2)
        ],
    )
    _insert(
        conn,
        "verification",
        [
            {
                "id": "ver-1",
                "identifier": "reset",
                "value": "x",
                "expires_at": naive(9),
                "created_at": naive(8),
                "updated_at": naive(8),
            },
        ],
    )
    preferences = {
        "target_roles": ["Backend Engineer", "Platform Engineer"],
        "seniority": "mid",
        "locations": ["San Francisco Bay Area"],
        "remote_pref": "hybrid",
        "salary_floor": {"amount": 150000, "currency": "USD", "period": "year"},
        "work_auth": "US citizen",
        "start_date": None,
        "must_haves": None,
        "dealbreakers": ["On-call every week"],
    }
    _insert(
        conn,
        "profiles",
        [
            {
                "id": uid(1),
                "user_id": USER,
                "data": J({"basics": {"name": "Dana Reyes"}}),
                "preferences": J(preferences),
                "interview_state": J({"version": 1, "skipped": ["start_date"]}),
                "assessment": None,
                "created_at": ts(2),
                "updated_at": ts(6),
                "settings": J({"scoreThreshold": 60, "defaultMode": "autopilot"}),
            },
        ],
    )

    company = lambda key, **f: {  # noqa: E731
        "id": COMPANY[key],
        "name": key.title(),
        "domain": None,
        "slug": key,
        "careers_urls": [],
        "tags": [],
        "status": "active",
        "last_fetched_at": ts(10),
        "last_fetch_status": "ok",
        "last_fetch_job_count": 3,
        "needs_browser": False,
        "discovered_via": "seed",
        "added_by_user_id": None,
        "created_at": ts(1),
        "updated_at": ts(10),
        **f,
    }
    _insert(
        conn,
        "companies",
        [
            company("acme", source_type="greenhouse", ats_slug="acme", tags=["ai", "infra"]),
            company(
                "leverco",
                source_type="lever",
                ats_slug="leverco",
                status="paused",
                discovered_via="agent",
                last_fetch_status="error",
            ),
            company(
                "ashbyco",
                source_type="ashby",
                ats_slug="ashbyco",
                status="invalid",
                discovered_via="jsearch",
                last_fetch_status="not-found",
                last_fetch_job_count=0,
            ),
            company(
                "retool",
                source_type="careers_page",
                ats_slug=None,
                domain="retool.com",
                careers_urls=["https://retool.com/careers"],
                discovered_via="user",
                added_by_user_id=USER,
                last_fetch_status="needs-browser",
                needs_browser=True,
            ),
            company(
                "fresh",
                source_type="greenhouse",
                ats_slug="fresh",
                last_fetched_at=None,
                last_fetch_status=None,
                last_fetch_job_count=None,
            ),
        ],
    )

    def job(job_id: str, **f: Any) -> dict[str, Any]:
        source, slug, external = job_id.split(":", 2)
        base = {
            "id": job_id,
            "source": source,
            "external_id": external,
            "company_id": COMPANY.get(slug),
            "company_name": slug.title(),
            "title": "Engineer",
            "locations": ["San Francisco, CA"],
            "location_norm": J(
                {
                    "raw": "San Francisco, CA",
                    "city": "San Francisco",
                    "region": "CA",
                    "country": None,
                    "metro": "bay-area",
                }
            ),
            "remote": "onsite",
            "employment_type": None,
            "salary_min": None,
            "salary_max": None,
            "salary_currency": None,
            "salary_period": None,
            "description_raw": f"<p>{job_id}</p>",
            "description_text": job_id,
            "url": f"https://jobs.example.test/{external}",
            "apply_url": None,
            "posted_at": ts(11),
            "raw": J({"id": external}),
            "content_hash": "stale-hash",
            "dedupe_key": None,
            "canonical_job_id": None,
            "is_active": True,
            "first_seen_at": ts(11, micro=1),
            "last_seen_at": ts(20, micro=2),
            "fetched_at": ts(20, micro=3),
        }
        return {**base, **f}

    jobs = [
        job(
            SALARY_JOB,
            title="Senior Backend Engineer (Payments)",
            remote="remote",
            locations=["Remote - US", "San Francisco, CA"],
            salary_min="124.128",
            salary_max="130.5",
            salary_currency="USD",
            salary_period="hour",
            description_raw="<p>Café — naïve résumé 😀 &amp; more</p>\n<ul><li>Python</li></ul>",
            description_text="Café — naïve résumé 😀 & more\n- Python",
            raw=J(
                {
                    "id": 101,
                    "title": "Senior Backend Engineer (Payments)",
                    "pay": 124.128,
                    "nested": {"z": 1, "a": [True, None, 1.5e-7]},
                }
            ),
        ),
        job(
            SMALL_SALARY_JOB,
            remote="hybrid",
            salary_min="7.982",
            salary_period="hour",
            salary_currency="USD",
            employment_type="Contract",
        ),
        job(
            "lever:leverco:abc-123",
            company_name="leverco",
            remote="onsite",
            salary_min="150000",
            salary_max="180000",
            salary_period="year",
            salary_currency="USD",
            is_active=False,
            apply_url="https://lever.test/apply",
        ),
        job(
            "ashby:ashbyco:f1e2d3",
            company_name="ashbyco",
            remote="unknown",
            salary_min="9000",
            salary_max="9000.50",
            salary_period="month",
            salary_currency="EUR",
            locations=[],
            location_norm=None,
        ),
        job(
            "jsearch:some-employer:xyz==",
            company_name="Some Employer Inc.",
            remote="remote",
            description_raw=None,
            description_text=None,
            raw=J({"job_id": "x"}),
        ),
        job(
            WEB_JOB,
            company_name="Retool",
            title="Product Engineer",
            posted_at=None,
            description_raw="Product Engineer\nBuild internal tools.",
            description_text="Product Engineer\nBuild internal tools.",
            url="https://retool.com/careers/7",
            raw=J(
                {"source": "careers-page", "team": "Eng", "listing": "https://retool.com/careers"}
            ),
        ),
    ]
    jobs += [
        job(
            f"greenhouse:acme:{1000 + i}",
            title=f"Engineer {i}",
            is_active=i % 7 != 0,
            posted_at=ts(12) + timedelta(minutes=i),
            first_seen_at=ts(12) + timedelta(seconds=i),
        )
        for i in range(FILLER_JOBS)
    ]
    _insert(conn, "jobs", jobs)

    def fast(score: int) -> str:
        return J(
            {
                "score": score,
                "fit_reasons": ["Python"],
                "red_flags": [],
                "seniority": "mid",
                "hard_requirement_misses": [],
            }
        )

    deep = J(
        {
            "matched_requirements": [
                {
                    "requirement": "Python",
                    "evidence": "Built it.",
                    "fact_ids": [uid(9001)],
                    "strength": "strong",
                }
            ],
            "gaps": [],
            "seniority_fit": "match",
            "location_fit": "match",
            "salary_fit": "within",
            "risk_flags": [],
            "talking_points": ["Payments"],
            "summary": "Close fit.",
            "score": 84,
            "recommendation": "strong",
        }
    )
    job_for_stage = {
        "found": SALARY_JOB,
        "shortlisted": SMALL_SALARY_JOB,
        "applied": "lever:leverco:abc-123",
        "interviewing": "ashby:ashbyco:f1e2d3",
        "offer": "jsearch:some-employer:xyz==",
        "rejected": WEB_JOB,
        "archived": "greenhouse:acme:1000",
    }
    matches = []
    for i, (stage, match_id) in enumerate(MATCH.items()):
        matches.append(
            {
                "id": match_id,
                "user_id": USER,
                "job_id": job_for_stage[stage],
                "stage": stage,
                "stage_changed_at": ts(13, micro=i),
                "fast_score": 50 + i,
                "fast_eval": fast(50 + i),
                "deep_score": 84 if stage == "shortlisted" else None,
                "deep_eval": deep if stage == "shortlisted" else None,
                "user_rating": {"offer": 5, "rejected": -3}.get(stage),
                "notes": "Called the recruiter.\n\nFollow up." if stage == "applied" else None,
                "tailored_resume_id": {"shortlisted": RESUME["tailored"], "applied": DANGLING}.get(
                    stage
                ),
                "created_at": ts(13),
                "updated_at": ts(14, micro=i),
            }
        )
    matches.append({**matches[0], "id": OTHER_MATCH, "user_id": OTHER, "notes": None})
    _insert(conn, "job_matches", matches)

    _insert(
        conn,
        "documents",
        [
            {
                "id": DOC["upload"],
                "user_id": USER,
                "kind": "resume_upload",
                "filename": "Dana.pdf",
                "mime": "application/pdf",
                "size_bytes": len(FILES["upload"].data),
                "storage_path": FILES["upload"].key,
                "sha256": FILES["upload"].sha256,
                "text_extracted": "Dana Reyes\nBackend engineer",
                "created_at": ts(5),
            },
            {
                "id": DOC["pdf"],
                "user_id": USER,
                "kind": "resume_pdf",
                "filename": "Dana - Acme.pdf",
                "mime": "application/pdf",
                "size_bytes": len(FILES["pdf"].data),
                "storage_path": FILES["pdf"].key,
                "sha256": FILES["pdf"].sha256,
                "text_extracted": None,
                "created_at": ts(15),
            },
            {
                "id": DOC["linkedin"],
                "user_id": USER,
                "kind": "linkedin_pdf",
                "filename": "Profile.pdf",
                "mime": "application/pdf",
                "size_bytes": len(FILES["linkedin"].data),
                "storage_path": FILES["linkedin"].key,
                "sha256": FILES["linkedin"].sha256,
                "text_extracted": "",
                "created_at": ts(6),
            },
        ],
    )
    resume_data = J(
        {
            "basics": {"name": "Dana Reyes", "links": []},
            "work": [],
            "education": [],
            "projects": [],
            "skills": [{"id": uid(9001), "source": "resume", "name": "Python", "category": None}],
        }
    )
    _insert(
        conn,
        "resumes",
        [
            {
                "id": RESUME["base1"],
                "user_id": USER,
                "kind": "base",
                "job_match_id": None,
                "version": 1,
                "data": resume_data,
                "rendered_document_id": None,
                "render_engine": None,
                "changes": None,
                "verification": None,
                "created_at": ts(5),
                "updated_at": ts(5),
            },
            {
                "id": RESUME["base2"],
                "user_id": USER,
                "kind": "base",
                "job_match_id": None,
                "version": 2,
                "data": resume_data,
                "rendered_document_id": None,
                "render_engine": "latex",
                "changes": None,
                "verification": None,
                "created_at": ts(7),
                "updated_at": ts(7),
            },
            {
                "id": RESUME["tailored"],
                "user_id": USER,
                "kind": "tailored",
                "job_match_id": MATCH["shortlisted"],
                "version": 1,
                "data": resume_data,
                "rendered_document_id": DOC["pdf"],
                "render_engine": "html",
                "changes": J(
                    [
                        {
                            "section": "skills",
                            "item_id": None,
                            "before": None,
                            "after": "Go",
                            "reason": "The posting asks for Go.",
                            "evidence_fact_ids": [],
                        }
                    ]
                ),
                "verification": J(
                    {
                        "passed": True,
                        "checkedAt": "2025-01-15T12:04:05.123Z",
                        "unsupported": [],
                        "revised": False,
                        "checked": 1,
                        "modelChecked": 0,
                        "revisions": [],
                        "modelError": None,
                        "baseResumeId": RESUME["base2"],
                        "baseVersion": 2,
                    }
                ),
                "created_at": ts(15),
                "updated_at": ts(16),
            },
        ],
    )

    _insert(
        conn,
        "conversations",
        [
            {
                "id": CONVERSATION,
                "user_id": USER,
                "title": "Find me backend roles",
                "created_at": ts(17),
                "updated_at": ts(18),
            },
            {
                "id": QUIET_CONVERSATION,
                "user_id": USER,
                "title": None,
                "created_at": ts(19),
                "updated_at": ts(19),
            },
        ],
    )
    message = lambda mid, role, parts, meta, at: {  # noqa: E731
        "id": mid,
        "conversation_id": CONVERSATION,
        "user_id": USER,
        "role": role,
        "parts": J(parts),
        "metadata": None if meta is None else J(meta),
        "created_at": at,
    }
    _insert(
        conn,
        "messages",
        [
            message(
                "msgUser0001",
                "user",
                [{"type": "text", "text": "Find me backend roles"}],
                None,
                ts(17, micro=1),
            ),
            message(
                "msgAsst0002",
                "assistant",
                [
                    {"type": "step-start"},
                    {
                        "type": "tool-search_jobs",
                        "toolCallId": "call_1",
                        "state": "output-available",
                        "input": {"query": "backend"},
                        "output": {"runId": RUN["search"]},
                    },
                    {"type": "text", "text": "Started a search.", "state": "done"},
                ],
                None,
                ts(17, micro=2),
            ),
            # Two notices at the same instant: seq follows the id.
            message(
                "msgNote0004",
                "assistant",
                [{"type": "text", "text": "Search finished."}],
                {"kind": "follow_up", "runId": RUN["search"], "eventId": NOTICE_EVENT + 1},
                ts(17, micro=3),
            ),
            message(
                "msgNote0003",
                "assistant",
                [{"type": "text", "text": "Search finished."}],
                {
                    "kind": "run_update",
                    "runId": RUN["search"],
                    "eventId": NOTICE_EVENT,
                    "label": "Search",
                },
                ts(17, micro=3),
            ),
            message(
                "msgNote0005",
                "assistant",
                [{"type": "text", "text": "Lost event."}],
                {
                    "kind": "run_update",
                    "runId": RUN["search"],
                    "eventId": 999999,
                    "label": "Search",
                },
                ts(17, micro=4),
            ),
        ],
    )

    run = lambda key, kind, status, **f: {  # noqa: E731
        "id": RUN[key],
        "user_id": USER,
        "kind": kind,
        "parent_run_id": None,
        "mode": "approve_each",
        "status": status,
        "input": J({"prompt": key}),
        "stats": J({}),
        "output": None,
        "error": None,
        "conversation_id": None,
        "pgboss_job_id": None,
        "started_at": ts(21),
        "finished_at": ts(22) if status in TERMINAL else None,
        "created_at": ts(21, micro=0),
        "updated_at": ts(22, micro=9),
        **f,
    }
    _insert(
        conn,
        "runs",
        [
            run(
                "search",
                "search",
                "completed",
                input=J(SEARCH_INPUT),
                conversation_id=CONVERSATION,
                stats=J(
                    {"phase": "done", "scored": 7, "activeInMirror": 1156, "resumedFrom": None}
                ),
                output=J({"topMatchIds": [MATCH["shortlisted"]], "scored": 7, "shortlisted": 1}),
            ),
            run(
                "tailor",
                "tailor",
                "completed",
                input=J(
                    {
                        "matchId": MATCH["shortlisted"],
                        "prompt": "Tailor your resume for Engineer at Acme",
                    }
                ),
                output=J({"resumeId": RESUME["tailored"], "changes": 1}),
            ),
            run("render", "resume_render", "failed", error="Chromium did not start."),
            run("rank", "rank_deep", "cancelled", input=J({"matchIds": sorted(MATCH.values())})),
            run("discovery", "company_discovery", "queued", started_at=None),
            run("refresh", "company_refresh", "running"),
            run(
                "waiting", "search", "awaiting_approval", mode="autopilot", conversation_id=DANGLING
            ),
            run("import", "profile_import", "completed"),
        ],
    )

    approval = lambda key, kind, status, run_key, **f: {  # noqa: E731
        "id": APPROVAL[key],
        "user_id": USER,
        "run_id": RUN[run_key],
        "kind": kind,
        "title": f"Approve {key}",
        "payload": J({"items": [{"matchId": MATCH["found"]}]}),
        "status": status,
        "decision": None,
        "decided_via": None,
        "decided_at": None,
        "continuation": J(
            {
                "queue": "search.evaluate" if kind == "deep_eval_batch" else "search.shortlist",
                "payload": {"userId": USER, "runId": RUN[run_key], "approvalId": APPROVAL[key]},
            }
        ),
        "expires_at": ts(30),
        "created_at": ts(23),
        **f,
    }
    _insert(
        conn,
        "approvals",
        [
            approval("pending", "deep_eval_batch", "pending", "waiting"),
            approval(
                "approved",
                "shortlist_proposal",
                "approved",
                "search",
                decided_via="ui",
                decided_at=ts(24),
                decision=J({"approved": True, "itemIds": [MATCH["found"]]}),
            ),
            approval(
                "rejected",
                "deep_eval_batch",
                "rejected",
                "search",
                decided_via="chat",
                decided_at=ts(24, micro=7),
                decision=J({"approved": False, "itemIds": []}),
            ),
            approval(
                "auto",
                "deep_eval_batch",
                "auto_approved",
                "rank",
                decided_via="autopilot",
                decided_at=ts(23),
                expires_at=None,
                decision=J({"approved": True}),
            ),
            approval("expired", "shortlist_proposal", "expired", "rank"),
        ],
    )

    levels = ["debug", "info", "warn", "error"]
    kinds = [
        "run.started",
        "scoring.progress",
        "approval.requested",
        "run.completed",
        "chat.followup",
        "tailor.done",
        "render.done",
        "approval.reopened",
        "worker.ping",
        "evaluation.failed",
        "run.failed",
        "approval.decided",
    ]
    _insert(
        conn,
        "agent_events",
        [
            {
                "id": event_id,
                "user_id": USER,
                "run_id": None if kind == "worker.ping" else RUN["search"],
                "level": levels[i % 4],
                "kind": kind,
                "message": f"Event {event_id}: {kind}",
                "data": None if i % 3 == 0 else J({"n": i, "kind": kind}),
                "approval_id": {2: APPROVAL["approved"], 11: DANGLING}.get(i),
                "created_at": ts(25, micro=i),
            }
            for i, (event_id, kind) in enumerate(zip(EVENT_IDS, kinds, strict=True))
        ],
    )
    conn.execute("SELECT setval('agent_events_id_seq', 60)")

    purposes = ["chat", "extract", "score", "evaluate", "tailor", "verify", "merge"]
    _insert(
        conn,
        "llm_calls",
        [
            {
                "id": uid(800 + i),
                "user_id": None if purpose == "merge" else USER,
                "run_id": RUN["search"] if purpose == "score" else None,
                "purpose": purpose,
                "provider": "openai" if i % 2 else "deepseek",
                "model": f"model-{purpose}",
                "input_tokens": 100 * i,
                "output_tokens": 10 * i,
                "cached_input_tokens": i,
                "reasoning_tokens": 0,
                "cost_usd": ["0.000123", "0.001000", "1.234567"][i % 3],
                "latency_ms": None if i == 0 else 250 * i,
                "error": "timeout" if purpose == "verify" else None,
                "created_at": datetime(2025, 8 + i % 2, 28 + i % 3, 23, 59, 59, 999999, tzinfo=UTC),
            }
            for i, purpose in enumerate(purposes)
        ],
    )
    _insert(
        conn,
        "api_usage",
        [
            {
                "id": "jsearch:2026-09",
                "provider": "jsearch",
                "period": "2026-09",
                "requests": 25,
                "updated_at": ts(26),
            },
        ],
    )
    _insert(
        conn,
        "system_state",
        [
            {"key": "worker", "value": J({"pid": 4123, "concurrency": 2}), "updated_at": ts(27)},
        ],
    )


TERMINAL = {"completed", "failed", "cancelled"}
