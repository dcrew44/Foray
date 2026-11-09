"""`POST /api/notifications/test` (#59): send one test notification to the user's saved topic.

`202 {ok, message, jobId}`: the request only queues `notify.test`, because no request waits on
a third party (design invariant 12). The worker sends it and answers with a `notification.test`
event whose data is `{jobId, sent}` and whose message is the sentence to show: that it was
sent, or why not. 400 when no topic is saved; 429 past five tests in ten minutes.
"""

from __future__ import annotations

from typing import Any, cast

from django.http import HttpRequest
from ninja import Status

from apps.accounts.models import User
from apps.core.openapi import register_schema
from apps.core.queue import defer_in_tx
from apps.core.router import ApiModel, router
from apps.notifications import outbox, tasks
from apps.runs.catalogue import NotificationTestData

notifications = router(tags=["notifications"])

ROUTERS = {"/notifications": notifications}

TEST_QUEUED = "Sending a test notification."

# `LiveEvent.data` is an open object; this is what the Settings page reads from the answer.
register_schema(NotificationTestData)


class NotificationTestOut(ApiModel):
    ok: bool
    message: str
    job_id: int


@notifications.post(
    "/test", response={202: NotificationTestOut}, summary="Send a test notification"
)
def post_test(request: HttpRequest) -> Status[dict[str, Any]]:
    user_id = cast(User, request.user).pk
    outbox.topic_for_test(user_id)  # 400 without a topic, 429 past the throttle
    job_id = defer_in_tx(tasks.notify_test, user_id=user_id)
    return Status(202, {"ok": True, "message": TEST_QUEUED, "job_id": job_id})
