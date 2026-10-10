"""Approvals and continuations (design §9.6, R6; `map/worker.md` §6).

A pipeline at a decision point raises a gate with `request_approval()` and returns. Deciding
(`decide()`, from the card or from chat), or the approval lapsing (`expire_lapsed()`), moves the
run back to `running` and defers its continuation (`approvals.next_step`), all in one
transaction with the decision and its event. A failed defer therefore rolls the decision back:
there is no "reopened" recovery any more. Autopilot writes the approval already `auto_approved`
and defers the continuation at once; it waits on the run's lock until the current step returns.

Every decision defers the continuation, a "no" included: the continuation is what finishes the
run, and it treats anything but `approved`/`auto_approved` as a no (`is_approved`).

Exactly once: the run row and then the approval row are locked (`select_for_update`, always in
that order, as `lifecycle.cancel` does), so a second concurrent decision sees a decided row.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, cast

from django.db import connection, transaction
from django.db.models import Q
from django.db.models.functions import Now

from apps.runs import events, lifecycle, step
from apps.runs.catalogue import (
    APPROVAL_NEXT_STEP,
    ApprovalKind,
    ApprovalStatus,
    DecidedVia,
    EventKind,
    Level,
    Mode,
    NextStep,
    RunStatus,
    StoredDecision,
)
from apps.runs.models import Approval, Run

__all__ = [
    "APPROVAL_TTL",
    "DecideResult",
    "approved_item_ids",
    "decide",
    "decision_reply",
    "describe_decision",
    "expire_lapsed",
    "is_approved",
    "list_pending",
    "payload_item_ids",
    "request_approval",
    "resolve_decision",
]

log = logging.getLogger("foray.runs")

type Id = str | uuid.UUID

APPROVAL_TTL = timedelta(days=7)


# --- pure helpers (ported from approvals.ts) -----------------------------------------------------


def _has_items(payload: Any) -> bool:
    return isinstance(payload, Mapping) and isinstance(payload.get("items"), list)


def payload_item_ids(payload: Any) -> list[str]:
    """Item ids in payload order: `matchId`, else `id`; strings only, no duplicates."""
    if not _has_items(payload):
        return []
    ids: list[str] = []
    for item in payload["items"]:
        if not isinstance(item, Mapping):
            continue
        value = item.get("matchId")
        if value is None:
            value = item.get("id")
        if isinstance(value, str) and value not in ids:
            ids.append(value)
    return ids


def _decision_parts(decision: Mapping[str, Any]) -> tuple[bool, list[str] | None]:
    item_ids = decision.get("itemIds", decision.get("item_ids"))
    return bool(decision.get("approved")), (list(item_ids) if item_ids is not None else None)


def resolve_decision(payload: Any, decision: Mapping[str, Any]) -> StoredDecision:
    """The stored decision for a payload: payload order kept, unknown ids dropped, and an empty
    subset is a rejection. A payload without items stores `{approved}` alone."""
    approved, wanted = _decision_parts(decision)
    if not _has_items(payload):
        return StoredDecision(approved=approved)
    if not approved:
        return StoredDecision(approved=False, item_ids=[])
    every = payload_item_ids(payload)
    chosen = every if wanted is None else [i for i in every if i in set(wanted)]
    if chosen:
        return StoredDecision(approved=True, item_ids=chosen)
    return StoredDecision(approved=False, item_ids=[])


def is_approved(approval: Approval) -> bool:
    """Whether a decided approval said yes. Rejected, expired and pending are all a no."""
    if approval.status not in (ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED):
        return False
    decision = approval.decision
    return not (isinstance(decision, Mapping) and decision.get("approved") is False)


def approved_item_ids(approval: Approval) -> list[str]:
    """The items a decided approval covers: the approved subset, or every item."""
    decision: Any = approval.decision
    item_ids = decision.get("itemIds") if isinstance(decision, Mapping) else None
    if isinstance(item_ids, list):
        return [i for i in item_ids if isinstance(i, str)]
    return payload_item_ids(approval.payload)


def _roles(n: int) -> str:
    return f"{n} {'role' if n == 1 else 'roles'}"


def describe_decision(approval: Approval, decision: StoredDecision) -> str:
    """What the feed says a decision was, e.g. "Approved deep evaluation of 12 of 15 roles."."""
    total = len(payload_item_ids(approval.payload))
    chosen = len(decision.item_ids) if decision.item_ids is not None else total
    count = _roles(chosen) if chosen == total else f"{chosen} of {_roles(total)}"
    if approval.kind == ApprovalKind.DEEP_EVAL_BATCH:
        return (
            f"Approved deep evaluation of {count}."
            if decision.approved
            else "Skipped the deep evaluation."
        )
    if approval.kind == ApprovalKind.SHORTLIST_PROPOSAL:
        return (
            f"Approved shortlisting {count}."
            if decision.approved
            else "Left the shortlist as it was."
        )
    return f"Approved: {approval.title}." if decision.approved else f"Declined: {approval.title}."


def decision_reply(approval: Approval) -> str:
    """The answer the card shows after a decision (`decideApprovalAction`'s messages)."""
    stored = StoredDecision.read(approval.decision) or StoredDecision(approved=False)
    if not stored.approved:
        if approval.kind == ApprovalKind.DEEP_EVAL_BATCH:
            return "Skipped the deep evaluation."
        if approval.kind == ApprovalKind.SHORTLIST_PROPOSAL:
            return "Left the shortlist as it was."
        return "Declined."
    total = len(payload_item_ids(approval.payload))
    chosen = len(stored.item_ids) if stored.item_ids is not None else total
    if approval.kind == ApprovalKind.DEEP_EVAL_BATCH:
        return f"Approved. Deep-evaluating {_roles(chosen)} in the background."
    if approval.kind == ApprovalKind.SHORTLIST_PROPOSAL:
        return f"Approved. Shortlisting {_roles(chosen)}."
    return "Approved."


def lapsed_message(approval: Approval) -> str:
    return f"No answer within a week, so this lapsed: {approval.title}."


# --- raising a gate ------------------------------------------------------------------------------


# The phase each continuation starts in. A yes writes it with the decision, in the same
# transaction as `awaiting_approval -> running`, so a card never shows the gate's stale
# `awaiting_*` phase on a running run while the continuation waits for the worker (it writes the
# same phase again through `resume_from`). A no or a lapse finishes the run without that work,
# so it keeps the gate's phase until the continuation completes it.
CONTINUATION_PHASE: dict[str, str] = {
    NextStep.SEARCH_EVALUATE: "evaluating",
    NextStep.SEARCH_SHORTLIST: "shortlisting",
}


def _resume(run_id: Id, phase: str | None = None) -> bool:
    """`awaiting_approval -> running`, conditionally, with the continuation's phase if given."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE runs SET status = 'running', stats = stats || %s::jsonb, updated_at = now() "
            "WHERE id = %s AND status = 'awaiting_approval' RETURNING id",
            [lifecycle.jsonb({"phase": phase} if phase else {}), str(run_id)],
        )
        return cursor.fetchone() is not None


def _defer_continuation(approval: Approval) -> int:
    return step.defer_step(
        approval.next_step,
        approval.run_id,
        queueing_lock=f"approval:{approval.id}",
        approval_id=str(approval.id),
    )


def request_approval(
    run_id: Id,
    kind: str,
    title: str,
    payload: Any,
    next_step: str | None = None,
    *,
    stats: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    messages: Mapping[str, str] | None = None,
) -> Approval:
    """Raise a gate on a running run (one transaction).

    - **approve_each:** `running -> awaiting_approval` with `stats` merged, a `pending` approval
      that expires in 7 days, and `approval.requested` ("Waiting for your go-ahead: <title>." or
      `messages["requested"]`). The caller returns afterwards; the decision resumes the run.
    - **autopilot:** an `auto_approved` approval for every item, `stats` merged, the
      continuation deferred, and `approval.auto` ("Autopilot approved: <title>." or
      `messages["auto"]`). The run stays `running`.

    A run that is no longer running (cancelled meanwhile) gets the approval already `expired`,
    with no event and no continuation.
    """
    value = lifecycle.dump(payload)
    next_step = next_step or APPROVAL_NEXT_STEP[kind]
    messages = messages or {}
    event_data = {"kind": kind, **{key: lifecycle.dump(v) for key, v in (data or {}).items()}}
    with transaction.atomic():
        run = (
            Run.objects.unscoped("request_approval: the pipeline's own run")
            .select_for_update()
            .get(id=run_id)
        )
        fields: dict[str, Any] = {"run": run, "kind": kind, "title": title, "payload": value}
        if run.status != RunStatus.RUNNING:
            return Approval.objects.create_for(
                run.user_id, **fields, status=ApprovalStatus.EXPIRED, next_step=next_step
            )
        if run.mode == Mode.AUTOPILOT:
            decision = StoredDecision(
                approved=True, item_ids=payload_item_ids(value) if _has_items(value) else None
            )
            approval = Approval.objects.create_for(
                run.user_id,
                **fields,
                status=ApprovalStatus.AUTO_APPROVED,
                decision=decision.dump(),
                decided_via=DecidedVia.AUTOPILOT,
                decided_at=Now(),
                next_step=next_step,
            )
            if stats:
                lifecycle.merge_stats(run.id, stats)
            _defer_continuation(approval)
            events.emit(
                run.user_id,
                EventKind.APPROVAL_AUTO,
                messages.get("auto") or f"Autopilot approved: {title}.",
                run_id=run.id,
                approval_id=approval.id,
                data=event_data,
            )
            return approval
        approval = Approval.objects.create_for(
            run.user_id,
            **fields,
            status=ApprovalStatus.PENDING,
            expires_at=Now() + APPROVAL_TTL,
            next_step=next_step,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE runs SET status = 'awaiting_approval', stats = stats || %s::jsonb, "
                "updated_at = now() WHERE id = %s AND status = 'running'",
                [lifecycle.jsonb(dict(stats or {})), str(run.id)],
            )
        events.emit(
            run.user_id,
            EventKind.APPROVAL_REQUESTED,
            messages.get("requested") or f"Waiting for your go-ahead: {title}.",
            run_id=run.id,
            approval_id=approval.id,
            data=event_data,
        )
        return approval


# --- deciding ------------------------------------------------------------------------------------

type Outcome = Literal["decided", "not_found", "already_decided", "expired"]


@dataclass(frozen=True, slots=True)
class DecideResult:
    outcome: Outcome
    approval: Approval | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "decided"


def _expire(approval: Approval, run: Run) -> None:
    """Lapse one pending approval (rows locked by the caller). A paused run resumes and its
    continuation finishes it with a "no"; a run that already ended gets no event, so its terminal
    event stays its last."""
    approval.status = ApprovalStatus.EXPIRED
    approval.decided_via = DecidedVia.EXPIRY
    approval.decided_at = Now()  # evaluated by the database, read back with RETURNING
    approval.save(update_fields=["status", "decided_via", "decided_at"])
    if not _resume(run.id):
        return
    _defer_continuation(approval)
    events.emit(
        run.user_id,
        EventKind.APPROVAL_EXPIRED,
        lapsed_message(approval),
        run_id=run.id,
        approval_id=approval.id,
        level=Level.WARN,
    )


def decide(user_id: str, approval_id: Id, decision: Mapping[str, Any], via: str) -> DecideResult:
    """Answer the user's pending approval, exactly once (R6). `decision` is
    `{approved, itemIds?}`; `via` is `ui` or `chat`. One transaction: the decision, the run's move
    to `running`, the continuation job and `approval.decided` commit together or not at all (a
    failed defer raises and leaves the approval pending)."""
    if via not in DecidedVia.values:
        raise ValueError(f"{via!r} is not a way to decide")
    approval_uuid = lifecycle.as_uuid(approval_id)
    if approval_uuid is None:
        return DecideResult("not_found")
    run_id = (
        Approval.objects.for_user(user_id)
        .filter(id=approval_uuid)
        .values_list("run_id", flat=True)
        .first()
    )
    if run_id is None:
        return DecideResult("not_found")
    with transaction.atomic():
        run = Run.objects.for_user(user_id).select_for_update().filter(id=run_id).first()
        approval = (
            Approval.objects.for_user(user_id).select_for_update().filter(id=approval_uuid).first()
        )
        if run is None or approval is None:
            return DecideResult("not_found")
        if approval.status == ApprovalStatus.EXPIRED:
            return DecideResult("expired", approval)
        if approval.status != ApprovalStatus.PENDING:
            return DecideResult("already_decided", approval)
        if _lapsed(approval):
            _expire(approval, run)  # the expiry path, inline
            return DecideResult("expired", approval)
        if run.status != RunStatus.AWAITING_APPROVAL:
            # Cancelled or finished: it cannot be resumed, whatever the row still says.
            approval.status = ApprovalStatus.EXPIRED
            approval.save(update_fields=["status"])
            return DecideResult("expired", approval)

        stored = resolve_decision(approval.payload, decision)
        approval.status = ApprovalStatus.APPROVED if stored.approved else ApprovalStatus.REJECTED
        approval.decision = cast(Any, stored.dump())  # django-types types JSONField narrowly
        approval.decided_via = via
        approval.decided_at = Now()  # evaluated by the database, read back with RETURNING
        approval.save(update_fields=["status", "decision", "decided_via", "decided_at"])
        _resume(run.id, CONTINUATION_PHASE.get(approval.next_step) if stored.approved else None)
        _defer_continuation(approval)
        events.emit(
            user_id,
            EventKind.APPROVAL_DECIDED,
            describe_decision(approval, stored),
            run_id=run.id,
            approval_id=approval.id,
            data={
                "kind": approval.kind,
                "via": via,
                "approved": stored.approved,
                "itemIds": stored.item_ids,
            },
        )
    return DecideResult("decided", approval)


# --- expiry and reading --------------------------------------------------------------------------

_LOCK_LAPSED = """
SELECT a.id, r.id FROM approvals a JOIN runs r ON r.id = a.run_id
 WHERE a.id = %s AND a.status = 'pending' AND a.expires_at <= statement_timestamp()
 FOR UPDATE OF a, r SKIP LOCKED
"""


def _lapsed(approval: Approval) -> bool:
    """Whether a pending approval is past its expiry, by the database's clock, which wrote it."""
    return (
        approval.expires_at is not None
        and Approval.objects.for_user(approval.user_id)
        .filter(id=approval.id, expires_at__lte=Now())
        .exists()
    )


def expire_lapsed(limit: int = 500) -> int:
    """The periodic `approvals.expire` (§9.3): every pending approval past its 7 days lapses in
    its own transaction (`FOR UPDATE … SKIP LOCKED`, so a row someone is deciding right now is
    left for the next minute). An expiry is a "no" that lets the run finish. Returns how many.

    `expires_at` is written by the database (`Now() + 7 days`), so it is compared with the
    database's clock, like every stored timestamp (core/clock.py)."""
    candidates = list(
        Approval.objects.unscoped("approvals.expire: every user's lapsed approvals")
        .filter(status=ApprovalStatus.PENDING, expires_at__lte=Now())
        .order_by("expires_at")
        .values_list("id", flat=True)[:limit]
    )
    expired = 0
    for approval_id in candidates:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(_LOCK_LAPSED, [str(approval_id)])
                row = cursor.fetchone()
            if row is None:
                continue
            approval = Approval.objects.unscoped("approvals.expire: locked").get(id=row[0])
            run = Run.objects.unscoped("approvals.expire: locked").get(id=row[1])
            _expire(approval, run)
            expired += 1
    if expired:
        log.info("expired %s lapsed approval%s", expired, "" if expired == 1 else "s")
    return expired


def list_pending(user_id: str) -> list[Approval]:
    """The user's answerable approvals, newest first: pending, unexpired, on a paused run."""
    return list(
        Approval.objects.for_user(user_id)
        .filter(status=ApprovalStatus.PENDING, run__status=RunStatus.AWAITING_APPROVAL)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=Now()))
        .order_by("-created_at")
    )
