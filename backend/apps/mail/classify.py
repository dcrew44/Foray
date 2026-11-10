"""What an employer's email is, read from its subject line and Gmail's snippet (#26).

`classify(subject, snippet)` answers `confirmation` ("we received your application"),
`rejection`, `interview` (an invitation to talk, or to take an assessment) or `other`. It is
deterministic: patterns, no model, and nothing but those two short strings, because the body is
never read. It errs toward `other`. A confirmation is recorded as proof without anyone looking
and a rejection is put to the user as a suggestion, so a wrong label costs more than a missing
one, and a message it cannot place is left alone.

The rules run in this order, the narrow ones first:

1. **Not an answer at all** (`other`): job alerts and digests, marketing from job boards, a
   reminder to finish a draft, account housekeeping (verify your email, reset your password)
   and advice ("5 interview tips").
2. **Rejection.** Before everything that follows, because a rejection usually opens with the
   words of a confirmation ("Thank you for applying. Unfortunately…") and one sent after an
   interview mentions the interview. A phrase said about a case and not about the reader does
   not count: "if you do not hear from us, we have moved forward with other candidates" and
   "applicants not selected will not be contacted" are a confirmation's small print.
3. **An offer** is `other`: it is none of the three, and too important to guess at.
4. **Interview.** An invitation or a request for times, not a promise of one: "we will reach
   out to schedule an interview if there is a fit" is a confirmation, and "thank you for
   interviewing with us" is the note that follows an interview, not an invitation to one.
5. **A soft rejection**: "after careful consideration", which is how a rejection begins when
   the snippet ends before it gets to the point.
6. **Confirmation.** A statement of receipt ("we received your application", "your application
   was submitted", "we will review your application"), or thanks for applying in the subject
   line, or thanks in the snippet followed by "we'll be in touch". Thanks in the snippet alone
   is not enough: it is also how a rejection whose snippet was cut short reads.

**What this cannot see.** Gmail's snippet is the first couple of hundred characters. A
rejection that spends them on thanks reads as `other` here (or as a confirmation, when its
subject line is "Thank you for applying"). The fixture set (`tests/fixtures/mail/`) has both
cases, labelled with what a person who read the whole message would say, so the measured
accuracy counts them as misses.

`explain()` also names the rule that decided, for the tests and for anyone tuning a pattern.
"""

from __future__ import annotations

import html
import re
from typing import Literal

__all__ = ["KINDS", "Kind", "classify", "clean", "explain"]

Kind = Literal["confirmation", "rejection", "interview", "other"]
KINDS: tuple[Kind, ...] = ("confirmation", "rejection", "interview", "other")

_I = re.IGNORECASE

# --- reading the text ----------------------------------------------------------------------------

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " "})
# Zero-width and invisible characters, which marketing mail pads its preview text with.
_INVISIBLE = re.compile("[­͏؜ᅟᅠ឴឵᠎​-‏‪-‮⁠-⁤﻿]")
_SPACE = re.compile(r"\s+")


def clean(text: str) -> str:
    """Text as the patterns read it: HTML entities decoded (Gmail's snippet is escaped), curly
    quotes straightened, invisible padding dropped, whitespace collapsed. Case is kept."""
    plain = _INVISIBLE.sub("", html.unescape(text or "")).translate(_QUOTES)
    return _SPACE.sub(" ", plain).strip()


# Where a clause ends: a phrase is small print only when its own clause makes it so.
_CLAUSE_END = re.compile(r"[.!?;:]")


def _clause_before(text: str, start: int) -> str:
    """The clause a match sits in, from where the clause begins to where the match does."""
    begin = 0
    for boundary in _CLAUSE_END.finditer(text, 0, start):
        begin = boundary.end()
    return text[begin:start]


def _stated(pattern: re.Pattern[str], text: str, *small_print: re.Pattern[str]) -> bool:
    """Whether `pattern` occurs somewhere it is said outright: not in a clause that one of the
    `small_print` patterns marks, from the clause's start to the end of the phrase itself."""
    for found in pattern.finditer(text):
        said = _clause_before(text, found.start()) + found.group()
        if not any(mark.search(said) for mark in small_print):
            return True
    return False


# --- 1. not an answer ----------------------------------------------------------------------------

# Subject lines of mail about jobs that no employer wrote in answer to an application.
_NOT_AN_ANSWER_SUBJECT = re.compile(
    r"\b(?:job\s+alerts?|jobs?\s+(?:for\s+you|you\s+(?:may|might)|matching|like\s+this|near\s+you)"
    r"|(?:new|more|recommended|top|similar|remote|latest)\s+(?:\w+\s+)?(?:jobs|roles|openings)"
    r"|\d+\+?\s+(?:new\s+)?(?:\w+\s+)?(?:jobs|roles|openings|positions)"
    r"|apply\s+(?:now|today|soon|before|by)|(?:is|are|we're|now)\s+hiring|hiring\s+now"
    r"|newsletter|digest|webinar|picked\s+for\s+you|picks\s+for\s+you|premium|upgrade"
    r"|(?:finish|complete|continue|resume|submit)\s+your\s+application"
    r"|your\s+(?:draft|saved|incomplete|unfinished)\s+application"
    r"|you\s+started\s+an?\s+application|don't\s+forget\s+to\s+apply|still\s+interested"
    r"|(?:verify|confirm|validate)\s+your\s+(?:email|e-mail|account|address)"
    r"|activate\s+your|reset\s+your\s+password|password\s+reset|verification\s+code"
    r"|security\s+code|one[\s-]time\s+(?:code|password|passcode)|new\s+sign[\s-]?in"
    r"|tips|how\s+to|guide\s+to|ways\s+to|salary\s+(?:guide|report)|unsubscribe"
    r"|survey|your\s+feedback|how\s+(?:was|did)|rate\s+your)\b",
    _I,
)
# The same, when only the snippet gives it away.
_NOT_AN_ANSWER_SNIPPET = re.compile(
    r"\b(?:unsubscribe|(?:view|see|browse|explore)\s+(?:all\s+|more\s+)?(?:jobs|roles|openings)"
    r"|job\s+alerts?|(?:more|similar|new|recommended)\s+jobs|jobs\s+like"
    r"|based\s+on\s+your\s+(?:profile|search|searches|preferences|activity)"
    r"|manage\s+(?:your\s+)?(?:alerts|preferences|notifications)"
    r"|(?:finish|complete|continue)\s+your\s+application|you\s+started\s+an?\s+application"
    r"|apply\s+(?:now|today)|verification\s+code|reset\s+your\s+password"
    r"|this\s+(?:week|month)(?=\s*:))\b",
    _I,
)

# --- 2. rejection --------------------------------------------------------------------------------

_NEGATED = r"(?:\bnot|n't|\bcannot|\bunable\s+to|\bno\s+longer)"
_REJECTION = re.compile(
    r"(?:"
    # "we will not be moving forward", "decided not to proceed", "unable to move ahead"
    + _NEGATED
    + r"\s+(?:to\s+)?(?:be\s+|going\s+to\s+(?:be\s+)?)?(?:able\s+to\s+)?"
    r"(?:mov(?:e|ing)\s+(?:you\s+)?(?:forward|ahead|on\b)|proceed(?:ing)?|progress(?:ing)?"
    r"|advanc(?:e|ing)|continu(?:e|ing)\s+with|pursu(?:e|ing)|tak(?:e|ing)\s+your\s+application)"
    # "move forward with other candidates", "pursue another applicant"
    r"|\b(?:with|pursu(?:e|ing)|select(?:ed)?|chosen?|hir(?:ed|ing)|prioriti[sz](?:e|ing)"
    r"|offer\s+to|(?:role|position|job)\s+to|on\s+to)\s+"
    r"(?:an?\s+|some\s+|the\s+)?(?:other|another|different)\s+(?:candidates?|applicants?)\b"
    r"|\b(?:candidates?|applicants?)\s+(?:whose|who|that|with)\s+(?:\w+\s+){0,6}?"
    r"(?:more\s+closely|better|best|closer|stronger)\b"
    r"|\bgo(?:ing|ne)?\s+(?:in\s+)?a(?:nother|\s+different)\s+direction\b"
    # "we regret to inform you", "sorry to let you know"
    r"|\bregret(?:fully)?\s+(?:to\s+)?(?:inform|tell|say|let|share|advise|announce)\b"
    r"|\bregrettably\b"
    r"|\bwe\s+regret\s+that\b"
    r"|\bsorry\s+to\s+(?:inform|tell|say|let\s+you\s+know|share|report)\b"
    r"|\bunfortunately\b[^.!?]{0,140}?(?:\b(?:other\s+candidates?|another\s+candidate"
    r"|declin\w+|unable\s+to\s+(?:offer|extend|accommodate|consider))\b"
    r"|(?:\bnot|n't)\s+(?:be\s+)?(?:able\s+to\s+)?(?:offer|extend|consider)\b)"
    # "we are unable to offer you a place", "we can't offer you an interview"
    r"|(?:\bnot|n't|\bunable\s+to)\s+(?:be\s+)?(?:able\s+to\s+)?offer\s+you\b"
    # A person, not a template: "we ended up hiring someone else", "we went with another"
    r"|\b(?:ended\s+up\s+hiring|went\s+with|gone\s+with|going\s+with|decided\s+on)\s+"
    r"(?:someone|somebody|another|a\s+different|an?\s+(?:internal|external)\s+candidate)\b"
    r"|\bhired\s+(?:someone|somebody)\b"
    # "the position has been filled", "the role is no longer available"
    r"|\b(?:position|role|job|opening|requisition|vacancy|search)"
    r"(?:\s+(?:of|for|as)\s+[^.!?,;:]{1,60}?)?\s+"
    r"(?:(?:has\s+(?:now\s+|since\s+|already\s+)?been|was|is\s+now)\s+"
    r"(?:filled|closed|cancell?ed|withdrawn|put\s+on\s+hold)"
    r"|is\s+no\s+longer\s+(?:available|open|active|accepting))\b"
    r"|\b(?:filled|clos(?:e|ed|ing)|cancell?ed)\s+(?:the|this|that)\s+"
    r"(?:position|role|job|opening|requisition|vacancy|search)\b"
    # "you were not selected", "your application was unsuccessful", "not the right fit"
    r"|(?:\bnot|n't)\s+(?:been\s+|be\s+)?(?:selected|shortlisted|chosen|successful"
    r"|offering|consider(?:ed|ing)\s+(?:for|further))\b"
    r"|\b(?:been|was|were|is|are|proved)\s+unsuccessful\b"
    r"|\bno\s+longer\s+(?:be(?:ing)?\s+)?(?:considered|under\s+consideration|in\s+consideration)\b"
    r"|(?:\bnot|n't)\s+(?:(?:think|believe|feel)\s+)?"
    r"(?:(?:it|this|that|you|we)\s+(?:is|are|'s|'re|would\s+be)\s+)?"
    r"(?:quite\s+)?(?:a|an|the)\s+(?:\w+\s+)?(?:fit|match)\b"
    r"|\bdeclin(?:e|ed|ing)\s+(?:to\s+move|your\s+application|your\s+candidacy)\b"
    r")",
    _I,
)
# Small print: the phrase is about a case ("if you do not hear…"), not about the reader.
_IF = re.compile(r"\b(?:if|should|unless|in\s+case|whether|until|in\s+the\s+event)\b", _I)
# Or about people in general: "applicants not selected will not be contacted".
_PEOPLE_IN_GENERAL = re.compile(
    r"\b(?:applicants|candidates|those|anyone|individuals)\s+"
    r"(?:(?:who|that)\s+(?:are|were|have)\s+)?(?:not|n't)\b",
    _I,
)

# --- 3. an offer ---------------------------------------------------------------------------------

_OFFER = re.compile(
    r"\b(?:offer\s+letter|job\s+offer|offer\s+of\s+employment|(?:formal|written|verbal)\s+offer"
    r"|extend(?:ing)?\s+(?:you\s+)?an?\s+(?:\w+\s+)?offer"
    r"|offer\s+you\s+(?:the|a)\s+(?:\w+\s+){0,3}?(?:position|role|job)"
    r"|your\s+offer\s+(?:from|at|with)|accept(?:ed|ing)?\s+(?:the|our|your)\s+offer)\b",
    _I,
)

# --- 4. interview --------------------------------------------------------------------------------

_MEETING = (
    r"(?:(?:phone|video|zoom|intro(?:ductory)?|initial|first|quick|brief|short|technical"
    r"|screening|recruiter|follow[\s-]?up|final|onsite|on[\s-]site|virtual"
    r"|\d+[\s-]?min(?:ute)?)\s+)*"
    r"(?:interviews?|calls?|chats?|conversations?|phone\s+screens?|screens?|screening"
    r"|meetings?|discussion)"
)
_INTERVIEW = re.compile(
    r"(?:"
    # "invite you to interview", "would like to schedule a call", "set up a time to chat"
    r"\binvit(?:e|ed|ing)\s+you\s+(?:to|for)\s+(?:an?\s+|our\s+|the\s+)?" + _MEETING + r"\b"
    r"|\binvit(?:e|ed|ing)\s+you\s+to\s+(?:interview|speak|meet|chat|talk)\b"
    r"|\b(?:schedul(?:e|ing)|set(?:ting)?\s+up|arrang(?:e|ing)|book(?:ing)?)\s+"
    r"(?:an?\s+|some\s+|your\s+|the\s+|our\s+)(?:time\s+(?:to|for)\s+(?:an?\s+)?)?"
    + _MEETING
    + r"\b"
    r"|\b(?:mov(?:e|ing)|advanc(?:e|ing)|progress(?:ing)?|invit(?:e|ing))\s+you\s+"
    r"(?:forward\s+|on\s+|ahead\s+|through\s+)?(?:to|into)\s+(?:the|our|a)\s+(?:\w+\s+){0,2}?"
    r"(?:round|stage|interview)\b"
    r"|\b(?:for|to)\s+(?:an?|the|our)\s+(?:\w+\s+)?(?:final|second|next|technical|onsite)\s+round\b"
    r"|\b(?:have|invite|bring|welcome)\s+you\s+back\b"
    r"|\b(?:find|grab|get|spare)\s+(?:a\s+few|some|\d+)\s+minutes\b"
    r"|\b(?:my|our)\s+calendar\b|\b(?:calendar|scheduling|booking)\s+link\b"
    r"|\b(?:pick|choose|select|book)\s+a\s+(?:slot|time)\b"
    r"|\b(?:schedul(?:e|ing)|set(?:ting)?\s+up|find(?:ing)?|book(?:ing)?|grab(?:bing)?)\s+"
    r"(?:(?:a|some)\s+)?time\s+(?:to|for|with)\b"
    r"|\b(?:interview|phone\s+screen)\s+(?:invitation|invite|request|confirmation|confirmed"
    r"|scheduled|scheduling|details|schedule|availability)\b"
    # "your video interview with Priya tomorrow", and a scheduling tool's "… has been scheduled"
    r"|\byour\s+(?:\w+\s+){0,2}?(?:interview|phone\s+screen)\s+"
    r"(?:with|on|at|tomorrow|today|is|this|next)\b"
    r"|\b" + _MEETING + r"\s+(?:has\s+been|is|was)\s+(?:scheduled|booked|confirmed)\b"
    # "bring you in for an onsite", "for an intro call with me and our CTO"
    r"|\bbring\s+you\s+(?:in|on[\s-]?site)\b|\b(?:for|to)\s+an?\s+on[\s-]?site\b"
    r"|\b(?:for|on|join|have)\s+an?\s+" + _MEETING + r"\s+with\s+(?:me|us|our|the\s+team)\b"
    r"|\bdo\s+you\s+have\s+(?:\w+\s+){0,3}?(?:time|minutes|a\s+moment)\b[^.!?]{0,80}?"
    r"\b(?:call|chat|talk|interview|conversation)\b"
    r"|\b(?:what|which)\s+(?:days?|times?|dates?)\s+(?:\w+\s+){0,8}?work\s+(?:for\s+you|best)\b"
    r"|\binvitation(?:\s+to|:)\s+(?:an?\s+)?(?:\w+\s+){0,3}?(?:interview|phone\s+screen)\b"
    r"|\b(?:like|love|want|wanted|hoping|happy|excited)\s+to\s+"
    r"(?:(?:schedule|set\s+up|arrange|book)\b|(?:chat|speak|talk|connect|meet)\s+with\s+you\b"
    r"|interview\s+you\b)"
    r"|\bmov(?:e|ing)\s+(?:you\s+)?(?:forward|ahead|on)\s+(?:to|with|in(?:to)?)\s+"
    r"(?:an?\s+|the\s+|our\s+)?(?:next\s+(?:step|stage|round)|" + _MEETING + r")\b"
    r"|\bnext\s+(?:steps?|stage|round)\b[^.!?]{0,90}?\b(?:interview|availab\w+|schedul\w+"
    r"|call|chat|speak|meet|phone\s+screen|assessment|work\s+sample|exercise|challenge"
    r"|take[\s-]?home|pairing)\b"
    # "what is your availability", "are you available for a call on Tuesday"
    r"|\b(?:share|send|provide|know|what(?:'s|\s+is))\s+(?:me\s+|us\s+)?your\s+availability\b"
    r"|\bavailability\s+(?:for|to|this|next|over|in\s+the\s+coming)\b"
    r"|\b(?:are|would)\s+you\s+(?:be\s+)?(?:available|free)\s+(?:for|to|on|at|this|next|some)\b"
    r"|\bwhen\s+(?:are|would)\s+you\s+(?:be\s+)?(?:available|free)\b"
    r"|\b(?:times?|slots?)\s+that\s+work\s+(?:for\s+you|best)\b"
    # An assessment is a step past the application, like a first interview.
    r"|\b(?:invit(?:e|ed|ing)\s+you\s+to|like\s+you\s+to)\s+(?:(?:complete|take|do|start)\s+)?"
    r"(?:an?\s+|the\s+|our\s+)?(?:\w+\s+){0,3}?"
    r"(?:assessment|challenge|test|exercise|assignment)\b"
    r"|\b(?:take[\s-]?home|coding|technical|online|skills?)\s+"
    r"(?:assessment|challenge|test|exercise|assignment)\b"
    r")",
    _I,
)
# A subject line that is an invitation by itself: "Northwind interview", "Phone screen at …".
# ("Interview with …" and "Invitation: …" are phrases of `_INTERVIEW`.) Not any subject with
# the word in it: a friend writes "Re: interview outfit??" too.
_INTERVIEW_SUBJECT = re.compile(
    r"^(?:(?:re|fwd?)\s*:\s*)*interview\s+with\b"
    r"|\b(?:interview|(?:phone|recruiter|technical|hiring\s+manager)\s+screen"
    r"|(?:intro(?:ductory)?|recruiter|screening)\s+call"
    r"|(?:next|final|second|technical|onsite)\s+round)(?:\s*[!.]?$|\s+(?:at|for|with)\b)",
    _I,
)
# A promise of a later invitation, or small print: "we will reach out to schedule an interview".
_LATER = re.compile(
    r"\b(?:if|should|unless|in\s+case|whether|once|will|shall|may|might)\b|'ll\b"
    r"|\b(?:selected|shortlisted|successful|qualified)\s+(?:candidates|applicants)\b"
    r"|\b(?:candidates|applicants|recruiters|employers|companies)\s+(?:who|that|we)\b",
    _I,
)
# The note after an interview is not an invitation to one.
_AFTER_INTERVIEW = re.compile(
    r"\b(?:thank(?:s|\s+you)(?:\s+\w+){0,3}?\s+for\s+(?:(?:taking|making)\s+the\s+time\s+to\s+"
    r"(?:interview|speak|meet|chat|talk)|interviewing|speaking\s+with|meeting\s+with"
    r"|your\s+interview|your\s+time\s+(?:today|yesterday|this|last|on))"
    r"|(?:after|following|regarding|about|since)\s+(?:your|our|the)\s+(?:\w+\s+){0,2}?"
    r"(?:interviews?|conversations?|calls?|phone\s+screen)"
    r"|(?:enjoyed|pleasure)\s+(?:\w+\s+){0,2}?(?:meeting|speaking|talking|chatting)"
    r"|interview\s+(?:experience|feedback|survey)"
    r"|(?:your|the)\s+interview\s+(?:with\s+\S+(?:\s+\S+)?\s+)?(?:went|was))\b",
    _I,
)

# --- 5. a soft rejection -------------------------------------------------------------------------

_SOFT_REJECTION = re.compile(
    r"\bafter\s+(?:(?:much|very)\s+)?(?:(?:careful|thorough|thoughtful|much|long)\s+"
    r"(?:consideration|review|deliberation|thought|evaluation)"
    r"|(?:carefully|thoroughly|thoughtfully)\s+(?:reviewing|considering|evaluating))\b",
    _I,
)

# --- 6. confirmation -----------------------------------------------------------------------------

_THING = r"(?:application|resume|résumé|cv|submission|candidacy|materials)"
_RECEIPT = re.compile(
    r"(?:"
    r"\b(?:received|receiving|got)\s+your\s+(?:\w+\s+){0,2}?" + _THING + r"\b"
    r"|\bwe(?:'ve|\s+have)\s+(?:got\s+)?your\s+(?:\w+\s+){0,2}?" + _THING + r"\b"
    r"|\b" + _THING + r"\s+(?:(?:for|to)\s+(?:\w+\s+){1,8}?)?"
    r"(?:has\s+been|was|is|were|have\s+been)\s+"
    r"(?:successfully\s+)?(?:received|submitted|sent|recorded|delivered)\b"
    r"|\b" + _THING + r"\s+(?:(?:for|to)\s+(?:\w+\s+){1,8}?)?(?:has\s+|have\s+)?"
    r"(?:arrived|reached\s+us|come\s+through|landed)\b"
    r"|\bapplication\s+is\s+(?:now\s+)?with\s+(?:our|the)\b"
    r"|\b(?:reception|receipt)\s+of\s+your\s+" + _THING + r"\b"
    r"|\b(?:application|submission)\s+(?:received|confirmation|confirmed|submitted)\b"
    r"|\bsuccessfully\s+(?:applied|submitted)\b"
    r"|\byou(?:'ve|\s+have)\s+(?:successfully\s+)?applied\s+(?:to|for)\b"
    r"|\bconfirm(?:ing|s)?\s+(?:that\s+)?(?:we\s+(?:have\s+|'ve\s+)?received|receipt)\b"
    r"|\backnowledg(?:e|es|ing)\s+(?:the\s+)?receipt\b"
    r"|\byour\s+application\s+is\s+(?:now\s+|currently\s+)?(?:under|in|being)\s+review(?:ed)?\b"
    r")",
    _I,
)
# "We will review your application" says it arrived; "after reviewing your application" is the
# start of a decision, and which one is not visible yet.
_REVIEWING = re.compile(
    r"\breview(?:ing)?\s+(?:your\s+(?:\w+\s+){0,2}?"
    r"(?:application|resume|résumé|cv|submission|materials|qualifications|background)"
    r"|(?:(?:all|every|each)\s+)?applications?)\b",
    _I,
)
_LOOKING_BACK = re.compile(r"\b(?:after|upon|having|following|since)\b", _I)
# "Recruiters will review your resume live" reviews a resume and is about no application.
_ABOUT_AN_APPLICATION = re.compile(r"\bappl(?:y|ied|ying|ication|ications)\b", _I)
_THANKS = re.compile(
    r"\bthank(?:s|\s+you)(?:\s+(?:so|very)\s+much)?(?:\s+again)?\s+for\s+"
    r"(?:applying|your\s+application|submitting\s+your\s+application|your\s+recent\s+application"
    r"|your\s+interest\s+in\s+(?:joining|working|a\s+career))\b",
    _I,
)
_WILL_FOLLOW_UP = re.compile(
    r"\b(?:be\s+in\s+(?:touch|contact)|get\s+back\s+to\s+(?:you|candidates|applicants|everyone)"
    r"|respond\s+(?:to|within)|hear\s+(?:back\s+)?from\s+(?:us|our)"
    r"|reach\s+out|contact\s+you|follow\s+up|let\s+you\s+know|in\s+good\s+hands"
    r"|(?:will|'ll|going\s+to)\s+(?:\w+\s+){0,2}?(?:review|consider|look|read|evaluate|assess))\b",
    _I,
)


def explain(subject: str, snippet: str) -> tuple[Kind, str]:
    """The kind, and the name of the rule that decided it."""
    subject, snippet = clean(subject), clean(snippet)
    both = f"{subject}. {snippet}"

    if _NOT_AN_ANSWER_SUBJECT.search(subject) or _NOT_AN_ANSWER_SNIPPET.search(snippet):
        return "other", "not-an-answer"
    if _stated(_REJECTION, both, _IF, _PEOPLE_IN_GENERAL):
        return "rejection", "rejection"
    if _OFFER.search(both):
        return "other", "offer"
    if not _AFTER_INTERVIEW.search(both):
        if _stated(_INTERVIEW, both, _LATER):
            return "interview", "invitation"
        if _INTERVIEW_SUBJECT.search(subject):
            return "interview", "subject"
    if _SOFT_REJECTION.search(both):
        return "rejection", "soft-rejection"
    if _RECEIPT.search(both):
        return "confirmation", "receipt"
    if _ABOUT_AN_APPLICATION.search(both) and _stated(_REVIEWING, both, _LOOKING_BACK):
        return "confirmation", "under-review"
    if _THANKS.search(subject):
        return "confirmation", "thanks-subject"
    if _THANKS.search(snippet) and _WILL_FOLLOW_UP.search(snippet):
        return "confirmation", "thanks-and-follow-up"
    return "other", "nothing"


def classify(subject: str, snippet: str) -> Kind:
    """`confirmation`, `rejection`, `interview` or `other` (see the module docstring)."""
    return explain(subject, snippet)[0]
