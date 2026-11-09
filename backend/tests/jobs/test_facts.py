"""Posting facts (#56): what `facts.read_facts` reads from a title and a description, how the
mirror stores it, and the labelled benchmark's fixture. The postings here are written for the
tests; the hand-labelled real ones are measured by `manage.py facts_benchmark`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.jobs import fact_store, facts
from apps.jobs.facts import level_from_title, read_facts, years_mentions
from apps.jobs.ingest import ingest_jobs
from apps.jobs.models import Clearance, Job, JobLevel, WorkAuth
from tests.factories import make_company, make_job
from tests.jobs.test_ingest import posting

LABELS = Path(__file__).resolve().parents[1] / "fixtures" / "facts" / "labels.json"


# --- the title -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("Software Engineering Intern, Summer 2027", "intern"),
        ("Hardware Co-op (Fall)", "intern"),
        ("Internal Tools Engineer", "unknown"),  # "internal" is not "intern"
        ("Software Engineer, New Grad (2027 Start)", "new_grad"),
        ("University Graduate - Data Scientist", "new_grad"),
        ("Software Engineer - Early Career", "new_grad"),
        ("Senior Software Engineer, New Grad Program", "new_grad"),  # the program wins
        ("Campus Recruiter, Technology", "unknown"),  # recruits graduates; is not one
        ("Early Careers Program Manager", "unknown"),
        ("Staff Software Engineer", "staff_plus"),
        ("Principal Product Designer", "staff_plus"),
        ("Director of Engineering", "staff_plus"),
        ("VP, Finance", "staff_plus"),
        ("Senior Staff Engineer", "staff_plus"),  # one level, not two
        ("Senior Director, Product", "staff_plus"),
        ("Staff/Sr. Staff Machine Learning Engineer", "staff_plus"),
        ("Member of Technical Staff", "unknown"),  # a title, not the staff level
        ("Chief of Staff", "unknown"),
        ("Staff Accountant", "unknown"),
        ("Senior Software Engineer", "senior"),
        ("Sr. Data Analyst", "senior"),
        ("Tech Lead, Payments", "senior"),
        ("Lead Generation Specialist", "unknown"),  # the leads are sales leads
        ("Engineering Manager, Platform", "senior"),
        ("Manager II, Applied Science", "senior"),  # a manager of people
        ("Product Manager, Growth", "unknown"),  # "manager" is the role's name
        ("Account Manager", "unknown"),
        ("Senior / Staff Product Designer", "senior"),  # two levels: the lower
        ("Staff or Senior Backend Engineer", "senior"),
        ("Software Engineer III", "senior"),
        ("Software Engineer II, Backend", "mid"),
        ("SDE 2", "mid"),
        ("Mid-Level Frontend Engineer", "mid"),
        ("Software Engineer I", "entry"),
        ("Technician I-III, Facilities", "entry"),  # the lowest rung
        ("Analyst 1/2", "entry"),
        ("Junior Software Engineer", "entry"),
        ("Associate Product Designer", "entry"),
        ("Level 3 Support Engineer", "unknown"),  # a support tier, not a ladder
        ("Web3 Engineer", "unknown"),
        ("Software Engineer", "unknown"),
    ],
)
def test_the_level_a_title_states(title: str, level: str) -> None:
    assert level_from_title(title)[0] == level, title


# --- years ---------------------------------------------------------------------------------------

REQUIRED = "Requirements\n"


@pytest.mark.parametrize(
    ("text", "years", "preferred"),
    [
        (REQUIRED + "• 5+ years of software engineering experience", 5, None),
        (REQUIRED + "• 3-5 years of experience with distributed systems", 3, None),
        (REQUIRED + "• 3\u20135+ years of hands-on experience, preferably in aerospace", 3, None),
        (REQUIRED + "• At least two (2) years of work experience in software development", 2, None),
        (REQUIRED + "• Minimum of 8 years building and shipping ML systems", 8, None),
        (REQUIRED + "• 7+ years in business operations, strategy or pricing", 7, None),
        ("You have 6+ years of experience in professional software development.", 6, None),
        ("Experience: 12+ years in B2B SaaS sales.", 12, None),
        # The largest requirement is the requirement.
        (REQUIRED + "• 5+ years of experience\n• 2+ years leading projects", 5, None),
        (
            REQUIRED + "• 10+ years of industry experience, including 4+ years in leadership",
            10,
            None,
        ),
        # A preference stated as one, by its section or its sentence.
        ("Nice to have\n• 5+ years of experience with Kubernetes", None, 5),
        (REQUIRED + "• 3+ years of experience preferred", None, 3),
        (REQUIRED + "• Ideally 4+ years of experience in fintech", None, 4),
        ("The ideal candidate will have at least 7 years of experience selling SaaS.", None, 7),
        (
            "We prefer:\n• 3+ years of experience on ML\nYou have:\n• 2+ years of experience",
            2,
            None,
        ),
        # ... but a preference about something else leaves the years required.
        (
            REQUIRED + "• 5+ years industry experience, ideally from aerospace or automotive",
            5,
            None,
        ),
        (REQUIRED + "• 3+ years of experience in sales; a SaaS background preferred.", 3, None),
        # Degree alternatives: the figure that goes with the bachelor's.
        (
            REQUIRED
            + "• Bachelor's degree and 5+ years of experience, or 9+ years in lieu of a degree",
            5,
            None,
        ),
        (REQUIRED + "• BS and 5 years of experience or MS and 3 years", 5, None),
        (
            REQUIRED
            + "• 8+ years of experience and a bachelor's degree or equivalent, or 12+ years",
            8,
            None,
        ),
        (
            REQUIRED
            + "• Advanced degree; or bachelor's degree and 2+ years of experience; or 6+ years",
            2,
            None,
        ),
        (
            REQUIRED + "• Bachelor's degree in CS, IS, or a related field and 3+ years of "
            "experience; OR 5+ years in lieu of a degree",
            3,
            None,
        ),
        (
            REQUIRED + "• Typically requires 7 years of experience with a Bachelor's "
            "degree; or 6 years and a Master's",
            7,
            None,
        ),
        # The degree alone is enough: nothing is asked of someone who has it.
        (
            REQUIRED + "• Bachelor's degree in engineering; OR 2+ years of professional "
            "experience in lieu of a degree",
            None,
            None,
        ),
        (REQUIRED + "• Bachelor's degree and/or 5+ years of relevant work experience", None, None),
        # No degree route at all: what stands in is all there is.
        (REQUIRED + "• High school diploma or GED and 2+ years of experience", 2, None),
        # A ceiling and "none required" are zero.
        ("You have less than two years of engineering experience.", 0, None),
        (REQUIRED + "• 6 months to 2 years of technical work experience", 0, None),
        ("No prior experience required; we will train you.", 0, None),
        # Not requirements at all.
        ("We have been named a best workplace for 7+ years and counting.", None, None),
        (
            "We made the Cloud 100 four years in a row, thanks to people with experience.",
            None,
            None,
        ),
        ("Building on more than 20 years of sensor innovation, we design payloads.", None, None),
        ("Benefits\n• Equity with 4 years vesting", None, None),
        (REQUIRED + "• Drivers must have held a licence for a minimum of 3 years", None, None),
        (REQUIRED + "• Must be 18 years of age or older", None, None),
        ("In just five years, our work has reached two million people.", None, None),
        ("Founded in 2015, we have 10 years of experience serving customers.", None, None),
        (
            "Why join\n• The product has 10 years of proof behind it. You are "
            "selling something real.",
            None,
            None,
        ),
        ("", None, None),
    ],
)
def test_the_years_a_posting_requires(text: str, years: int | None, preferred: int | None) -> None:
    read = read_facts("Software Engineer", text)
    assert (read.years_min, read.years_preferred) == (years, preferred), text


def test_an_established_candidate_is_not_an_established_company() -> None:
    text = "Who you are\n• An established sourcer with 8+ years of experience at a search firm"
    assert read_facts("Sourcer", text).years_min == 8
    [mention] = years_mentions(text)
    assert (mention.years, mention.preferred, mention.alternative) == (8, False, False)


# --- the level, with the text and the years ------------------------------------------------------


def test_the_level_comes_from_the_title_then_the_text_then_the_years() -> None:
    asks_five = REQUIRED + "• 5+ years of experience"
    assert read_facts("Senior Engineer", asks_five).level_from == "title"
    by_years = read_facts("Software Engineer", asks_five)
    assert (by_years.level, by_years.level_from) == (JobLevel.SENIOR, "years")
    assert read_facts("Software Engineer", REQUIRED + "• 3 years of experience").level == "mid"
    assert read_facts("Software Engineer", REQUIRED + "• 1+ years of experience").level == "entry"

    said = read_facts("Software Engineer", "This is an entry-level role on our platform team.")
    assert (said.level, said.level_from) == (JobLevel.ENTRY, "text")
    grads = read_facts("Software Engineer", "We are hiring new graduates for the class of 2027.")
    assert (grads.level, grads.level_from) == (JobLevel.NEW_GRAD, "text")
    # Words in passing do not outweigh what the posting requires.
    senior = read_facts(
        "Software Engineer",
        "This is an entry-level role.\n" + REQUIRED + "• 6+ years of experience",
    )
    assert (senior.level, senior.level_from) == (JobLevel.SENIOR, "years")

    nothing = read_facts("Software Engineer", "Build things with us.")
    assert (nothing.level, nothing.level_from, nothing.years_min) == (JobLevel.UNKNOWN, None, None)


def test_an_entry_word_gives_way_to_the_years() -> None:
    counsel = read_facts("Associate General Counsel", REQUIRED + "• 15+ years of legal experience")
    assert (counsel.level, counsel.level_from) == (JobLevel.SENIOR, "years")
    analyst = read_facts("Associate Analyst", REQUIRED + "• 1-2 years of experience")
    assert (analyst.level, analyst.level_from) == (JobLevel.ENTRY, "title")


def test_a_title_with_two_levels_takes_the_lower_rungs_years() -> None:
    text = (
        "Senior designers bring 5+ years of experience. Staff designers bring 8+ years of "
        "experience."
    )
    read = read_facts("Senior / Staff Product Designer", text)
    assert (read.level, read.years_min) == (JobLevel.SENIOR, 5)
    ladder = (
        "AS A TECHNICIAN I:\n• 2+ years of experience\nAS A TECHNICIAN III:\n• 8+ years "
        "of experience"
    )
    read = read_facts("Technician I-III", ladder)
    assert (read.level, read.years_min) == (JobLevel.ENTRY, 2)


# --- authorization and clearance -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "work_auth"),
    [
        ("Must be a U.S. citizen due to the nature of the work.", "citizen_only"),
        ("• U.S. citizenship due to the customers assigned to this role", "citizen_only"),
        ("US citizenship is required for this position.", "citizen_only"),
        (
            "To conform to export regulations, applicants must be a U.S. citizen, lawful permanent "
            "resident, refugee or asylee, or be eligible to obtain the required authorizations.",
            "us_person",
        ),
        ("Applicants must be a U.S. person as defined by ITAR.", "us_person"),
        ("You must be a U.S. citizen or national to apply.", "us_person"),
        ("We are unable to sponsor employment visas for this role.", "no_sponsorship"),
        (
            "You must be authorized to work in the U.S. without the need for visa sponsorship.",
            "no_sponsorship",
        ),
        ("This role is not eligible for visa sponsorship.", "no_sponsorship"),
        (
            "The Company may not be able to employ candidates in certain U.S. visa categories, or "
            "support future H-1B sponsorship at this time.",
            "no_sponsorship",
        ),
        ("Visa sponsorship is available for this position.", "sponsors"),
        ("We do sponsor visas, and will make every effort to get you one.", "sponsors"),
        # Mentions that restrict nobody.
        ("Familiarity with export control (ITAR/EAR) requirements in defense hiring.", "unknown"),
        (
            "This position may require access to information protected under "
            "U.S. export control laws.",
            "unknown",
        ),
        ("We will sponsor you for relocation if you need to move.", "unknown"),
        ("You must be authorized to work in the United States.", "unknown"),
    ],
)
def test_work_authorization(text: str, work_auth: str) -> None:
    read = read_facts("Engineer", text)
    assert read.work_auth == work_auth, text
    assert ("workAuth" in read.evidence) == (work_auth != WorkAuth.UNKNOWN)


@pytest.mark.parametrize(
    ("text", "clearance"),
    [
        ("Active TS/SCI security clearance", "required"),
        ("Must have top secret clearance / clearance with SCI eligibility", "required"),
        ("Active Secret clearance (TS preferred)", "required"),
        ("Security clearance and eligibility required", "required"),
        ("Ability to obtain and maintain a U.S. Secret security clearance.", "eligible"),
        ("Active U.S. Secret security clearance (or ability to obtain and maintain).", "eligible"),
        (
            "This position requires the ability to obtain a DoD government "
            "clearance at the Secret level",
            "eligible",
        ),
        (
            "Active Secret clearance required.\nAbility to obtain a TS/SCI security clearance.",
            "eligible",
        ),
        # Not asked of the applicant.
        ("Security clearance eligibility preferred (for government sites).", "none"),
        ("An active security clearance is a bonus, not a requirement.", "none"),
        (
            "Experience recruiting candidates who hold or are eligible for security clearances",
            "none",
        ),
        ("You will handle customs clearance for inbound freight.", "none"),
        ("", "none"),
    ],
)
def test_clearance(text: str, clearance: str) -> None:
    read = read_facts("Engineer", text)
    assert read.clearance == clearance, text
    assert ("clearance" in read.evidence) == (clearance != Clearance.NONE)


def test_evidence_is_the_sentence_without_its_bullet() -> None:
    read = read_facts(
        "Software Engineer II", REQUIRED + "• 3+ years of experience\n• Must be a U.S. citizen."
    )
    assert read.evidence == {
        "years": "3+ years of experience",
        "level": "Software Engineer II",
        "workAuth": "Must be a U.S. citizen.",
    }
    assert read.dump() == {"levelFrom": "title", "evidence": read.evidence}


# --- the mirror ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_ingest_reads_the_facts_and_reads_them_again_when_the_posting_changes() -> None:
    acme = make_company(slug="acme", ats_slug="acme")
    first = posting(
        acme, 1, title="Software Engineer II", description="Requirements\n• 3+ years of experience"
    )
    ingest_jobs(acme.id, [first])
    job = Job.objects.get(external_id="1")
    assert (job.level, job.years_min, job.work_auth, job.clearance) == ("mid", 3, "unknown", "none")
    assert job.facts_version == facts.VERSION
    assert job.facts == {
        "levelFrom": "title",
        "evidence": {"years": "3+ years of experience", "level": "Software Engineer II"},
    }

    changed = posting(
        acme,
        1,
        title="Software Engineer II",
        description="Requirements\n• 6+ years of experience\n• Active TS/SCI security clearance",
    )
    assert ingest_jobs(acme.id, [changed]).updated == 1
    job.refresh_from_db()
    assert (job.level, job.years_min, job.clearance) == ("mid", 6, "required")


@pytest.mark.django_db
def test_postings_from_before_are_caught_up_in_batches_newest_first() -> None:
    acme = make_company(slug="acme", ats_slug="acme")
    jobs = [
        make_job(
            company=acme,
            title="Senior Engineer",
            description_text="Requirements\n• 8+ years of experience",
        )
        for _ in range(5)
    ]
    assert fact_store.count_stale() == 5
    assert fact_store.read_stale(limit=3) == 3
    assert fact_store.count_stale() == 2
    assert fact_store.read_stale(limit=3) == 2
    assert fact_store.read_stale(limit=3) == 0
    read = Job.objects.get(id=jobs[0].id)
    assert (read.level, read.years_min, read.facts_version) == ("senior", 8, facts.VERSION)

    # An older reading is behind again once the version moves on.
    Job.objects.filter(id=jobs[1].id).update(facts_version=facts.VERSION - 1)
    assert fact_store.count_stale() == 1


def test_the_labels_fixture_is_whole() -> None:
    labels = json.loads(LABELS.read_text())
    assert len(labels) == 210 and len({label["id"] for label in labels}) == 210
    assert {label["set"] for label in labels} == {"bench", "second", "fresh"}
    for label in labels:
        assert label["level"] in JobLevel.values
        assert label["work_auth"] in WorkAuth.values
        assert label["clearance"] in Clearance.values
        assert label["years_min"] is None or 0 <= label["years_min"] <= 25
