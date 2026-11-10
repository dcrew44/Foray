import type { components } from "@/lib/api/schema";
import { plural, STAGE_LABEL, timeAgo } from "@/lib/format";

/**
 * How the mail Foray found is put into words: on a match page, where an employer's email
 * suggests a stage, and in Settings, where the connected mailbox says what was found. No
 * directive: the match page (a server component) and both panels (client components) read
 * these.
 */

type S = components["schemas"];
export type MailSuggestion = S["MailSuggestion"];
export type MailFound = S["MailFound"];
export type MailWaiting = S["MailWaiting"];

/** What an email is called in a sentence: "looks like a rejection". */
export const SUGGESTION_NOUN: Record<MailSuggestion["kind"], string> = {
  rejection: "a rejection",
  interview: "an interview invitation",
};

/** Where the documentation for the owner lives. */
export const GMAIL_DOC = "https://github.com/dcrew44/Foray/blob/main/docs/gmail.md";

/** A subject line in quotes, or "with no subject" when it had none. */
function quoted(subject: string): string {
  return subject.trim() ? `“${subject.trim()}”` : "with no subject";
}

/**
 * The question a suggestion puts to the user: `An email from Stripe Recruiting, 2d ago, looks
 * like a rejection: “Your application to Stripe”. Move this match to Rejected?`
 */
export function suggestionQuestion(suggestion: MailSuggestion, now = Date.now()): string {
  const what = SUGGESTION_NOUN[suggestion.kind];
  const when = timeAgo(suggestion.receivedAt, now);
  const subject = suggestion.subject.trim() ? `: ${quoted(suggestion.subject)}` : "";
  return (
    `An email from ${suggestion.sender}, ${when}, looks like ${what}${subject}. ` +
    `Move this match to ${STAGE_LABEL[suggestion.stage]}?`
  );
}

/** `The employer confirmed your application arrived, 3d ago: “Thank you for applying”.` */
export function confirmationLine(
  confirmedAt: string,
  subject: string | null,
  now = Date.now(),
): string {
  const said = subject?.trim() ? `: ${quoted(subject)}` : "";
  return `The employer confirmed your application arrived, ${timeAgo(confirmedAt, now)}${said}.`;
}

/**
 * What a mailbox's checks have found so far, as one sentence: `2 confirmations, 1 rejection
 * and 1 interview invitation, 3 of them matched to your applications.` Null when nothing
 * that looks like an employer's answer has been found.
 */
export function foundSummary(found: MailFound): string | null {
  const parts = [
    found.confirmations > 0 ? plural(found.confirmations, "confirmation") : null,
    found.rejections > 0 ? plural(found.rejections, "rejection") : null,
    found.interviews > 0 ? plural(found.interviews, "interview invitation") : null,
  ].filter((part): part is string => part !== null);
  if (parts.length === 0) return null;
  const total = found.confirmations + found.rejections + found.interviews;
  const list =
    parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}`;
  const matched =
    found.matched === 0
      ? "none matched to an application yet"
      : total === 1
        ? "matched to one of your applications"
        : found.matched === total
          ? "all matched to your applications"
          : `${found.matched} of them matched to your applications`;
  return `${list}, ${matched}.`;
}

/** A waiting suggestion as a link's words: "A rejection for Backend Engineer at Stripe". */
export function waitingLabel(waiting: MailWaiting): string {
  const what = SUGGESTION_NOUN[waiting.kind];
  return `${what[0].toUpperCase()}${what.slice(1)} for ${waiting.title} at ${waiting.company}`;
}
