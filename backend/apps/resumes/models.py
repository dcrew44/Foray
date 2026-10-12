"""Uploaded and rendered files, and resume versions (design §2.2, §11).

- A `Document` is a file under `STORAGE_DIR` (`storage_path` is its key, §11.7). The same bytes
  uploaded twice are the same document (`documents_user_sha256`), and a rendered PDF is reused by
  its `render_hash` (§11.6).
- A `Resume` is a version: base resumes per user, tailored ones per match, never overwritten,
  because a tailored resume's `changes` cite base fact ids that must keep resolving. The newest
  base version is the one source of resume facts (R14, `resumes.queries.latest_base_resume`).
  `data` is `resumes.schema.Resume`; `changes` is a list of `TailorChange`; `verification` is
  `Verification`.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now

from apps.accounts.models import User
from apps.core.choices import choices_checks
from apps.core.ids import new_id
from apps.core.models import Timestamped
from apps.core.scoping import UserOwned
from apps.matching.models import JobMatch


class DocumentKind(models.TextChoices):
    RESUME_UPLOAD = "resume_upload"
    LINKEDIN_PDF = "linkedin_pdf"
    RESUME_PDF = "resume_pdf"


class ResumeKind(models.TextChoices):
    BASE = "base"
    TAILORED = "tailored"


class Document(UserOwned):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, sha256) constraint.
    user = models.ForeignKey(User, models.CASCADE, related_name="documents", db_index=False)
    kind = models.TextField(choices=DocumentKind.choices)
    filename = models.TextField()
    mime = models.TextField()
    size_bytes = models.IntegerField()
    storage_path = models.TextField()  # key under STORAGE_DIR: <user_id>/<kind>/<ms>-<name>
    sha256 = models.CharField(max_length=64)
    # resume_pdf only: sha256(html_normal | RENDER_VERSION) for a base resume (§11.6), and
    # sha256(html_normal | RENDER_VERSION | "<Name> - Resume - <Company>.pdf") for a tailored one,
    # so it downloads under its own name even when its HTML equals the base's (step-H1.md §10).
    render_hash = models.CharField(max_length=64, null=True)  # noqa: DJ001
    text_extracted = models.TextField(null=True)  # noqa: DJ001 (the file's text, before any model)
    created_at = models.DateTimeField(db_default=Now())

    user_id: str

    class Meta(UserOwned.Meta):
        db_table = "documents"
        constraints = [
            models.UniqueConstraint(fields=["user", "sha256"], name="documents_user_sha256"),
            models.UniqueConstraint(
                fields=["user", "render_hash"],
                condition=Q(render_hash__isnull=False),
                name="documents_user_render_hash",
            ),
            *choices_checks("documents", kind=DocumentKind),
        ]

    def __str__(self) -> str:
        return f"{self.kind}: {self.filename}"


class Resume(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, kind, version) index.
    user = models.ForeignKey(User, models.CASCADE, related_name="resumes", db_index=False)
    kind = models.TextField(choices=ResumeKind.choices)
    match = models.ForeignKey(JobMatch, models.CASCADE, null=True, related_name="resumes")
    version = models.IntegerField()  # base: per user; tailored: per match
    data = models.JSONField()  # resumes.schema.Resume
    # Upload provenance: the document this version was read from (new).
    source_document = models.ForeignKey(Document, models.SET_NULL, null=True, related_name="+")
    rendered_document = models.ForeignKey(Document, models.SET_NULL, null=True, related_name="+")
    changes = models.JSONField(null=True)  # list[TailorChange]
    verification = models.JSONField(null=True)  # Verification

    user_id: str
    match_id: uuid.UUID | None
    source_document_id: uuid.UUID | None
    rendered_document_id: uuid.UUID | None

    class Meta(UserOwned.Meta):
        db_table = "resumes"
        constraints = [
            models.CheckConstraint(
                condition=Q(kind=ResumeKind.BASE, match__isnull=True)
                | Q(kind=ResumeKind.TAILORED, match__isnull=False),
                name="resumes_kind_match",
            ),
            models.UniqueConstraint(
                fields=["user", "version"],
                condition=Q(kind=ResumeKind.BASE),
                name="resumes_base_version",
            ),
            models.UniqueConstraint(
                fields=["match", "version"],
                condition=Q(kind=ResumeKind.TAILORED),
                name="resumes_tailored_version",
            ),
            *choices_checks("resumes", kind=ResumeKind),
        ]
        indexes = [
            models.Index(F("user"), F("kind"), F("version").desc(), name="resumes_user_kind_ver"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} v{self.version}"
