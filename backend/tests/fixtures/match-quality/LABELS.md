# The match-quality labels

`labels.json` holds 40 postings read against one fixed candidate: the sample candidate every
bake-off uses (`apps/accounts/sample_resume.json` with `SAMPLE_PREFERENCES`: a backend engineer
with six years, in Oakland, looking for mid-level software or backend roles in the Bay Area,
hybrid, at $150,000 or more). Each is called `strong`, `apply`, `maybe` or `skip` by
`RUBRIC.md`, which is the deep review's own scale.

## Where they came from

- **The postings** are the bake-off's own stable sample of the mirror on 2026-11-09, widened to
  40: 24 that the candidate could plausibly want (engineering roles at a plausible level, in the
  Bay Area or remote) and 16 others. Their text is other people's writing, so it is not in this
  repository: it is in the private notes repository
  (`notes/benchmarks/match-quality/postings.jsonl`), and `match_benchmark` falls back to the
  mirror for postings still in it.
- **The labels** were made by two readers working alone from the rubric, the candidate and the
  postings' full text, without seeing each other's work or any model's score. Both readers were
  Claude agents, not people. They agreed on 37 of the 40, and each of the three disagreements
  was one band apart; those are settled to the lower band. `readers` keeps what each said and
  `why` each one's sentence.

## What they are good for, and what they are not

- 26 are `skip`, 5 `maybe`, 9 `apply` and none `strong` (each reader called one posting strong,
  but not the same one). So this set says whether a model keeps what is worth applying to and
  archives what is not; it says nothing yet about telling `strong` from `apply`.
- One candidate. A model that reads this resume well may read a new graduate's badly.
- Forty postings: one posting is 2.5 points of agreement.
- A reading by a model, checked by another model, is not the owner's judgment. The labels are
  worth replacing with the owner's own: open `labels.json`, change `label` where you disagree,
  and add postings near the top of the scale.
