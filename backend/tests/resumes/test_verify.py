"""The truthfulness verifier (design §11.4; port of `verify.test.ts`, 32 cases, injected judge).

The verifier is the product's guard against the one unforgivable failure: a tailored resume that
claims something the user never did. These tests pin each rule against the fabrication it exists
to catch, and pin the opposite too: a faithful tailoring must pass untouched, or the guard just
reverts every useful edit.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.resumes.schema import Resume, Revision, SkillItem, TailorChange, WorkItem
from apps.resumes.verify import (
    BasicsTarget,
    Claim,
    FieldTarget,
    ItemTarget,
    TextTarget,
    Verdict,
    check_rules,
    collect_claims,
    extract_links,
    extract_metrics,
    extract_terms,
    finalize_verification,
    reconcile_changes,
    revise_tailored,
    split_sentences,
    verify_tailored,
)

KESTREL = "w-kestrel"
NORTHGATE = "w-northgate"
INTERN = "w-intern"


def base_resume() -> Resume:
    return Resume.model_validate(
        {
            "basics": {
                "name": "Dana Reyes",
                "email": "dana@example.com",
                "phone": "(555) 0134-2087",
                "location": "Sacramento, CA",
                "links": [{"id": "l1", "url": "github.com/dana", "label": "github.com/dana"}],
            },
            "summary": (
                "Backend-leaning engineer with four years of experience building Node.js and "
                "Python services. Comfortable owning a feature from schema design through rollout."
            ),
            "work": [
                {
                    "id": KESTREL,
                    "company": "Kestrel Logistics Group",
                    "position": "Software Engineer II",
                    "location": "Sacramento, CA",
                    "startDate": "March 2023",
                    "endDate": "Present",
                    "highlights": [
                        "Rebuilt the shipment-tracking ingestion service in TypeScript on "
                        "Node.js, cutting median event processing latency from 840 ms to 190 ms "
                        "and reducing dropped events by 94%.",
                        "Designed the partitioning strategy for a 40-million-row PostgreSQL events "
                        "table, which held query times under 200 ms as volume tripled.",
                        "Mentored two junior engineers through their first on-call rotations.",
                    ],
                },
                {
                    "id": NORTHGATE,
                    "company": "Northgate Analytics",
                    "position": "Software Engineer I",
                    "location": "Remote",
                    "startDate": "July 2021",
                    "endDate": "February 2023",
                    "highlights": [
                        "Built the customer-facing reporting API (Python, FastAPI, PostgreSQL) "
                        "used by roughly 1,200 daily active accounts.",
                        "Cut the nightly aggregation job from 3 hours 10 minutes to 26 minutes "
                        "with set-based SQL.",
                    ],
                },
                {
                    "id": INTERN,
                    "company": "Willowmere Civic Tech",
                    "position": "Software Engineering Intern",
                    "startDate": "May 2020",
                    "endDate": "August 2020",
                    "highlights": [
                        "Wrote the first automated test suite for the intake application, reaching "
                        "71% line coverage."
                    ],
                },
            ],
            "education": [
                {
                    "id": "e1",
                    "institution": "Bellwater State University",
                    "area": "Computer Science",
                    "studyType": "B.S.",
                    "startDate": "2017",
                    "endDate": "2021",
                    "score": "GPA 3.6",
                    "highlights": ["Minor in Statistics."],
                }
            ],
            "projects": [
                {
                    "id": "p1",
                    "name": "Tideline",
                    "url": "github.com/dana/tideline",
                    "description": (
                        "An open-source CLI that replays PostgreSQL write-ahead log segments into "
                        "a local test database. Roughly 900 stars."
                    ),
                }
            ],
            "skills": [
                {"id": "s1", "name": "TypeScript", "category": "Languages"},
                {"id": "s2", "name": "Python", "category": "Languages"},
                {"id": "s3", "name": "PostgreSQL", "category": "Data"},
                {"id": "s4", "name": "Kafka", "category": "Data"},
                {"id": "s5", "name": "Docker", "category": "Infrastructure"},
            ],
        }
    )


def draft() -> Resume:
    """A copy to tailor."""
    return base_resume().model_copy(deep=True)


def reasons(findings: list[Any]) -> str:
    return " | ".join(finding.reason for finding in findings)


# --- check_rules: a faithful tailoring passes ----------------------------------------------------


def test_accepts_reordering_selection_and_a_reordered_skill_subset():
    base = base_resume()
    tailored = draft()
    tailored.work[0].highlights.reverse()
    tailored.work[1].highlights = [tailored.work[1].highlights[1]]
    tailored.work = tailored.work[:2]  # dropped the internship
    tailored.skills = [tailored.skills[3], tailored.skills[0]]
    tailored.projects = []

    assert check_rules(base, tailored) == []
    assert all(claim.verbatim for claim in collect_claims(base, tailored))


def test_accepts_a_rephrase_keeping_every_number_and_name_from_the_same_role():
    base = base_resume()
    tailored = draft()
    tailored.work[0].highlights[0] = (
        "Cut median event processing latency from 840 ms to 190 ms and dropped events by 94% by "
        "rebuilding the shipment-tracking ingestion service in TypeScript/Node.js."
    )
    tailored.work[1].highlights[0] = (
        "Built a customer-facing reporting API with Python, FastAPI and Postgres, serving ~1.2k "
        "daily active accounts."
    )
    assert check_rules(base, tailored) == []


def test_written_and_numeric_forms_of_a_quantity_are_the_same():
    base = base_resume()
    tailored = draft()
    tailored.work[0].highlights[2] = (
        "Mentored 2 junior engineers through their first on-call rotations."
    )
    tailored.work[0].highlights[1] = (
        "Partitioned a 40M-row PostgreSQL events table, keeping query times under 200 ms as "
        "volume grew 3x."
    )
    tailored.work[1].highlights[1] = (
        "Cut the nightly aggregation job from 3h 10m to 26m with set-based SQL."
    )
    assert reasons(check_rules(base, tailored)) == ""


def test_the_summary_may_draw_on_the_whole_resume_skills_included():
    base = base_resume()
    tailored = draft()
    tailored.summary = (
        "Backend engineer with four years of experience across TypeScript, Python and "
        "PostgreSQL. Particular strength in making data pipelines faster."
    )
    assert check_rules(base, tailored) == []


# --- check_rules: fabrication is caught ----------------------------------------------------------


def test_catches_an_injected_skill():
    tailored = draft()
    tailored.skills.append(
        SkillItem(id="s-new", source="resume", name="Kubernetes", category="Infrastructure")
    )
    findings = check_rules(base_resume(), tailored)
    assert len(findings) == 1
    assert (findings[0].section, findings[0].text, findings[0].source) == (
        "skills",
        "Kubernetes",
        "rule",
    )


def test_matches_skills_ignoring_case_and_spacing():
    tailored = draft()
    tailored.skills[0] = tailored.skills[0].model_copy(update={"name": "  typescript "})
    assert check_rules(base_resume(), tailored) == []


def test_catches_an_injected_employer():
    tailored = draft()
    tailored.work.insert(
        0,
        WorkItem(
            id="w-google",
            source="resume",
            company="Google",
            position="Senior Software Engineer",
            start_date="2024",
            end_date="Present",
            highlights=["Led the search infrastructure team."],
        ),
    )
    findings = check_rules(base_resume(), tailored)
    assert len(findings) == 1
    assert (findings[0].section, findings[0].item_id, findings[0].source) == (
        "work",
        "w-google",
        "rule",
    )
    assert findings[0].target == ItemTarget("work", "w-google")


def test_catches_a_changed_date_title_or_company():
    tailored = draft()
    tailored.work[0].start_date = "January 2023"
    tailored.work[1].position = "Senior Software Engineer"
    tailored.work[2].company = "Willowmere"

    findings = check_rules(base_resume(), tailored)
    assert [finding.target for finding in findings] == [
        FieldTarget("work", KESTREL, "start_date"),
        FieldTarget("work", NORTHGATE, "position"),
        FieldTarget("work", INTERN, "company"),
    ]
    assert 'from "March 2023" to "January 2023"' in findings[0].reason


def test_allows_leaving_out_a_location_or_gpa_but_not_changing_one():
    base = base_resume()
    omitted = draft()
    omitted.work[0].location = None
    omitted.education[0].score = None
    assert check_rules(base, omitted) == []

    changed = draft()
    changed.education[0].score = "GPA 3.9"
    target = check_rules(base, changed)[0].target
    assert isinstance(target, FieldTarget) and target.field == "score"


def test_catches_an_invented_metric_even_when_the_digits_appear_elsewhere():
    # The base says "40-million-row": a 40, but not 40%.
    tailored = draft()
    tailored.work[0].highlights[1] = (
        "Designed the partitioning strategy for a PostgreSQL events table, which increased "
        "revenue 40%."
    )
    findings = check_rules(base_resume(), tailored)
    assert len(findings) == 1
    assert "40%" in findings[0].reason
    assert findings[0].target == TextTarget(f"work:{KESTREL}:highlight:1")


def test_catches_a_number_computed_from_the_originals():
    tailored = draft()
    tailored.work[0].highlights[0] = (
        "Made the shipment-tracking ingestion service 77% faster by rebuilding it in TypeScript "
        "on Node.js."
    )
    assert "77%" in reasons(check_rules(base_resume(), tailored))


def test_catches_a_metric_moved_from_one_role_to_another():
    tailored = draft()
    tailored.work[1].highlights[1] = "Cut the nightly aggregation job by 94% with set-based SQL."
    findings = check_rules(base_resume(), tailored)
    assert len(findings) == 1
    assert findings[0].item_id == NORTHGATE
    assert "this role" in findings[0].reason


def test_catches_a_technology_the_role_never_mentioned_even_a_listed_skill():
    tailored = draft()
    tailored.work[0].highlights[2] = (
        "Mentored two junior engineers through their first on-call rotations on Kubernetes and "
        "Kafka."
    )
    findings = check_rules(base_resume(), tailored)
    assert len(findings) == 1
    assert "Kubernetes and Kafka" in findings[0].reason


def test_a_capitalised_first_word_is_not_a_technology():
    tailored = draft()
    tailored.work[0].highlights[2] = (
        "Coached two junior engineers through their first on-call rotations."
    )
    assert check_rules(base_resume(), tailored) == []


def test_catches_a_link_that_is_not_on_the_resume_and_allows_one_that_is():
    base = base_resume()
    invented = draft()
    invented.projects[0].description = (
        "An open-source CLI that replays PostgreSQL write-ahead log segments. Demo at "
        "tideline-demo.io."
    )
    assert "tideline-demo.io" in reasons(check_rules(base, invented))

    real = draft()
    real.projects[0].description = (
        "An open-source CLI (github.com/dana/tideline) that replays PostgreSQL write-ahead log "
        "segments into a local test database."
    )
    assert check_rules(base, real) == []


def test_catches_an_invented_fact_in_the_summary_against_the_whole_resume():
    tailored = draft()
    tailored.summary = "Backend engineer with 7 years of experience. Deep expertise in Rust."
    findings = check_rules(base_resume(), tailored)
    assert [finding.target for finding in findings] == [
        TextTarget("summary:sentence:0"),
        TextTarget("summary:sentence:1"),
    ]


def test_catches_changed_basics():
    tailored = draft()
    tailored.basics.email = "dana@other.com"
    assert check_rules(base_resume(), tailored)[0].target == BasicsTarget("email")


# --- extractors ----------------------------------------------------------------------------------


def keys(text: str) -> list[list[str]]:
    return [metric.keys for metric in extract_metrics(text)]


def test_reads_numbers_with_their_units():
    assert keys("reduced errors by 40%") == [["pct:40"]]
    assert keys("40 percent") == [["pct:40"]]
    assert keys("3x faster") == [["x:3"]]
    assert keys("$2M ARR") == [["n:2000000"]]
    assert keys("10k users") == [["n:10000"]]
    assert keys("1,200 accounts") == [["n:1200"]]
    assert keys("a 40-million-row table") == [["n:40000000"]]
    assert keys("in 2019") == [["n:2019"]]
    assert keys("12 engineers") == [["n:12"]]
    assert keys("840 ms") == [["n:840"]]


def test_reads_number_words_and_multipliers():
    assert keys("twenty-five services") == [["n:25"]]
    assert keys("six incidents") == [["n:6"]]
    assert keys("volume tripled") == [["x:3"]]
    assert keys("one of the first") == []


def test_does_not_read_names_as_numbers():
    assert keys("S3, EC2 and Q3 planning") == []
    assert extract_terms("S3, EC2 and Q3 planning") == ["S3", "EC2", "Q3"]


def test_finds_named_things_but_not_ordinary_capitalised_first_words():
    assert extract_terms("Built a service in TypeScript/Node.js with Kafka and C++.") == [
        "TypeScript",
        "Node.js",
        "Kafka",
        "C++",
    ]
    assert extract_terms("Led the team's on-call rotation.") == []


def test_finds_links_and_emails():
    assert extract_links("See https://www.Example.com/x/ or mail a@b.co") == [
        "a@b.co",
        "example.com/x",
    ]
    assert extract_links("Built with Node.js and Next.js") == []


def test_splits_sentences_without_breaking_abbreviations():
    assert split_sentences("B.S. in Computer Science. Served in the U.S. Army. Done!") == [
        "B.S. in Computer Science.",
        "Served in the U.S. Army.",
        "Done!",
    ]


# --- revise_tailored -----------------------------------------------------------------------------


def test_reverts_fields_drops_invented_items_and_skills_and_restores_the_bullet():
    base = base_resume()
    tailored = draft()
    tailored.work[0].start_date = "January 2023"
    tailored.work.append(
        WorkItem(
            id="w-fake",
            source="resume",
            company="Stripe",
            position="Staff Engineer",
            highlights=["Scaled payments."],
        )
    )
    tailored.skills.append(SkillItem(id="s-x", source="resume", name="Rust", category=None))
    tailored.work[0].highlights[1] = (
        "Designed the partitioning strategy for a PostgreSQL events table, which increased "
        "revenue 40%."
    )

    resume, revisions = revise_tailored(base, tailored, check_rules(base, tailored))

    assert [item.id for item in resume.work] == [KESTREL, NORTHGATE, INTERN]
    assert resume.work[0].start_date == "March 2023"
    assert "Rust" not in [skill.name for skill in resume.skills]
    assert resume.work[0].highlights[1] == base.work[0].highlights[1]
    assert sorted(revision.action for revision in revisions) == [
        "dropped",
        "dropped",
        "replaced",
        "reverted",
    ]
    assert check_rules(base, resume) == []


def test_drops_a_flagged_bullet_that_resembles_nothing():
    base = base_resume()
    tailored = draft()
    tailored.work[0].highlights.append("Shipped a Kubernetes operator for GPU scheduling.")

    resume, revisions = revise_tailored(base, tailored, check_rules(base, tailored))
    assert resume.work[0].highlights == base.work[0].highlights
    assert revisions[0].action == "dropped"


def test_never_duplicates_an_original_already_on_the_resume():
    base = base_resume()
    tailored = draft()
    # Two rewrites of the same original: one kept verbatim, one embellished.
    tailored.work[0].highlights = [
        base.work[0].highlights[2],
        "Mentored two junior engineers through their first on-call rotations and led hiring "
        "for 5 roles.",
    ]
    resume, _ = revise_tailored(base, tailored, check_rules(base, tailored))
    assert resume.work[0].highlights == [base.work[0].highlights[2]]


def test_drops_only_the_unsupported_summary_sentence_and_falls_back_to_the_base():
    base = base_resume()
    partly = draft()
    partly.summary = "Backend engineer with four years of experience. Deep expertise in Rust."
    one, _ = revise_tailored(base, partly, check_rules(base, partly))
    assert one.summary == "Backend engineer with four years of experience."

    wholly = draft()
    wholly.summary = "Engineer with 9 years of experience. Deep expertise in Rust."
    every, _ = revise_tailored(base, wholly, check_rules(base, wholly))
    assert every.summary == base.summary
    assert check_rules(base, every) == []


# --- verify_tailored with a judge ----------------------------------------------------------------

EMBELLISHED = (
    "Led the team that rebuilt the shipment-tracking ingestion service in TypeScript on "
    "Node.js, cutting latency from 840 ms to 190 ms."
)


class RecordingJudge:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[list[Claim]] = []

    async def __call__(
        self, base: Resume, claims: list[Claim], changes: list[TailorChange]
    ) -> dict[str, Verdict]:
        self.calls.append(claims)
        return self.answer(claims)


async def test_asks_the_judge_only_about_rewritten_lines_that_passed_the_rules():
    base = base_resume()
    tailored = draft()
    tailored.work[0].highlights[0] = EMBELLISHED  # passes the rules, fails the judge
    tailored.work[0].highlights[1] = "Partitioned a table, raising revenue 40%."  # fails the rules

    judge = RecordingJudge(
        lambda claims: {
            claim.key: Verdict(False, "The original does not say they led a team.")
            if claim.text.startswith("Led")
            else Verdict(True, None)
            for claim in claims
        }
    )
    result = await verify_tailored(base, tailored, [], judge=judge)
    assert len(judge.calls) == 1
    assert [claim.text for claim in judge.calls[0]] == [EMBELLISHED]
    assert result.model_checked == 1
    assert [finding.source for finding in result.findings] == ["rule", "model"]

    resume, revisions = revise_tailored(base, tailored, result.findings)
    assert resume.work[0].highlights[:2] == base.work[0].highlights[:2]
    verification = finalize_verification(
        base=base, final=resume, result=result, revisions=revisions
    )
    assert verification.passed is True
    assert verification.revised is True
    assert verification.unsupported == []
    assert verification.model_error is None
    assert len(verification.revisions) == 2


async def test_does_not_call_the_judge_when_nothing_was_rewritten():
    judge = RecordingJudge(lambda claims: {})
    tailored = draft()
    tailored.work[0].highlights.reverse()
    result = await verify_tailored(base_resume(), tailored, [], judge=judge)
    assert judge.calls == []
    assert result.findings == []


async def test_a_line_the_judge_skipped_is_unverified():
    tailored = draft()
    tailored.work[0].highlights[0] = EMBELLISHED
    result = await verify_tailored(
        base_resume(), tailored, [], judge=RecordingJudge(lambda claims: {})
    )
    assert len(result.findings) == 1
    assert result.findings[0].source == "model"


async def test_reverts_every_rewrite_when_the_judge_cannot_run():
    base = base_resume()
    tailored = draft()
    tailored.work[0].highlights[0] = EMBELLISHED
    tailored.summary = "Backend engineer with four years of experience building Node.js services."

    async def failing(*_: Any) -> dict[str, Verdict]:
        raise RuntimeError("provider unavailable")

    result = await verify_tailored(base, tailored, [], judge=failing)
    assert result.model_error == "provider unavailable"

    resume, revisions = revise_tailored(base, tailored, result.findings)
    assert resume.work[0].highlights[0] == base.work[0].highlights[0]
    assert resume.summary == base.summary
    verification = finalize_verification(
        base=base, final=resume, result=result, revisions=revisions
    )
    assert verification.passed is True
    assert verification.model_error == "provider unavailable"


# --- reconcile_changes ---------------------------------------------------------------------------


def test_reconcile_removes_reverted_entries_and_logs_forgotten_rewrites():
    base = base_resume()
    final = draft()
    final.work[1].highlights[0] = (
        "Built a customer-facing reporting API with Python and FastAPI for roughly 1,200 daily "
        "active accounts."
    )
    changes = [
        TailorChange(
            section="work",
            item_id=KESTREL,
            before=base.work[0].highlights[0],
            after="Led the rebuild of the ingestion service.",
            reason="Leadership",
            evidence_fact_ids=[KESTREL],
        ),
        TailorChange(
            section="work",
            item_id="w-invented",
            before=None,
            after="Scaled payments.",
            reason="Payments",
            evidence_fact_ids=[],
        ),
        TailorChange(
            section="skills",
            item_id=None,
            before=None,
            after="Led with TypeScript and PostgreSQL",
            reason="Matches the stack",
            evidence_fact_ids=["s1", "s3", "not-an-id"],
        ),
    ]
    out = reconcile_changes(
        base=base,
        final=final,
        changes=changes,
        revisions=[
            Revision(
                section="work",
                item_id=KESTREL,
                text="Led the rebuild of the ingestion service.",
                reason="x",
                source="model",
                action="replaced",
                replacement=base.work[0].highlights[0],
            )
        ],
    )
    assert len(out) == 2
    assert (out[0].section, out[0].evidence_fact_ids) == ("skills", ["s1", "s3"])
    assert (out[1].section, out[1].item_id, out[1].before, out[1].after) == (
        "work",
        NORTHGATE,
        base.work[1].highlights[0],
        final.work[1].highlights[0],
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Cut costs 26m", [["n:26000000", "n:26"]]),
        ("grew 2 times", [["x:2", "n:2"]]),
        ("a $3B market", [["n:3000000000"]]),
        ("2.3M events", [["n:2300000"]]),
        ("seventeen services", [["n:17"]]),
    ],
)
def test_suffix_readings(text: str, expected: list[list[str]]):
    """Extra: the ambiguous and scaled suffixes render keys as JS did (`n:20`, never
    `n:20.0`)."""
    assert keys(text) == expected
