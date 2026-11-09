"""Resume variants: one resume per kind of role, chosen by a posting's title (#61; DESIGN.md,
Tailoring).

**A variant never stores a fact.** Employers, titles, dates, schools, the name and the contact
details live only in the newest base resume (R14). A variant is a `VariantSpec`: which of the
base's items it shows, in what order, and how its headline, summary and bullets are worded.
`compose(base, spec)` builds the resume it renders, so a variant cannot contradict the base and
follows it when the base is edited. An id the base no longer has is dropped; an item the spec
does not name is left out, because a variant is an explicit selection (a new one starts as
`full_spec`: everything, in the base's order).

**Choosing is deterministic.** `choose_variant(variants, title)` counts, for each variant, the
keywords the posting's title says as whole words (`backend` is not in "Backendless", and
`data engineer` must occur as that phrase; spaces and hyphens inside a word do not count, so
`backend` is in "Back-End Engineer"). The most keywords wins; a tie goes to the default variant
when it is among them, else to the oldest; a title that says none of them gets the default
variant; without a default the answer is None, the base resume as it is. No model is asked.
`resume_for(user_id, title, variant_id)` is what an application calls: the choice, the composed
resume and one sentence saying why.

**Snapshots.** `snapshot(user_id, variant_id)` writes the composed resume as a `resumes` row of
kind `variant` (unless the variant's newest snapshot already says the same), which is what
`pipeline.render_variant` renders: the exact resume a PDF was made from is kept, as it is for
base and tailored versions.

Sync, like every ORM function. A refusal the user should read is a `VariantRefused`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from django.db import IntegrityError, transaction
from django.db.models.functions import Now
from pydantic import Field, StringConstraints

from apps.accounts.models import User
from apps.core.choices import CAMEL, JsonModel
from apps.resumes import schema
from apps.resumes.edits import canonical_json
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.models import ResumeKind, ResumeVariant
from apps.resumes.queries import latest_base_resume, latest_base_resume_row
from apps.resumes.render import pdf_filename
from apps.resumes.text import js_trim

__all__ = [
    "FULL_RESUME",
    "MAX_KEYWORDS",
    "MAX_NAME",
    "MAX_VARIANTS",
    "Chosen",
    "VariantItem",
    "VariantRefused",
    "VariantSpec",
    "VariantView",
    "choose_variant",
    "clean_keywords",
    "compose",
    "create_variant",
    "delete_variant",
    "full_spec",
    "list_variants",
    "matched_keywords",
    "resume_for",
    "resume_row",
    "selects_nothing",
    "snapshot",
    "update_variant",
    "variant_views",
]

MAX_VARIANTS = 6
MAX_NAME = 60
MAX_KEYWORDS = 20
MAX_KEYWORD = 40
# Bounds on what one variant may store. The base resume has none of its own, so these are far
# above any real resume: they stop a runaway request, not a long bullet.
MAX_ITEMS = 300
MAX_HIGHLIGHTS = 100
MAX_HIGHLIGHT = 5000
MAX_HEADLINE = 300
MAX_SUMMARY = 5000

# `resume_for(..., variant_id=FULL_RESUME)`: the user chose the base resume over any variant.
FULL_RESUME = "base"

NO_RESUME = "Upload a resume before making a variant."
VARIANT_GONE = "That variant no longer exists."
TOO_MANY = f"You can keep up to {MAX_VARIANTS} variants. Delete one to add another."
NAME_NEEDED = "Give the variant a name."
SELECTS_NOTHING = "A variant needs at least one role, project, school or skill from your resume."
NOTHING_TO_RENDER = (
    "This variant selects nothing from your current resume. Choose what it shows, then make "
    "the PDF."
)

CHOSE_VARIANT = "You chose this variant."
CHOSE_FULL = "You chose your full resume."
DEFAULT_VARIANT = "Your default variant."
NO_MATCH = "Your full resume: no variant matched."
NO_VARIANTS = "Your full resume: you have no variants yet."

_NAME_CONSTRAINT = "resume_variants_user_name"

type Id = str | uuid.UUID
Refusal = Literal["not_found", "no_resume", "duplicate", "limit", "empty", "invalid"]


class VariantRefused(Exception):
    """A variant that cannot be made, changed or rendered. The message is shown to the user."""

    def __init__(self, refusal: Refusal, message: str) -> None:
        super().__init__(message)
        self.refusal: Refusal = refusal
        self.message = message


# --- The spec ------------------------------------------------------------------------------------

_Bullet = Annotated[str, StringConstraints(max_length=MAX_HIGHLIGHT)]


# The models carry no docstrings: pydantic would publish them as the components' descriptions.
class VariantItem(JsonModel):
    model_config = CAMEL

    id: str  # a base work or project item's id
    # The variant's own wording of the item's bullets; None means the base's bullets, whatever
    # they are when the variant is composed.
    highlights: Annotated[list[_Bullet], Field(max_length=MAX_HIGHLIGHTS)] | None = None


class VariantSpec(JsonModel):
    model_config = CAMEL

    headline: Annotated[str, StringConstraints(max_length=MAX_HEADLINE)] | None = None
    summary: Annotated[str, StringConstraints(max_length=MAX_SUMMARY)] | None = None
    work: Annotated[list[VariantItem], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)
    projects: Annotated[list[VariantItem], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=list
    )
    education: Annotated[list[str], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)
    skills: Annotated[list[str], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)


def full_spec(base: schema.Resume) -> VariantSpec:
    """Where a new variant starts: everything in the base, in the base's order and words."""
    return VariantSpec(
        work=[VariantItem(id=item.id) for item in base.work],
        projects=[VariantItem(id=item.id) for item in base.projects],
        education=[item.id for item in base.education],
        skills=[item.id for item in base.skills],
    )


def _reworded[T: (schema.WorkItem, schema.ProjectItem)](
    items: list[T], entries: list[VariantItem]
) -> list[T]:
    by_id = {item.id: item for item in items}
    chosen: list[T] = []
    seen: set[str] = set()
    for entry in entries:
        item = by_id.get(entry.id)
        if item is None or entry.id in seen:
            continue
        seen.add(entry.id)
        if entry.highlights is not None:
            item = item.model_copy(update={"highlights": list(entry.highlights)})
        chosen.append(item)
    return chosen


def _selected[T: (schema.EducationItem, schema.SkillItem)](
    items: list[T], ids: list[str]
) -> list[T]:
    by_id = {item.id: item for item in items}
    return [by_id[item_id] for item_id in dict.fromkeys(ids) if item_id in by_id]


def compose(base: schema.Resume, spec: VariantSpec) -> schema.Resume:
    """The resume a variant renders: the base's facts in the variant's order, selection and
    wording. Ids the base no longer has are dropped, and so is a second mention of one."""
    basics = base.basics
    if spec.headline is not None:
        basics = basics.model_copy(update={"headline": spec.headline})
    return base.model_copy(
        update={
            "basics": basics,
            "summary": spec.summary if spec.summary is not None else base.summary,
            "work": _reworded(base.work, spec.work),
            "projects": _reworded(base.projects, spec.projects),
            "education": _selected(base.education, spec.education),
            "skills": _selected(base.skills, spec.skills),
        }
    )


def selects_nothing(resume: schema.Resume) -> bool:
    """A composed resume with no role, project, school or skill: its variant names nothing the
    base still has (after a new upload, whose items all get new ids). Never sent."""
    return not (resume.work or resume.projects or resume.education or resume.skills)


def _text(value: str | None) -> str | None:
    trimmed = js_trim(value) if value is not None else ""
    return trimmed or None


def _clean_items(
    items: Sequence[schema.WorkItem | schema.ProjectItem], entries: list[VariantItem]
) -> list[VariantItem]:
    by_id = {item.id: item for item in items}
    cleaned: list[VariantItem] = []
    for entry in entries:
        item = by_id.get(entry.id)
        if item is None or any(kept.id == entry.id for kept in cleaned):
            continue
        highlights = entry.highlights
        if highlights is not None:
            highlights = [line for line in (js_trim(line) for line in highlights) if line]
            if highlights == item.highlights:
                highlights = None  # the base's own bullets: keep following the base
        cleaned.append(VariantItem(id=entry.id, highlights=highlights))
    return cleaned


def clean_spec(base: schema.Resume, spec: VariantSpec) -> VariantSpec:
    """`spec` as it is stored: only ids the base has, each once; empty text as None; and bullets
    that equal the base's as None, so an untouched role keeps following the base."""
    education = {item.id for item in base.education}
    skills = {item.id for item in base.skills}
    return VariantSpec(
        headline=_text(spec.headline),
        summary=_text(spec.summary),
        work=_clean_items(base.work, spec.work),
        projects=_clean_items(base.projects, spec.projects),
        education=[item_id for item_id in dict.fromkeys(spec.education) if item_id in education],
        skills=[item_id for item_id in dict.fromkeys(spec.skills) if item_id in skills],
    )


def _stored_spec(variant: ResumeVariant) -> VariantSpec:
    return VariantSpec.read(variant.spec) or VariantSpec()


# --- Choosing ------------------------------------------------------------------------------------

# A word: letters and digits, with the "++" or "#" a language's name ends in ("C++", "C#").
_WORD = re.compile(r"\w+(?:\+\+|#)?")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _says(title_words: Sequence[str], keyword: str) -> bool:
    """Whether the title says the keyword: its words in a row, compared without the breaks
    between them, so "backend", "back-end" and "back end" are one word and "data engineer" is a
    phrase. Never part of a longer word: "data" is not in "database"."""
    target = "".join(_words(keyword))
    if not target:
        return False
    for start in range(len(title_words)):
        run = ""
        for word in title_words[start:]:
            run += word
            if run == target:
                return True
            if not target.startswith(run):
                break
    return False


def matched_keywords(keywords: Sequence[str], title: str) -> list[str]:
    """The keywords the title says, in the variant's order, each once however it is spelled."""
    title_words = _words(title)
    found: dict[str, str] = {}
    for keyword in keywords:
        key = "".join(_words(keyword))
        if key not in found and _says(title_words, keyword):
            found[key] = keyword
    return list(found.values())


def _pick(variants: Sequence[ResumeVariant], title: str) -> tuple[ResumeVariant | None, list[str]]:
    scored = [(variant, matched_keywords(variant.keywords, title)) for variant in variants]
    best = max((len(matched) for _, matched in scored), default=0)
    if best == 0:
        return next((variant for variant in variants if variant.is_default), None), []
    tied = [pair for pair in scored if len(pair[1]) == best]
    for pair in tied:
        if pair[0].is_default:
            return pair
    return min(tied, key=lambda pair: (pair[0].created_at, str(pair[0].id)))


def choose_variant(variants: Sequence[ResumeVariant], title: str) -> ResumeVariant | None:
    """The variant to send for a posting with this title; None means the base resume."""
    return _pick(variants, title)[0]


@dataclass(frozen=True, slots=True)
class Chosen:
    """The resume an application sends."""

    variant: ResumeVariant | None  # None: the base resume as it is
    resume: schema.Resume
    reason: str  # one sentence for the user


def _quoted(words: Sequence[str]) -> str:
    quoted = [f"“{word}”" for word in words]
    if len(quoted) == 1:
        return quoted[0]
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def resume_for(user_id: str, title: str, variant_id: Id | None = None) -> Chosen | None:
    """The resume to send for a posting with this title, and why. `variant_id` is the user's own
    choice (a variant's id, or `FULL_RESUME`); one that no longer exists, or that selects
    nothing from the current base, falls back to the automatic choice. None only when there is
    no base resume to send at all."""
    base = latest_base_resume(user_id)
    if base is None:
        return None
    if variant_id == FULL_RESUME:
        return Chosen(None, base.resume, CHOSE_FULL)
    variants = list_variants(user_id)
    # A variant that selects nothing from this base is never sent.
    composed = {variant.id: compose(base.resume, _stored_spec(variant)) for variant in variants}
    usable = [variant for variant in variants if not selects_nothing(composed[variant.id])]
    if variant_id is not None:
        wanted = _as_uuid(variant_id)
        for variant in usable:
            if variant.id == wanted:
                return Chosen(variant, composed[variant.id], CHOSE_VARIANT)
    variant, matched = _pick(usable, title)
    if variant is None:
        return Chosen(None, base.resume, NO_MATCH if variants else NO_VARIANTS)
    reason = f"Chosen because the title says {_quoted(matched)}." if matched else DEFAULT_VARIANT
    return Chosen(variant, composed[variant.id], reason)


# --- Reading -------------------------------------------------------------------------------------


def _as_uuid(value: Id) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def list_variants(user_id: str) -> list[ResumeVariant]:
    """The user's variants, oldest first."""
    return list(ResumeVariant.objects.for_user(user_id).order_by("created_at", "id"))


@dataclass(frozen=True, slots=True)
class VariantView:
    """A variant as the Profile page lists it."""

    variant: ResumeVariant
    spec: VariantSpec
    document_id: uuid.UUID | None  # the newest PDF made of it
    # That PDF says what the variant composes to now, under the name it has now: a render
    # today would print the same file.
    current: bool


def variant_views(user_id: str, base: schema.Resume | None) -> list[VariantView]:
    """Every variant with its newest PDF: two queries, one when there are no variants."""
    variants = list_variants(user_id)
    if not variants:
        return []
    rendered = {
        row["variant_id"]: row
        for row in ResumeRow.objects.for_user(user_id)
        .filter(
            kind=ResumeKind.VARIANT,
            rendered_document__isnull=False,
            rendered_document__user_id=user_id,
        )
        .order_by("variant_id", "-version")
        .distinct("variant_id")
        .values("variant_id", "rendered_document_id", "rendered_document__filename", "data")
    }
    views: list[VariantView] = []
    for variant in variants:
        spec = _stored_spec(variant)
        row = rendered.get(variant.id)
        current = False
        if row is not None and base is not None:
            composed = compose(base, spec)
            same_words = canonical_json(row["data"]) == canonical_json(composed.dump())
            same_name = row["rendered_document__filename"] == pdf_filename(composed, variant.name)
            current = same_words and same_name
        views.append(
            VariantView(
                variant=variant,
                spec=spec,
                document_id=row["rendered_document_id"] if row else None,
                current=current,
            )
        )
    return views


# --- Writing -------------------------------------------------------------------------------------


def _clean_name(name: str) -> str:
    cleaned = " ".join(js_trim(name).split())
    if not cleaned:
        raise VariantRefused("invalid", NAME_NEEDED)
    if len(cleaned) > MAX_NAME:
        raise VariantRefused("invalid", f"A variant's name can be {MAX_NAME} characters at most.")
    return cleaned


def clean_keywords(keywords: Sequence[str]) -> list[str]:
    """Keywords as stored: trimmed, single-spaced, and each once however it is capitalised or
    hyphenated. One with no letter or digit in it could never match and is dropped."""
    cleaned: dict[str, str] = {}
    for keyword in keywords:
        text = " ".join(js_trim(keyword).split())
        key = "".join(_words(text))
        if not key or key in cleaned:
            continue
        if len(text) > MAX_KEYWORD:
            raise VariantRefused("invalid", f"A keyword can be {MAX_KEYWORD} characters at most.")
        cleaned[key] = text
    if len(cleaned) > MAX_KEYWORDS:
        raise VariantRefused("invalid", f"A variant can have {MAX_KEYWORDS} keywords at most.")
    return list(cleaned.values())


def _duplicate(name: str) -> VariantRefused:
    return VariantRefused("duplicate", f"You already have a variant called “{name}”.")


def _violates(error: IntegrityError, constraint: str) -> bool:
    diag = getattr(error.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == constraint


def _name_taken(user_id: str, name: str, *, except_id: uuid.UUID | None = None) -> bool:
    taken = ResumeVariant.objects.for_user(user_id).filter(name__iexact=name)
    if except_id is not None:
        taken = taken.exclude(id=except_id)
    return taken.exists()


def _queue_on_user(user_id: str) -> None:
    """Inside a transaction: writers that must see each other's variants (two creates against
    the limit, two changes of the default) take turns on the user's row."""
    User.objects.select_for_update().filter(pk=user_id).values_list("pk").first()


def create_variant(user_id: str, name: str, keywords: Sequence[str]) -> ResumeVariant:
    """A new variant that shows everything in the base resume, to be narrowed in the editor."""
    cleaned_name, cleaned_keywords = _clean_name(name), clean_keywords(keywords)
    base = latest_base_resume(user_id)
    if base is None:
        raise VariantRefused("no_resume", NO_RESUME)
    try:
        with transaction.atomic():
            _queue_on_user(user_id)
            if ResumeVariant.objects.for_user(user_id).count() >= MAX_VARIANTS:
                raise VariantRefused("limit", TOO_MANY)
            if _name_taken(user_id, cleaned_name):
                raise _duplicate(cleaned_name)
            return ResumeVariant.objects.create_for(
                user_id,
                name=cleaned_name,
                keywords=cleaned_keywords,
                spec=full_spec(base.resume).dump(),
            )
    except IntegrityError as error:
        if _violates(error, _NAME_CONSTRAINT):
            raise _duplicate(cleaned_name) from None
        raise


def _locked(user_id: str, variant_id: Id) -> ResumeVariant:
    """Inside a transaction: the variant, locked. Refuses a malformed, missing or foreign id."""
    wanted = _as_uuid(variant_id)
    variant = (
        ResumeVariant.objects.for_user(user_id).select_for_update().filter(id=wanted).first()
        if wanted is not None
        else None
    )
    if variant is None:
        raise VariantRefused("not_found", VARIANT_GONE)
    return variant


def update_variant(
    user_id: str,
    variant_id: Id,
    *,
    name: str | None = None,
    keywords: Sequence[str] | None = None,
    spec: VariantSpec | None = None,
    is_default: bool | None = None,
) -> ResumeVariant:
    """Changes what is given and leaves the rest. Making a variant the default takes the mark
    from the one that had it, in the same transaction."""
    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = _clean_name(name)
    if keywords is not None:
        fields["keywords"] = clean_keywords(keywords)
    try:
        with transaction.atomic():
            if is_default:
                _queue_on_user(user_id)  # before any variant's lock, so two cannot deadlock
            variant = _locked(user_id, variant_id)
            if spec is not None:
                base = latest_base_resume(user_id)
                if base is None:
                    raise VariantRefused("no_resume", NO_RESUME)
                cleaned = clean_spec(base.resume, spec)
                if selects_nothing(compose(base.resume, cleaned)):
                    raise VariantRefused("empty", SELECTS_NOTHING)
                fields["spec"] = cleaned.dump()
            if "name" in fields and _name_taken(user_id, fields["name"], except_id=variant.id):
                raise _duplicate(fields["name"])
            if is_default is not None:
                fields["is_default"] = is_default
                if is_default:
                    ResumeVariant.objects.for_user(user_id).filter(is_default=True).exclude(
                        id=variant.id
                    ).update(is_default=False, updated_at=Now())
            for field, value in fields.items():
                setattr(variant, field, value)
            if fields:
                variant.save(update_fields=list(fields))
            return variant
    except IntegrityError as error:
        if _violates(error, _NAME_CONSTRAINT):
            raise _duplicate(fields.get("name", "")) from None
        raise


def delete_variant(user_id: str, variant_id: Id) -> None:
    """Deletes the variant and its snapshots. PDFs already made of it stay on file: an
    application that sent one keeps pointing at what it sent."""
    with transaction.atomic():
        _locked(user_id, variant_id).delete()


def snapshot(user_id: str, variant_id: Id) -> ResumeRow:
    """The stored resume a render of this variant prints: what it composes to from the newest
    base, written as its next snapshot unless the newest one already says the same."""
    with transaction.atomic():
        variant = _locked(user_id, variant_id)  # two snapshots of one variant take turns
        base = latest_base_resume(user_id)
        if base is None:
            raise VariantRefused("no_resume", NO_RESUME)
        composed = compose(base.resume, _stored_spec(variant))
        if selects_nothing(composed):
            raise VariantRefused("empty", NOTHING_TO_RENDER)
        data = composed.dump()
        newest = (
            ResumeRow.objects.for_user(user_id)
            .filter(kind=ResumeKind.VARIANT, variant=variant)
            .order_by("-version")
            .first()
        )
        if newest is not None and canonical_json(newest.data) == canonical_json(data):
            return newest
        return ResumeRow.objects.create_for(
            user_id,
            kind=ResumeKind.VARIANT,
            variant=variant,
            version=(newest.version if newest else 0) + 1,
            data=data,
        )


def resume_row(user_id: str, variant: ResumeVariant | None) -> ResumeRow | None:
    """The stored resume to render and send for a choice (`Chosen.variant`): the variant's
    snapshot, or the newest base version when no variant was chosen. None without a base."""
    if variant is None:
        return latest_base_resume_row(user_id)
    return snapshot(user_id, variant.id)
