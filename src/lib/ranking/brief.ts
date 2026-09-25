import { z } from "zod";

import {
  RemotePreference,
  Seniority,
  type Preferences,
} from "@/lib/profile/interview";

/**
 * What one search is for.
 *
 * Saved preferences say what someone generally wants; a request often asks for
 * something narrower or different: "entry-level roles at Anthropic", "remote
 * data jobs in fintech". The brief holds only what the request itself states.
 * A null field means "as saved". It overrides the preferences for that one run
 * and never changes them.
 *
 * Before the brief existed, a search read nothing but the saved preferences:
 * the request was kept "for the record", so every search returned the same
 * roles whatever was asked.
 */

export const SearchBrief = z.object({
  /** Kinds of role asked for, as short titles ("software engineer"). */
  roles: z.array(z.string()).nullable(),
  seniority: Seniority.nullable(),
  locations: z.array(z.string()).nullable(),
  remote: RemotePreference.nullable(),
  /** Companies the search is limited to, as the user named them. */
  companies: z.array(z.string()).nullable(),
  /** Anything else that should shape the ranking, in one sentence. */
  focus: z.string().nullable(),
});

export type SearchBrief = z.infer<typeof SearchBrief>;

export const EMPTY_BRIEF: SearchBrief = {
  roles: null,
  seniority: null,
  locations: null,
  remote: null,
  companies: null,
  focus: null,
};

/** What a search started without a request (the Home button) records as its prompt. */
export const SAVED_PREFERENCES_PROMPT = "Search with my saved preferences";

/** The request worth reading, or null when there is none beyond the saved preferences. */
export function requestText(prompt: string | null | undefined): string | null {
  const text = prompt?.trim();
  return text && text !== SAVED_PREFERENCES_PROMPT ? text : null;
}

/** A stored brief, read leniently: anything unreadable means "as saved". */
export function parseBrief(value: unknown): SearchBrief {
  const parsed = SearchBrief.partial().safeParse(value ?? {});
  return parsed.success ? cleanBrief(parsed.data) : EMPTY_BRIEF;
}

/**
 * Trims strings, drops blanks and duplicates, and turns an empty list into
 * null, so "no companies" and "companies: []" mean the same thing.
 */
export function cleanBrief(brief: Partial<SearchBrief>): SearchBrief {
  const list = (values: string[] | null | undefined) => {
    const seen = new Set<string>();
    const kept: string[] = [];
    for (const value of values ?? []) {
      const text = value.trim();
      if (!text || seen.has(text.toLowerCase())) continue;
      seen.add(text.toLowerCase());
      kept.push(text);
    }
    return kept.length ? kept : null;
  };

  return {
    roles: list(brief.roles),
    seniority: brief.seniority ?? null,
    locations: list(brief.locations),
    remote: brief.remote ?? null,
    companies: list(brief.companies),
    focus: brief.focus?.trim() || null,
  };
}

/**
 * The derived brief with the chat agent's structured hints laid over it, field
 * by field. The hints come from a model that saw the whole conversation, so
 * where both say something, the hint wins.
 */
export function mergeBrief(
  derived: Partial<SearchBrief>,
  hints: Partial<SearchBrief> | null | undefined,
): SearchBrief {
  const base = cleanBrief(derived);
  const over = cleanBrief(hints ?? {});
  return {
    roles: over.roles ?? base.roles,
    seniority: over.seniority ?? base.seniority,
    locations: over.locations ?? base.locations,
    remote: over.remote ?? base.remote,
    companies: over.companies ?? base.companies,
    focus: over.focus ?? base.focus,
  };
}

export function isEmptyBrief(brief: SearchBrief): boolean {
  return Object.values(brief).every((value) => value == null);
}

/** The preferences this search runs on: the brief where it says something, the saved ones elsewhere. */
export function applyBrief(
  preferences: Preferences,
  brief: SearchBrief,
): Preferences {
  return {
    ...preferences,
    target_roles: brief.roles ?? preferences.target_roles,
    seniority: brief.seniority ?? preferences.seniority,
    locations: brief.locations ?? preferences.locations,
    remote_pref: brief.remote ?? preferences.remote_pref,
  };
}

const LEVEL: Record<z.infer<typeof Seniority>, string> = {
  intern: "Internship",
  new_grad: "New-grad",
  entry: "Entry-level",
  mid: "Mid-level",
  senior: "Senior",
  staff_plus: "Staff-level and above",
};

const REMOTE: Record<z.infer<typeof RemotePreference>, string | null> = {
  remote: "remote only",
  hybrid: "hybrid",
  onsite: "on site",
  any: null,
};

/**
 * The brief in one line, for the run card and the activity feed:
 * "Entry-level software engineer roles at Anthropic, in San Francisco Bay Area".
 * Null when the brief adds nothing to the saved preferences.
 */
export function describeBrief(brief: SearchBrief): string | null {
  const { roles, seniority, locations, remote, companies } = brief;
  if (!roles && !seniority && !locations && !remote && !companies) return null;

  const what = roles ? `${joinOr(roles)} roles` : "roles";
  let text = seniority
    ? `${LEVEL[seniority]} ${what}`
    : what.charAt(0).toUpperCase() + what.slice(1);

  if (companies) text += ` at ${joinOr(companies)}`;
  if (locations) text += `, in ${joinOr(locations)}`;
  const mode = remote ? REMOTE[remote] : null;
  if (mode) text += `, ${mode}`;
  return text;
}

function joinOr(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  if (items.length === 2) return `${items[0]} or ${items[1]}`;
  return `${items.slice(0, -1).join(", ")} or ${items.at(-1)}`;
}
