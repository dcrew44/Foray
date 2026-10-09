"""The commit-safe live cursor (design §9.5, R5; `map/_gaps.md` §3.5).

An event id is allocated at INSERT and visible at COMMIT. Without the per-user advisory lock in
`emit()`, a poll between two out-of-order commits would move the cursor past the earlier id
forever. With it, ids become visible in id order for each user, so `id > since` never skips.
"""

from __future__ import annotations

import random
import threading
import time

import pytest
from django.db import connections, transaction

from apps.runs import events, live
from apps.runs.models import Event
from tests.factories import make_user

pytestmark = pytest.mark.django_db(transaction=True)


def _poll(user_id: str, cursor: int) -> tuple[int, list[int]]:
    snapshot = live.snapshot(user_id, cursor)
    return snapshot.cursor, [event.id for event in snapshot.events]


def _deterministic(user_id: str, cursor: int) -> None:
    t1_emitted, t1_commit = threading.Event(), threading.Event()
    t2_started, t2_emitted, t2_commit = threading.Event(), threading.Event(), threading.Event()
    ids: dict[str, int] = {}

    def t1() -> None:
        try:
            with transaction.atomic():
                ids["t1"] = events.emit(user_id, "scoring.start", "one").id
                t1_emitted.set()
                assert t1_commit.wait(10)
        finally:
            connections.close_all()

    def t2() -> None:
        try:
            with transaction.atomic():
                t2_started.set()
                ids["t2"] = events.emit(user_id, "scoring.done", "two").id  # waits for T1
                t2_emitted.set()
                assert t2_commit.wait(10)
        finally:
            connections.close_all()

    first = threading.Thread(target=t1)
    first.start()
    assert t1_emitted.wait(10)
    second = threading.Thread(target=t2)
    second.start()
    assert t2_started.wait(10)
    time.sleep(0.3)
    assert not t2_emitted.is_set(), "T2 must wait for T1's lock before it allocates an id"

    assert _poll(user_id, cursor) == (cursor, [])  # neither has committed

    t1_commit.set()
    first.join(10)
    assert t2_emitted.wait(10)
    cursor, seen = _poll(user_id, cursor)
    assert seen == [ids["t1"]]  # T2 holds a later id and is not visible yet

    t2_commit.set()
    second.join(10)
    cursor, seen_after = _poll(user_id, cursor)
    assert seen_after == [ids["t2"]]
    assert ids["t1"] < ids["t2"]


def _stress(user_id: str, cursor: int) -> None:
    threads, per_thread = 4, 50
    done = threading.Event()
    emitted: list[int] = []
    lock = threading.Lock()

    def writer(seed: int) -> None:
        rng = random.Random(seed)
        try:
            for n in range(per_thread):
                with transaction.atomic():
                    event = events.emit(user_id, "scoring.progress", f"{seed}:{n}")
                    time.sleep(rng.uniform(0, 0.004))  # commit a little later, at random
                with lock:
                    emitted.append(event.id)
        finally:
            connections.close_all()

    seen: list[int] = []

    def poller() -> None:
        nonlocal cursor
        try:
            while not done.is_set():
                cursor, ids = _poll(user_id, cursor)
                seen.extend(ids)
            cursor, ids = _poll(user_id, cursor)
            seen.extend(ids)
        finally:
            connections.close_all()

    poll_thread = threading.Thread(target=poller)
    poll_thread.start()
    writers = [threading.Thread(target=writer, args=(seed,)) for seed in range(threads)]
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join(60)
    done.set()
    poll_thread.join(60)

    assert len(emitted) == threads * per_thread
    assert sorted(seen) == sorted(emitted), "every id is seen"
    assert len(seen) == len(set(seen)), "exactly once"
    assert seen == sorted(seen), "in id order"


@pytest.mark.parametrize("case", ["deterministic", "stress"])
def test_cursor_never_skips_out_of_order_commits(case: str) -> None:
    user = make_user()
    seed = events.emit(user.pk, "run.started", "seed")  # later polls page with `id > since`
    {"deterministic": _deterministic, "stress": _stress}[case](user.pk, seed.id)


def test_the_first_poll_is_the_latest_slice_and_later_polls_page_forward() -> None:
    user = make_user()
    other = make_user()
    for n in range(70):
        events.emit(user.pk, "scoring.progress", f"{n}")
    events.emit(other.pk, "scoring.progress", "not yours")
    cursor, first = _poll(user.pk, 0)
    all_ids = list(Event.objects.for_user(user.pk).order_by("id").values_list("id", flat=True))
    assert first == all_ids[-60:] and cursor == all_ids[-1]
    assert _poll(user.pk, all_ids[9]) == (all_ids[-1], all_ids[10:])
    assert _poll(user.pk, cursor) == (cursor, [])
