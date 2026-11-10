/**
 * Small rules about contacts that both a page and a client component need (#63). Pure functions
 * only: safe to import from anywhere.
 */

import { resumeTooLarge } from "./uploads";

/** Django's sentence for a file that is not a list of people (`apps/contacts/parse.py`). */
export const NOT_A_CONTACT_LIST =
  "Upload a CSV file: LinkedIn's Connections.csv, or any list with a name column.";

const LIST_FILE = /\.(csv|tsv|txt|zip)$/i;

/**
 * Why a file cannot be imported, checked in the browser before anything is sent, or null when it
 * can be tried: Django reads the bytes and has the last word. The size limit is the upload
 * limit every file shares (`src/lib/uploads.ts`).
 */
export function contactsFileProblem(name: string, size: number): string | null {
  if (!LIST_FILE.test(name)) return NOT_A_CONTACT_LIST;
  return resumeTooLarge(size);
}

/** The people a match's row names, and how many there are in all (`MatchKnows`). */
type Knows = { count: number; people: Array<{ name: string }> };

/**
 * Who the user knows at a posting's company, in the few words a row has room for: "You know
 * Priya Rao", "You know Priya Rao and Marcus Bell", "You know Priya Rao and 4 others". Null
 * when they know nobody there, so a row for such a posting shows nothing at all.
 */
export function knowsLabel(knows: Knows | null | undefined): string | null {
  if (!knows || knows.count < 1) return null;
  const [first, second] = knows.people;
  if (!first) return knows.count === 1 ? "You know 1 person here" : `You know ${knows.count} people here`;
  if (knows.count === 1) return `You know ${first.name}`;
  if (knows.count === 2 && second) return `You know ${first.name} and ${second.name}`;
  const others = knows.count - 1;
  return `You know ${first.name} and ${others} ${others === 1 ? "other" : "others"}`;
}

/** How long a note to a contact may be once the user has edited it (`outreach.BODY_MAX`). */
export const NOTE_MAX_LENGTH = 5000;

/**
 * What to call a contact in a button or a toast: the first word of their name as the file wrote
 * it, or "them" when there is none.
 */
export function firstName(name: string | null | undefined): string {
  return (name ?? "").trim().split(/\s+/)[0] || "them";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/**
 * "Connected Mar 2024" from the day a file says two people connected (`2024-03-15`). The day is
 * a calendar date, not an instant, so it is read from the string and never through a time zone.
 */
export function connectedLabel(value: string | null | undefined): string | null {
  const found = /^(\d{4})-(\d{2})-\d{2}$/.exec(value ?? "");
  if (!found) return null;
  const month = MONTHS[Number(found[2]) - 1];
  return month ? `Connected ${month} ${found[1]}` : null;
}
