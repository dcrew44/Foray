"""What a posting asks of whoever applies, read once per posting (#56).

Whether someone can get a job at all turns on a few facts that are the same for every reader:
what level the role is, how many years it asks for, whether the employer sponsors a visa or wants
a citizen, whether it needs a clearance. Until now each user's search rediscovered them, by title
words in the prefilter and by a model reading the whole posting. Here they are read once, at
ingest, into columns the prefilter can filter on, and they travel with the posting.

`read_facts(title, text)` is deterministic: patterns over the title and the cleaned description.
It errs toward saying nothing. A posting wrongly called senior is filtered out of a junior's
search and never seen again, while one called `unknown` only costs a fraction of a cent to score;
so a level comes from the title's own words, from a few unambiguous phrases, or from the years
the posting requires, and anything else stays `unknown`.

- **Level** (`intern`, `new_grad`, `entry`, `mid`, `senior`, `staff_plus`, `unknown`). A program
  word wins over everything ("Senior Software Engineer, New Grad Program" is a new-grad role),
  then the title's level word (the lower, when it names two: "Senior / Staff", "Engineer I/II"),
  then a phrase in the text, then the years required. "Associate" and "Junior" give way to the
  years when the posting requires three or more ("Associate General Counsel, 15+ years").
- **Years** (`years_min`): the experience the posting *requires* of someone with a bachelor's
  degree. Every "N years" in the text is a candidate. One that is about something else (the
  company's age, a vesting schedule, a degree's length, an award "four years in a row") is
  dropped; one in a "preferred" section or sentence is kept apart (`years_preferred`); one that
  stands in for a degree ("or 9 years in lieu of a degree") gives way to the figure that goes
  with the bachelor's. Of those that remain the largest is the requirement: a posting that wants
  "5+ years of software experience and 2+ years leading projects" wants five. A ceiling ("less
  than two years") and "no experience required" are zero.
- **Work authorization** (`citizen_only`, `us_person`, `no_sponsorship`, `sponsors`, `unknown`)
  and **clearance** (`required`, `eligible`, `none`), each with the sentence that said so.

`evidence` keeps the words each fact was read from, so the app can show why. Bump `VERSION` when
the reading changes; `stale_facts` then finds what to read again. The reading is measured
against hand labels (`manage.py facts_benchmark`, `tests/fixtures/facts/`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from apps.jobs.models import Clearance, JobLevel, WorkAuth

__all__ = [
    "VERSION",
    "PostingFacts",
    "YearsMention",
    "level_from_title",
    "read_facts",
    "years_mentions",
]

VERSION = 1

_I = re.IGNORECASE

# --- the title -----------------------------------------------------------------------------------

# "Member of Technical Staff" is a title, not the staff level; "Chief of Staff" is a job.
_NOT_STAFF = re.compile(
    r"\b(?:members?\s+of\s+(?:the\s+)?technical\s+staff|technical\s+staff|chief\s+of\s+staff"
    r"|staff\s+(?:accountant|auditor|nurse|attorney|counsel|writer|assistant|associate"
    r"|pharmacist|physician|therapist|photographer|reporter|editor))\b",
    _I,
)
# "Lead" as the thing being generated or developed, not a level.
_NOT_LEAD = re.compile(
    r"\blead(?:s)?\s+(?:gen(?:eration)?|development|qualification|nurtur\w*|routing|scoring"
    r"|management|acquisition)\b",
    _I,
)

_INTERN = re.compile(r"\b(?:intern|interns|internship|internships|co-?op)\b", _I)
_NEW_GRAD = re.compile(
    r"\b(?:new[\s-]*grad(?:uate)?s?|university\s+grad(?:uate)?s?|recent\s+grad(?:uate)?s?"
    r"|college\s+grad(?:uate)?s?|grad(?:uate)?\s+(?:program(?:me)?|scheme|rotation(?:al)?)"
    r"|graduate\s+(?:software|engineer|developer|analyst|trainee|consultant|associate|data)"
    r"|early[\s-]*careers?|early\s+in\s+career|campus\s+hire|class\s+of\s+20\d\d"
    r"|20\d\d\s+(?:new\s+)?grad(?:uate)?s?|rotational)\b",
    _I,
)
# Someone who recruits or runs programmes for new graduates is not one.
_ABOUT_NEW_GRADS = re.compile(
    r"\b(?:recruit\w*|sourc(?:er|ing)|talent|programs?\s+(?:manager|lead|specialist|coordinator)"
    r"|university\s+relations)\b",
    _I,
)
_STAFF_PLUS = re.compile(
    r"\b(?:staff|principal|distinguished|director|vice\s+president|vp|svp|evp"
    r"|head\s+of|chief\s+\w+\s+officer|c[tfoi]o)\b",
    _I,
)
# A people manager is senior; "manager" inside a role's name (Product Manager, Account Manager)
# is not a level, so only the forms that are unmistakably a manager of people count.
_SENIOR = re.compile(
    r"(?:\b(?:senior|sr|lead|engineering\s+manager|manager\s+of|senior\s+manager|group\s+manager"
    r"|managing\s+director)\b|\A\s*manager\b)",
    _I,
)
# "Senior / Staff Engineer", "Staff or Senior Designer": two levels, open to the lower. Not
# "Staff / Sr. Staff" or "Senior Staff / Principal", where "senior" only modifies the higher word.
_HIGHER = r"(?:staff|principal|director|distinguished)"
_TWO_LEVELS = re.compile(
    rf"\b(?:senior|sr)\b(?!\.?\s+{_HIGHER}\b)\.?(?:\s+[\w.+#-]+){{0,4}}?\s*"
    rf"(?:/|\||&|\bor\b|\band\b)\s*(?:(?:senior|sr)\.?\s+)?(?:staff|principal)\b"
    rf"|\b(?:staff|principal)\b(?:\s+[\w.+#-]+){{0,4}}?\s*(?:/|\||&|\bor\b|\band\b)\s*"
    rf"(?:senior|sr)\b(?!\.?\s+{_HIGHER}\b)",
    _I,
)
_MID = re.compile(r"\b(?:mid[\s-]*level|intermediate)\b", _I)
_ENTRY = re.compile(
    r"\b(?:junior|jr|entry[\s-]*level|associate|apprentice|apprenticeship|trainee"
    r"|fellowship|residency)\b",
    _I,
)

# A numeral is a level only straight after the kind of word a ladder hangs on: "Engineer II",
# "SDE 2", "Analyst I". ("Level 3 Support" and "Web3" are not ladders.) The numeral itself is
# matched as written: a lowercase "i" is a pronoun.
_LADDER = (
    r"engineer|developer|programmer|scientist|analyst|designer|researcher|technician|specialist"
    r"|administrator|accountant|consultant|associate|architect|sde|swe|sre|mts|tester|writer"
    r"|recruiter|representative|coordinator|strategist|technologist|executive|operator|inspector"
    r"|machinist|mechanic|welder|assembler|buyer|planner|officer|counsel|auditor|agent|partner"
)
_RUNG = r"IV|III|II|I|[1-4]"
_NUMERAL = re.compile(rf"\b(?i:{_LADDER})s?[\s,/-]+(?P<n>{_RUNG})\b(?![\d.])")
# "Technician I-III", "Engineer I/II": the rungs after the first.
_MORE_RUNGS = re.compile(rf"\A\s*(?:[-/–&]|to\b|or\b|and\b)\s*(?:{_RUNG})\b")
_NUMERAL_RANK = ("I", "1", "II", "2", "III", "3", "IV", "4")
_NUMERAL_LEVEL = {
    "I": JobLevel.ENTRY,
    "1": JobLevel.ENTRY,
    "II": JobLevel.MID,
    "2": JobLevel.MID,
    "III": JobLevel.SENIOR,
    "3": JobLevel.SENIOR,
    "IV": JobLevel.SENIOR,
    "4": JobLevel.SENIOR,
}


def _title_level(title: str) -> tuple[str, str | None, bool]:
    """`(level, the words that state it, whether the title names more than one level)`."""
    found = _INTERN.search(title)
    if found:
        return JobLevel.INTERN, found.group(0), False
    found = _NEW_GRAD.search(title)
    if found and not _ABOUT_NEW_GRADS.search(title):
        return JobLevel.NEW_GRAD, found.group(0), False

    plain = _NOT_LEAD.sub(" ", _NOT_STAFF.sub(" ", title))
    # Where a title names two levels the lower decides: it is open to someone at that one.
    found = _TWO_LEVELS.search(plain)
    if found:
        return JobLevel.SENIOR, found.group(0), True
    found = _STAFF_PLUS.search(plain)
    if found:
        return JobLevel.STAFF_PLUS, found.group(0), False
    found = _SENIOR.search(plain)
    if found:
        return JobLevel.SENIOR, found.group(0), False
    numerals = list(_NUMERAL.finditer(plain))
    if numerals:
        lowest = min((match.group("n") for match in numerals), key=_NUMERAL_RANK.index)
        several = len(numerals) > 1 or any(
            _MORE_RUNGS.match(plain[match.end() :]) for match in numerals
        )
        return _NUMERAL_LEVEL[lowest], lowest, several
    found = _MID.search(plain)
    if found:
        return JobLevel.MID, found.group(0), False
    found = _ENTRY.search(plain)
    if found:
        return JobLevel.ENTRY, found.group(0), False
    return JobLevel.UNKNOWN, None, False


def level_from_title(title: str) -> tuple[str, str | None]:
    """The level a title states, and the words that state it; `unknown` when it states none."""
    level, words, _ = _title_level(title)
    return level, words


# --- years ---------------------------------------------------------------------------------------

_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
}
_N = r"\d{1,2}|" + "|".join(_WORDS)
# "3-5 years", "5+ years", "2 or more years", "two (2) years", "1 year", "6 months to 2 years"; not
# "5-year" (a vesting schedule, a degree), not "18 years old", not "10 years ago".
_FIGURE = re.compile(
    rf"(?<![\w$.,/-])(?:(?P<months>\d{{1,2}}\s*months?\s*(?:-|–|—|to)\s*)"
    rf"|(?P<lo>{_N})\s*\+?\s*(?:-|–|—|to|or)\s*)?(?P<hi>{_N})(?:\s*\((?:{_N})\))?"
    rf"\s*(?P<plus>\+)?\s*(?:or\s+more\s+|plus\s+)?(?:years?|yrs?)\b\.?"
    rf"(?!\s*(?:old\b|of\s+age\b|ago\b|-))",
    _I,
)
# What comes just before a figure that is not a requirement: "over the past 10 years", "within
# 2 years of graduating", "after 1 year", "in just five years".
_NOT_A_REQUIREMENT_BEFORE = re.compile(
    r"\b(?:past|last|next|previous|coming|every|within|after|first|nearly|almost|each|just|only"
    r"|for\s+(?:over|more\s+than|nearly|almost)|in\s+(?:over|under))\s*(?:the\s+)?\Z",
    _I,
)
# "less than two years", "up to 3 years": a ceiling, which asks for nothing.
_CEILING_BEFORE = re.compile(
    r"\b(?:less\s+than|fewer\s+than|under|up\s+to|no\s+more\s+than|at\s+most"
    r"|max(?:imum)?(?:\s+of)?)\s*\Z",
    _I,
)
# What comes just after one: "four years in a row".
_NOT_A_REQUIREMENT_AFTER = re.compile(
    r"\A\s*(?:in\s+a\s+row|running\b|straight\b|consecutive|and\s+counting|now\b)", _I
)
# A line that is about the company, pay, benefits or a licence rather than the candidate's
# experience.
_NOT_ABOUT_THE_CANDIDATE = re.compile(
    r"\b(?:(?:founded|established)\s+in|since\s+(?:19|20)\d\d|anniversary|vesting|vests|vested"
    r"|year[\s-]over[\s-]year|parental\s+leave|sabbatical|401\s*\(?k\)?|pto\b"
    r"|paid\s+time\s+off|contract\s+(?:term|length|duration)|years?\s+contract|fixed[\s-]term"
    r"|licen[sc]e\s+for|suspensions?|driving\s+record)\b",
    _I,
)
_BULLET = re.compile(r"^[•●◦‣·*–-]\s*")
# A line that opens with the figure is a requirement stated as one: "5+ years in B2B sales",
# "You have 3+ years ...", "Minimum of 8 years ...".
_OPENS_WITH_FIGURE = re.compile(
    r"\A(?:[•●◦‣·*–-]\s*)?(?:(?:you(?:\s+have|\s+bring|’ve|'ve)?"
    r"|have|has|with|a\s+minimum\s+of|minimum(?:\s+of)?|at\s+least|typically|approximately|about"
    r"|around|over|experience\s*:?)\s+){0,3}\Z",
    _I,
)
_EXPERIENCE_NOUN = r"(?:experience|exp|expertise|background|track\s+record|practice|tenure)"
# What follows a figure that is experience: "years of hands-on experience", "years in B2B
# sales", "years as a recruiter", "years building data systems".
_EXPERIENCE_AFTER = re.compile(
    rf"\A\s*(?:[’']\s*)?(?:(?:in|as)\b"
    rf"|(?:of\s+)?(?:(?:[\w/&+,’'()#-]+\s+){{0,7}}?{_EXPERIENCE_NOUN}\b"
    r"|(?:working|building|developing|designing|leading|managing|programming|coding|writing"
    r"|selling|operating|shipping|delivering|running|supporting|teaching|practicing"
    r"|hands[\s-]on|professional|industry|relevant|related|post[\s-]?\w+)\b))",
    _I,
)
# "Experience: 5+ years", "experience of at least 5 years".
_EXPERIENCE_BEFORE = re.compile(
    rf"\b{_EXPERIENCE_NOUN}\s*(?:of|:|–|-|\()?\s*(?:(?:a\s+)?(?:minimum|min)\s*(?:of\s+)?"
    r"|at\s+least\s+|approximately\s+|about\s+|over\s+)?\Z",
    _I,
)
_DEGREE = re.compile(
    r"\b(?:bachelor|master|ph\.?d|doctorate|degree|b\.?s\.?c?|m\.?s\.?c?|b\.?a\.?|mba)\b", _I
)
_PREFERRED = re.compile(
    r"\b(?:prefer(?:red|ably|ence)?|nice[\s-]to[\s-]haves?|bonus(?:es)?|ideal(?:ly)?"
    r"|desir(?:ed|able)|a\s+plus|is\s+a\s+plus|pluses|not\s+required|optional(?:ly)?"
    r"|would\s+be\s+(?:great|nice|a\s+plus)|stand\s+out|extra\s+credit|good\s+to\s+have"
    r"|helpful|advantage(?:ous)?)\b",
    _I,
)
# A preference stated straight after the figure, before any comma: "5+ years of experience
# preferred". After a comma it is about something else ("5+ years in industry, ideally aerospace").
_PREFERRED_AFTER = re.compile(
    r"\A[^,;.]{0,80}?\b(?:preferred|is\s+a\s+plus|a\s+plus|desired|desirable|nice\s+to\s+have"
    r"|is\s+ideal|ideal\b|bonus|optional|not\s+required|helpful)\b",
    _I,
)
_SENTENCE_BREAK = re.compile(r"[;.]\s")
_REQUIRED_HEADING = re.compile(
    r"\b(?:require(?:d|ments?)|minimum|basic|must[\s-]haves?|qualifications?"
    r"|what\s+you(?:'ll|’ll|\s+will)?\s+(?:need|bring|have)|who\s+you\s+are|about\s+you"
    r"|you\s+(?:have|are|bring|might|should)|what\s+we(?:'re|’re|\s+are)?\s+look(?:ing)?\s+for"
    r"|we(?:'re|’re|\s+are)\s+looking\s+for|skills|experience"
    r"|your\s+(?:background|profile|experience|expertise)|essential"
    r"|(?:good|great|strong)\s+fit)\b",
    _I,
)
_PREFERRED_HEADING = re.compile(
    r"\b(?:prefer\w*|nice[\s-]to[\s-]haves?|bonus(?:es)?|pluses|plus\s+points|desir(?:ed|able)"
    r"|ideal(?:ly)?|extra\s+credit|good\s+to\s+have|additional\s+(?:qualifications|skills)"
    r"|stand\s+out|brownie\s+points|even\s+better|we(?:'d|’d|\s+would)\s+love)\b",
    _I,
)
# Experience a posting accepts in place of a degree: not what it asks of someone who has one.
_IN_LIEU = re.compile(
    r"\b(?:in\s+lieu|instead\s+of\s+(?:an?\s+)?(?:\w+\s+)?degree"
    r"|without\s+(?:an?\s+)?(?:\w+\s+)?degree|no\s+degree|high\s+school|ged"
    r"|associate(?:'|’)?s?\s+degree)\b",
    _I,
)
_BACHELOR = re.compile(
    r"(?:\bbachelor(?:'|’)?s?\b|\bb\.?s\.?c?\b|\bb\.?a\.?\b|\bundergraduate\s+degree\b"
    r"|\b(?:university|college)\s+degree\b)",
    _I,
)
# "... degree or 5 years", "... degree and/or a minimum of 5 years": what follows is offered
# instead of what came before.
_OR_JUST_BEFORE = re.compile(
    r"(?:\band/or|\bor)\s+(?:an?\s+)?(?:(?:minimum|min)\s+(?:of\s+)?|at\s+least\s+)?\Z", _I
)
_STARTS_WITH_OR = re.compile(r"\A\s*or\b", _I)
_NO_EXPERIENCE = re.compile(
    r"\b(?:no\s+(?:prior\s+|previous\s+)?(?:work\s+|professional\s+|industry\s+)?experience\s+"
    r"(?:is\s+)?(?:required|necessary|needed)"
    r"|experience\s+(?:is\s+)?(?:preferred\s+but\s+)?not\s+(?:required|necessary|needed))\b",
    _I,
)
_HEADING_MAX = 90
_YEARS_MAX = 25
# In a section that lists requirements, an item this short is taken at its word.
_ITEM_MAX = 220

type _Figure = tuple[int, int, int]  # years, where the phrase starts, where it ends


def _number(text: str) -> int:
    return int(text) if text.isdigit() else _WORDS[text.lower()]


@dataclass(frozen=True, slots=True)
class YearsMention:
    years: int  # the least the phrase asks for: "3-5 years" is 3, "less than 2 years" is 0
    preferred: bool
    alternative: bool  # stands in for a degree; the figure that goes with the bachelor's wins
    line: str


def _lines(text: str) -> list[str]:
    """The text's lines, with a bullet that sits on a line of its own joined to its sentence."""
    lines: list[str] = []
    pending = ""
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _BULLET.fullmatch(line):
            pending = "• "
            continue
        lines.append(pending + line)
        pending = ""
    return lines


def _is_heading(line: str) -> bool:
    if len(line) > _HEADING_MAX or _BULLET.match(line):
        return False
    if line.endswith(":"):
        return True
    return not line.endswith((".", "!", ";", ",")) and len(line.split()) <= 8


def _figures(line: str, *, listed: bool) -> list[_Figure]:
    """Each figure on a line that is a requirement on the candidate's experience. `listed`: the
    line is an item in a section that lists requirements, where it is taken at its word."""
    found: list[_Figure] = []
    names_degree = _DEGREE.search(line) is not None
    for match in _FIGURE.finditer(line):
        before, after = line[: match.start()], line[match.end() :]
        if _NOT_A_REQUIREMENT_BEFORE.search(before) or _NOT_A_REQUIREMENT_AFTER.search(after):
            continue
        said = (
            _OPENS_WITH_FIGURE.match(before) is not None
            or _EXPERIENCE_AFTER.match(after) is not None
            or _EXPERIENCE_BEFORE.search(before) is not None
        )
        if not said and not (listed and (names_degree or len(line) <= _ITEM_MAX)):
            continue
        if _CEILING_BEFORE.search(before) or match.group("months"):
            years = 0
        else:
            low = match.group("lo")
            years = _number(low if low is not None else match.group("hi"))
        if years <= _YEARS_MAX:
            found.append((years, match.start(), match.end()))
    return found


def _with_degree(line: str, figures: list[_Figure]) -> list[tuple[_Figure, bool]]:
    """Each figure of a line with whether it only stands in for a degree, read against the
    degrees the line names. Figures the line offers *instead of* a degree that is enough on its
    own are left out altogether.

    A posting asks one thing of someone with a bachelor's degree and often offers another route:
    "Bachelor's and 5+ years, or 9+ years in lieu of a degree"; "an advanced degree, or a
    bachelor's and 2+ years, or 6+ years". The figure that goes with the bachelor's (the first
    after it in its clause, else the last before it, unless an "or" sets it apart) is the
    requirement, and the others stand in.
    """
    in_lieu = _IN_LIEU.search(line) is not None
    degree = _BACHELOR.search(line)
    if degree is None:
        return [(figure, in_lieu) for figure in figures]

    clause_start = line.rfind(";", 0, degree.start()) + 1
    clause_end = line.find(";", degree.end())
    clause_end = len(line) if clause_end == -1 else clause_end

    primary: _Figure | None = None
    after = [figure for figure in figures if degree.end() <= figure[1] < clause_end]
    if after and not _OR_JUST_BEFORE.search(line[degree.end() : after[0][1]]):
        primary = after[0]
    if primary is None:
        before = [figure for figure in figures if clause_start <= figure[1] < degree.start()]
        if before and not _OR_JUST_BEFORE.search(line[before[-1][2] : degree.start()]):
            primary = before[-1]
    if primary is not None:
        return [(figure, figure != primary) for figure in figures]

    # The degree stands alone, so it is enough: what its clause, an "or ..." clause or an "in
    # lieu" offers instead asks nothing of someone who has the degree.
    kept: list[tuple[_Figure, bool]] = []
    for figure in figures:
        own_clause = line.rfind(";", 0, figure[1]) + 1
        instead = (
            in_lieu
            or clause_start <= figure[1] < clause_end
            or _STARTS_WITH_OR.match(line[own_clause:]) is not None
        )
        if not instead:
            kept.append((figure, False))
    return kept


def _is_preferred(line: str, figure: _Figure) -> bool:
    """Whether the line states this figure as a preference: before it in its own sentence
    ("Ideally 5+ years"), or straight after it ("5+ years preferred")."""
    before = _SENTENCE_BREAK.split(line[: figure[1]])[-1]
    if _PREFERRED.search(before) is not None:
        return True
    return _PREFERRED_AFTER.match(line[figure[2] :]) is not None


def years_mentions(text: str) -> list[YearsMention]:
    """Every "N years" in the text that is a requirement on the candidate, in order, each marked
    preferred or not by its section and its own sentence, and marked `alternative` when it only
    stands in for a degree."""
    mentions: list[YearsMention] = []
    section = "open"  # open | required | preferred | other
    for line in _lines(text):
        if _is_heading(line) and not _FIGURE.search(line):
            if _PREFERRED_HEADING.search(line):
                section = "preferred"
            elif _REQUIRED_HEADING.search(line):
                section = "required"
            else:
                section = "other"
            continue
        if _NO_EXPERIENCE.search(line):
            mentions.append(YearsMention(0, False, False, line))
        if _NOT_ABOUT_THE_CANDIDATE.search(line):
            continue
        figures = _figures(line, listed=section in ("required", "preferred"))
        for figure, alternative in _with_degree(line, figures):
            preferred = section == "preferred" or _is_preferred(line, figure)
            mentions.append(YearsMention(figure[0], preferred, alternative, line))
    return mentions


# --- authorization and clearance -----------------------------------------------------------------

_US = r"(?:u\.?\s?s\.?(?:a\.?)?|united\s+states)"
# What follows "U.S. citizen" when citizenship is one of several ways to qualify.
_OR_ANOTHER_STATUS = (
    rf"\s*(?:,|/|\bor\b)\s*(?:(?:a|an)\s+)?(?:{_US}\s+)?"
    r"(?:national|lawful|legal|permanent|green\s+card|refugee|asylee)"
)
_CITIZEN_ONLY = re.compile(
    rf"(?:\b(?:must|need\s+to|required\s+to|shall)\s+be\s+(?:an?\s+)?{_US}\s+citizens?\b"
    rf"(?!{_OR_ANOTHER_STATUS})"
    rf"|\b{_US}\s+citizenship\s+(?:is\s+)?(?:a\s+)?(?:required|requirement|mandatory)"
    rf"|\b{_US}\s+citizens?\s+only\b|\brequires?\s+{_US}\s+citizenship\b"
    rf"|\bcitizenship\s*:\s*{_US}\b"
    rf"|(?m:^[^\w\n]*{_US}\s+citizenship\b(?!{_OR_ANOTHER_STATUS})))",
    _I,
)
# Who may apply at all (ITAR): a citizen, a permanent resident, a refugee or asylee. A posting
# that only mentions ITAR or export control restricts nobody by saying so.
_US_PERSON = re.compile(
    rf"(?:\b{_US}\s+persons?\b|\b{_US}\s+citizens?{_OR_ANOTHER_STATUS}"
    rf"|\bgreen\s+card\s+holders?\b)",
    _I,
)
_NO_SPONSORSHIP = re.compile(
    r"(?:\b(?:not|unable\s+to|cannot|can(?:'|’)?t|won(?:'|’)?t|will\s+not|do(?:es)?\s+not"
    r"|doesn(?:'|’)t|don(?:'|’)t)\s+(?:currently\s+|be\s+able\s+to\s+|able\s+to\s+)?"
    r"(?:provid\w+|offer\w*|support\w*|consider\w*)?\s*(?:any\s+|a\s+|an\s+)?"
    r"(?:visa\s+|work\s+|employment\s+|immigration\s+|h-?1b\s+)*sponsor"
    r"|\bwithout\s+(?:the\s+need\s+for\s+|requiring\s+|needing\s+)?(?:\w+\s+){0,3}sponsorship\b"
    r"|\bsponsorship\s+(?:is\s+)?(?:not|un)\s*(?:available|offered|provided|supported)"
    r"|\bnot\s+eligible\s+for\s+(?:\w+\s+){0,3}sponsorship\b"
    r"|\bnot\s+be\s+able\s+to\s+[^\n]{0,220}?\bsponsorship\b"
    r"|\bno\s+(?:visa\s+|work\s+|employment\s+|immigration\s+|h-?1b\s+){0,2}sponsorship\b)",
    _I,
)
_SPONSORS = re.compile(
    r"(?:\b(?:visa|immigration|h-?1b)\s+sponsorship\s+(?:is\s+)?"
    r"(?:available|offered|provided|possible)"
    r"|\bsponsorship\s+(?:is\s+)?(?:available|offered|provided)\b"
    r"|\bwe\s+(?:do\s+|will\s+|can\s+|are\s+able\s+to\s+)?sponsor\s+(?:\w+\s+){0,2}visas?\b"
    r"|\bwe\s+(?:offer|provide|support)\s+(?:\w+\s+){0,2}sponsorship\b)",
    _I,
)

# A security clearance, in the forms postings give it; not the clearances of the people a
# recruiter hires, and not a customs or medical one.
_CLEARANCE_WORD = (
    r"(?:(?:security|government|secret|dod|doe|federal)\s+(?:\w+\s+){0,2}?clearance(?!s)"
    r"|ts/sci|top\s+secret|[ql]\s+clearance(?!s)|clearance\s+(?:level|at\s+the))"
)
_CLEARANCE_ANY = re.compile(_CLEARANCE_WORD, _I)
# One that can be obtained after hire, including "active or the ability to obtain".
_CLEARANCE_OBTAINABLE = re.compile(
    r"\b(?:(?:ability|able|eligib\w+|willing(?:ness)?)\s+to\s+(?:obtain|get|acquire|receive"
    r"|qualify\s+for)|eligible\s+for|obtain(?:ing)?\s+(?:and\s+maintain(?:ing)?\s+)?"
    r"(?:an?\s+|the\s+)|(?:or|and/or)\s+eligib\w+|(?:security\s+)?clearance\s+eligibility)\b",
    _I,
)
# One the candidate must already hold.
_CLEARANCE_HELD = re.compile(
    rf"(?:\b(?:active|current|existing|valid|in[\s-]scope)\s+(?:\w+\s+){{0,3}}?{_CLEARANCE_WORD}"
    rf"|\bts/sci\s+(?:with|w/)\s+(?:\w+\s+){{0,2}}poly)",
    _I,
)
_CLEARANCE_REQUIRED = re.compile(
    rf"(?:\b(?:must|need\s+to|required\s+to)\s+(?:have|hold|possess|maintain)\s+(?:an?\s+)?"
    rf"(?:\w+\s+){{0,4}}?{_CLEARANCE_WORD}"
    rf"|\b{_CLEARANCE_WORD}\s+(?:\w+\s+){{0,3}}?(?:is\s+)?(?:required|mandatory|needed)"
    rf"|\brequires?\s+(?:an?\s+)?(?:\w+\s+){{0,4}}?{_CLEARANCE_WORD})",
    _I,
)
_PARENTHESES = re.compile(r"\([^)]*\)")


def _shown(line: str, limit: int = 240) -> str:
    """A line as evidence: no bullet, at most `limit` characters."""
    line = _BULLET.sub("", line)
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _clearance(text: str) -> tuple[str, str | None]:
    """`eligible` when a line says a clearance can be obtained; `required` when one wants a
    clearance already held, or requires one, and no line offers the chance to obtain it. A
    clearance that is only preferred is not asked for."""
    required: str | None = None
    eligible: str | None = None
    for line in text.split("\n"):
        if not _CLEARANCE_ANY.search(line):
            continue
        # "Active Secret clearance (TS preferred)" still requires the Secret.
        if _PREFERRED.search(_PARENTHESES.sub(" ", line)):
            continue
        shown = _shown(line.strip())
        if _CLEARANCE_OBTAINABLE.search(line):
            eligible = eligible or shown
        elif _CLEARANCE_HELD.search(line) or _CLEARANCE_REQUIRED.search(line):
            required = required or shown
    if required and not eligible:
        return Clearance.REQUIRED, required
    if eligible:
        return Clearance.ELIGIBLE, eligible
    return Clearance.NONE, None


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    """The line of the first match, as evidence."""
    found = pattern.search(text)
    if found is None:
        return None
    left = text.rfind("\n", 0, found.start()) + 1
    right = text.find("\n", found.end())
    return _shown(text[left : right if right != -1 else len(text)].strip())


# --- level from the text -------------------------------------------------------------------------

_TEXT_NEW_GRAD = re.compile(
    r"\b(?:new[\s-]*grad(?:uate)?\s+(?:role|position|program(?:me)?|hire|opportunit\w+)"
    r"|(?:for|seeking|hiring)\s+(?:new|recent)\s+(?:college\s+|university\s+)?grad(?:uate)?s"
    r"|recent\s+(?:college\s+|university\s+)?grad(?:uate)?s?\s+(?:are\s+)?(?:welcome|encouraged)"
    r"|graduating\s+(?:in|by|between)\s+(?:\w+\s+)?20\d\d|class\s+of\s+20\d\d"
    r"|expected\s+graduation)\b",
    _I,
)
_TEXT_ENTRY = re.compile(r"\bentry[\s-]level\s+(?:role|position|job|opportunit\w+)\b", _I)
# How much of a posting may name its level in passing; a mention far down is usually about a
# programme the company also runs.
_TEXT_HEAD = 1800
# A text or an early-career title word is believed only while the posting requires no more.
_EARLY_YEARS_MAX = 2


def _level_from_years(years: int) -> str:
    if years <= 1:
        return JobLevel.ENTRY
    if years <= 4:
        return JobLevel.MID
    return JobLevel.SENIOR


# --- the reading ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PostingFacts:
    level: str = JobLevel.UNKNOWN
    # Where the level came from: "title", "text", "years", or None for `unknown`.
    level_from: str | None = None
    years_min: int | None = None
    # The least a "preferred" figure asks for, when the posting requires none.
    years_preferred: int | None = None
    work_auth: str = WorkAuth.UNKNOWN
    clearance: str = Clearance.NONE
    evidence: dict[str, str] = field(default_factory=dict)

    def dump(self) -> dict[str, Any]:
        """`jobs.facts`: what the columns do not hold."""
        return {"levelFrom": self.level_from, "evidence": self.evidence}


def read_facts(title: str, text: str | None) -> PostingFacts:
    """The facts of one posting from its title and cleaned description."""
    body = text or ""
    evidence: dict[str, str] = {}

    level, words, several_levels = _title_level(title)

    mentions = years_mentions(body)
    required = [mention for mention in mentions if not mention.preferred]
    required = [mention for mention in required if not mention.alternative] or required
    preferred = [mention for mention in mentions if mention.preferred]
    years_min: int | None = None
    years_preferred: int | None = None
    if required:
        # The largest requirement, or the lowest rung's when the title names several levels.
        pick = min if several_levels else max
        chosen = pick(required, key=lambda mention: mention.years)
        years_min = chosen.years
        evidence["years"] = _shown(chosen.line)
    elif preferred:
        least = min(preferred, key=lambda mention: mention.years)
        years_preferred = least.years
        evidence["years"] = _shown(least.line)

    level_from: str | None = "title" if level != JobLevel.UNKNOWN else None
    if words:
        evidence["level"] = title
    asks_little = years_min is None or years_min <= _EARLY_YEARS_MAX
    if level == JobLevel.ENTRY and years_min is not None and not asks_little:
        # "Associate General Counsel" with 15+ years: the years say what the word does not.
        level, level_from = _level_from_years(years_min), "years"
        evidence.pop("level", None)
    elif level == JobLevel.UNKNOWN:
        soft = years_preferred is None or years_preferred <= _EARLY_YEARS_MAX
        said = _first(_TEXT_NEW_GRAD, body[:_TEXT_HEAD]) if asks_little and soft else None
        if said:
            level, level_from, evidence["level"] = JobLevel.NEW_GRAD, "text", said
        else:
            said = _first(_TEXT_ENTRY, body) if asks_little and soft else None
            if said:
                level, level_from, evidence["level"] = JobLevel.ENTRY, "text", said
            elif years_min is not None:
                level, level_from = _level_from_years(years_min), "years"

    work_auth = WorkAuth.UNKNOWN
    for value, pattern in (
        (WorkAuth.CITIZEN_ONLY, _CITIZEN_ONLY),
        (WorkAuth.US_PERSON, _US_PERSON),
        (WorkAuth.NO_SPONSORSHIP, _NO_SPONSORSHIP),
        (WorkAuth.SPONSORS, _SPONSORS),
    ):
        said = _first(pattern, body)
        if said:
            work_auth, evidence["workAuth"] = value, said
            break

    clearance, said = _clearance(body)
    if said:
        evidence["clearance"] = said

    return PostingFacts(
        level=level,
        level_from=level_from,
        years_min=years_min,
        years_preferred=years_preferred,
        work_auth=work_auth,
        clearance=clearance,
        evidence=evidence,
    )
