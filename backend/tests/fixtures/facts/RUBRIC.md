# Labelling rubric: what a job posting asks of an applicant

You label each posting with four facts. Judge only from the posting's TITLE and TEXT as given.
Apply the rules literally; when a rule does not settle it, prefer the less specific label
(`unknown`, `null`, `none`).

## 1. `years_min` (integer or null)

The minimum number of years of experience the posting REQUIRES of a candidate who holds a
bachelor's degree.

- A range gives its lower bound: "3-5 years" is 3; "0-2 years" is 0.
- "5+ years", "at least 5 years", "minimum of 5 years", "5 or more years", "over 5 years" are 5.
- Several required figures: take the largest required one ("5+ years of software engineering and
  2+ years leading projects" is 5; "3+ years with Python, 6+ years overall" is 6).
- Degree alternatives: use the figure for a bachelor's degree ("BS and 5 years, or MS and 3" is 5;
  "Bachelor's and 5+ years, or 9 years in lieu of a degree" is 5; "High school and 8 years or
  Bachelor's and 4" is 4; "PhD and 0, MS and 2, BS and 4" is 4).
- Figures stated ONLY as preferred / nice to have / ideal / bonus / a plus do not count: the
  answer is null unless some other figure is required. Put the smallest such figure in
  `years_preferred`. A sentence like "The ideal candidate has 3+ years" is preferred, not required.
- A ceiling is not a minimum: "less than 2 years", "up to 2 years", "0 to N" give 0. An explicit
  "no experience required" gives 0.
- Durations that are not experience requirements never count: the company's age or history,
  benefits, vesting, contract length, ages, degree lengths ("4-year degree"), "within 2 years of
  graduation", project durations.
- A posting that covers several levels at once ("Engineer I: 0-2 years; Engineer II: 2-5") gives
  the lowest level's figure.
- Nothing required is stated: null.

## 2. `level` (one of: intern, new_grad, entry, mid, senior, staff_plus, unknown)

Decide from the TITLE first, in this order:

1. Internship or co-op -> `intern`.
2. Explicitly for new or recent graduates, a university / campus-hire / "early career" /
   rotational graduate program -> `new_grad`. (A recruiter or program manager OF such programs is
   not a new-grad role.)
3. Staff, Principal, Distinguished, Director, Head of, VP, C-level -> `staff_plus`.
   ("Member of Technical Staff" and "Chief of Staff" are NOT staff level; "Staff Accountant" is
   not staff level.)
4. Senior, Sr, Lead (as a level: "Lead Engineer", "Tech Lead"; not "Lead Generation"), a people
   manager title ("Engineering Manager", "Manager of X", "Senior Manager"), or a ladder numeral
   III / IV / 3 / 4 straight after the role noun ("Engineer III") -> `senior`.
   Role names that merely contain "manager" (Product Manager, Program Manager, Project Manager,
   Account Manager, Customer Success Manager, Marketing Manager, Office Manager...) state NO level.
5. Ladder numeral II / 2 straight after the role noun, "mid-level", "intermediate" -> `mid`.
6. Junior, Jr, Entry-level, Associate, Trainee, Apprentice, ladder numeral I / 1 -> `entry`,
   EXCEPT when the posting requires 3 or more years (`years_min` >= 3): then use rule 8.
   When a title names two rungs ("Engineer I/II"), the lowest decides.

If the title states no level:

7. If the first part of the text explicitly says the role is for new or recent graduates
   (and it does not require more than 2 years) -> `new_grad`; if the text explicitly calls it an
   entry-level role or says no experience is required (and it does not require more than 2 years)
   -> `entry`.
8. Otherwise from the required years: `years_min` 0 or 1 -> `entry`; 2, 3 or 4 -> `mid`;
   5 or more -> `senior`.
9. No required years either -> `unknown`.

## 3. `work_auth` (one of: citizen_only, us_person, no_sponsorship, sponsors, unknown)

The strictest that applies:

- `citizen_only`: the posting requires U.S. citizenship (often with a clearance).
- `us_person`: the candidate must be a "U.S. person" (citizen, lawful permanent resident,
  refugee or asylee), typically for ITAR. Generic export-control compliance language that only
  says an export licence may be needed, or that offers may be conditioned on authorization,
  without restricting who may apply, does NOT count.
- `no_sponsorship`: says it will not sponsor a visa, or that candidates must be authorized to
  work without sponsorship.
- `sponsors`: says visa sponsorship is available.
- `unknown`: says nothing of the above (a plain "must be authorized to work in the U.S." alone is
  `unknown`).

## 4. `clearance` (one of: required, eligible, none)

- `required`: an active / current security clearance is required, or a clearance is required
  with no mention that it can be obtained after hire.
- `eligible`: the candidate must be able to obtain (and maintain) a clearance, including
  "active clearance or the ability to obtain one".
- `none`: no security clearance is mentioned. ("Background check" is not a clearance.)

## Output

One JSON object per line, in the same order as the postings, with exactly these keys:

{"n": <n>, "id": "<id>", "level": "...", "years_min": <int|null>, "years_preferred": <int|null>,
 "work_auth": "...", "clearance": "...", "quote": "<the sentence(s) you took years from, or the
 sentence that set the level, at most 200 characters; empty string if none>", "unsure": <true|false>}

Set "unsure": true when the rubric left you genuinely torn, and say why in the quote.

## Clarifications (added after the first round; they do not change any rule)

- Experience stated only in months ("6+ months", "3+ months of applied experience") gives
  `years_min` null, unless it is the lower end of a range that reaches years ("6 months to 2
  years" is 0).
- A title that names two levels or rungs ("Senior / Staff Engineer", "Engineer I/II",
  "Technician I-III") takes the lower one, and `years_min` is the lower rung's figure.
  "Senior Staff" and "Sr. Staff" are one level (`staff_plus`), as is "Senior Director".
- A hedged statement that the employer "may not be able to support" visa sponsorship is
  `no_sponsorship`.
