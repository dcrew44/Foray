"""Uploaded and rendered files, and resume versions (design §2.2, §11).

- A `Document` is a file under `STORAGE_DIR` (`storage_path` is its key, §11.7). The same bytes
  uploaded twice are the same document (`documents_user_sha256`), and a rendered PDF is reused by
  its `render_hash` (§11.6).
- A `Resume` is a version: base resumes per user, tailored ones per match, never overwritten,
  because a tailored resume's `changes` cite base fact ids that must keep resolving. The newest
  base version is the one source of resume facts (R14, `resumes.queries.latest_base_resume`).
  `data` is `resumes.schema.Resume`; `changes` is a list of `TailorChange`; `verification` is
  `Verification`.
- A `ResumeVariant` is a resume for one kind of role (#61): a name, the title words that choose
  it, and a `spec` (`resumes.variants.VariantSpec`) that selects, orders and rewords the base
  resume's items by their ids. It stores no fact, so it cannot contradict the base and follows
  it when the base is edited. A `Resume` of kind `variant` is one composed snapshot of a
  variant, written when it is rendered, so the exact resume a PDF was made from is kept.
"""

from __future__ import annotations

import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower, Now

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
    VARIANT = "variant"  # a composed snapshot of a `ResumeVariant`


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
    # A variant's is keyed the same way, with the variant's name where the company's is.
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


class ResumeVariant(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, lower(name)) constraint.
    user = models.ForeignKey(User, models.CASCADE, related_name="resume_variants", db_index=False)
    name = models.TextField()  # "Backend", "Data": also the end of its PDF's file name
    # The role-family words that choose it, matched against a posting's title as whole words.
    keywords = ArrayField(models.TextField(), default=list)
    spec = models.JSONField()  # resumes.variants.VariantSpec
    is_default = models.BooleanField(default=False)  # sent when no variant's keywords match

    user_id: str

    class Meta(UserOwned.Meta):
        db_table = "resume_variants"
        constraints = [
            # Expressions, not field names: one "Backend" per user however it is capitalised.
            models.UniqueConstraint(F("user"), Lower("name"), name="resume_variants_user_name"),
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(is_default=True),
                name="resume_variants_one_default",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Resume(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, kind, version) index.
    user = models.ForeignKey(User, models.CASCADE, related_name="resumes", db_index=False)
    kind = models.TextField(choices=ResumeKind.choices)
    match = models.ForeignKey(JobMatch, models.CASCADE, null=True, related_name="resumes")
    # variant only: the variant this row is a snapshot of. Indexed by `resumes_variant_version`.
    variant = models.ForeignKey(
        ResumeVariant, models.CASCADE, null=True, related_name="snapshots", db_index=False
    )
    version = models.IntegerField()  # base: per user; tailored: per match; variant: per variant
    data = models.JSONField()  # resumes.schema.Resume
    # Upload provenance: the document this version was read from (new).
    source_document = models.ForeignKey(Document, models.SET_NULL, null=True, related_name="+")
    rendered_document = models.ForeignKey(Document, models.SET_NULL, null=True, related_name="+")
    changes = models.JSONField(null=True)  # list[TailorChange]
    verification = models.JSONField(null=True)  # Verification

    user_id: str
    match_id: uuid.UUID | None
    variant_id: uuid.UUID | None
    source_document_id: uuid.UUID | None
    rendered_document_id: uuid.UUID | None

    class Meta(UserOwned.Meta):
        db_table = "resumes"
        constraints = [
            models.CheckConstraint(
                condition=Q(kind=ResumeKind.BASE, match__isnull=True, variant__isnull=True)
                | Q(kind=ResumeKind.TAILORED, match__isnull=False, variant__isnull=True)
                | Q(kind=ResumeKind.VARIANT, match__isnull=True, variant__isnull=False),
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
            # "Has a variant" is "is of kind variant" (`resumes_kind_match`). Said this way, the
            # index also answers `variant_id = …`, which the variant's own delete asks; the
            # foreign key has no index of its own.
            models.UniqueConstraint(
                fields=["variant", "version"],
                condition=Q(variant__isnull=False),
                name="resumes_variant_version",
            ),
            *choices_checks("resumes", kind=ResumeKind),
        ]
        indexes = [
            models.Index(F("user"), F("kind"), F("version").desc(), name="resumes_user_kind_ver"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} v{self.version}"
