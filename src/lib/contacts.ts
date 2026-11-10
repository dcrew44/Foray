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
