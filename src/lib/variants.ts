import type { components } from "@/lib/api/schema";

/**
 * Resume variants, as the Profile page edits them (#61).
 *
 * A variant stores no fact: its spec selects, orders and rewords the base resume's items by
 * their ids, and Django composes the resume from the newest base. So the editor's draft is the
 * same thing in a shape that is easy to edit: every role and project of the base is a row,
 * chosen or not, and a row's bullets are `null` until the variant words them itself.
 *
 * These helpers are pure. They mirror `clean_spec` in `backend/apps/resumes/variants.py`, so
 * what the editor calls unchanged is what Django would store unchanged.
 */

export type VariantSpec = components["schemas"]["VariantSpec"];
export type VariantItem = components["schemas"]["VariantItem"];

/** What the helpers read of the base resume: the editor's resume fits. */
export type VariantBase = {
  work: Array<{ id: string; highlights: string[] }>;
  projects: Array<{ id: string; highlights: string[] }>;
  education: Array<{ id: string }>;
  skills: Array<{ id: string }>;
};

/** One role or project of the base. `highlights: null` means the base's own bullets. */
export type VariantRow = { id: string; included: boolean; highlights: string[] | null };

export type VariantDraft = {
  headline: string | null;
  summary: string | null;
  work: VariantRow[];
  projects: VariantRow[];
  /** The ids chosen; they are shown, and saved, in the base's order. */
  education: string[];
  skills: string[];
};

/** A spec with every list present, as it is sent to Django. */
export type FullVariantSpec = {
  headline: string | null;
  summary: string | null;
  work: Array<{ id: string; highlights: string[] | null }>;
  projects: Array<{ id: string; highlights: string[] | null }>;
  education: string[];
  skills: string[];
};

/**
 * The rows for one list of the base: the draft's rows that the base still has, in the draft's
 * order, then every base item the draft does not mention, unchosen. A role added to the resume
 * therefore shows up unchosen at the end, and one removed from it disappears.
 */
export function rowsFor(items: Array<{ id: string }>, rows: VariantRow[]): VariantRow[] {
  const known = new Set(items.map((item) => item.id));
  const seen = new Set<string>();
  const kept: VariantRow[] = [];
  for (const row of rows) {
    if (!known.has(row.id) || seen.has(row.id)) continue;
    seen.add(row.id);
    kept.push(row);
  }
  const rest = items
    .filter((item) => !seen.has(item.id))
    .map((item) => ({ id: item.id, included: false, highlights: null }));
  return [...kept, ...rest];
}

/** The draft the editor starts from: the stored spec laid over the base. */
export function toDraft(base: VariantBase, spec: VariantSpec): VariantDraft {
  const chosen = (entries: VariantItem[] | undefined): VariantRow[] =>
    (entries ?? []).map((entry) => ({
      id: entry.id,
      included: true,
      highlights: entry.highlights ?? null,
    }));
  const present = (items: Array<{ id: string }>, ids: string[] | undefined) => {
    const wanted = new Set(ids ?? []);
    return items.filter((item) => wanted.has(item.id)).map((item) => item.id);
  };
  return {
    headline: spec.headline ?? null,
    summary: spec.summary ?? null,
    work: rowsFor(base.work, chosen(spec.work)),
    projects: rowsFor(base.projects, chosen(spec.projects)),
    education: present(base.education, spec.education),
    skills: present(base.skills, spec.skills),
  };
}

function text(value: string | null): string | null {
  const trimmed = (value ?? "").trim();
  return trimmed === "" ? null : trimmed;
}

function sameLines(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((line, index) => line === b[index]);
}

/**
 * The spec a draft saves as: chosen rows in their order, empty text as `null`, and bullets that
 * equal the base's as `null`, so a role nobody reworded keeps following the base.
 */
export function toSpec(base: VariantBase, draft: VariantDraft): FullVariantSpec {
  const items = (
    baseItems: Array<{ id: string; highlights: string[] }>,
    rows: VariantRow[],
  ): FullVariantSpec["work"] => {
    const byId = new Map(baseItems.map((item) => [item.id, item]));
    return rowsFor(baseItems, rows)
      .filter((row) => row.included)
      .map((row) => {
        const own =
          row.highlights === null
            ? null
            : row.highlights.map((line) => line.trim()).filter((line) => line !== "");
        const theirs = byId.get(row.id)?.highlights ?? [];
        return { id: row.id, highlights: own === null || sameLines(own, theirs) ? null : own };
      });
  };
  const inBaseOrder = (baseItems: Array<{ id: string }>, ids: string[]) => {
    const wanted = new Set(ids);
    return baseItems.filter((item) => wanted.has(item.id)).map((item) => item.id);
  };
  return {
    headline: text(draft.headline),
    summary: text(draft.summary),
    work: items(base.work, draft.work),
    projects: items(base.projects, draft.projects),
    education: inBaseOrder(base.education, draft.education),
    skills: inBaseOrder(base.skills, draft.skills),
  };
}

/**
 * Moves a chosen row past the nearest chosen row above (-1) or below (+1) it. Unchosen rows
 * keep their places: they are not on the resume, so moving past one would change nothing.
 */
export function moveRow(rows: VariantRow[], id: string, direction: -1 | 1): VariantRow[] {
  const from = rows.findIndex((row) => row.id === id);
  if (from === -1 || !rows[from]!.included) return rows;
  let to = from + direction;
  while (to >= 0 && to < rows.length && !rows[to]!.included) to += direction;
  if (to < 0 || to >= rows.length) return rows;
  const next = [...rows];
  [next[from], next[to]] = [next[to]!, next[from]!];
  return next;
}

/** Whether a chosen row can move that way: there is another chosen row there to pass. */
export function canMove(rows: VariantRow[], id: string, direction: -1 | 1): boolean {
  return moveRow(rows, id, direction) !== rows;
}

export type SelectionCounts = { work: number; projects: number; education: number; skills: number };

/** How many of the base's items a spec shows. Ids the base no longer has do not count. */
export function selectionCounts(base: VariantBase, spec: VariantSpec): SelectionCounts {
  const shown = toSpec(base, toDraft(base, spec));
  return {
    work: shown.work.length,
    projects: shown.projects.length,
    education: shown.education.length,
    skills: shown.skills.length,
  };
}

/**
 * A variant that names nothing the resume still has: after a new upload every item has a new
 * id, so the variants made from the old file select nothing. Django never sends such a variant.
 */
export function selectsNothing(counts: SelectionCounts): boolean {
  return counts.work + counts.projects + counts.education + counts.skills === 0;
}

/** "backend, api" typed as one tag is two keywords; each is kept once, whatever its capitals. */
export function splitKeywords(values: string[]): string[] {
  const seen = new Set<string>();
  const keywords: string[] = [];
  for (const value of values) {
    for (const part of value.split(",")) {
      const keyword = part.trim().replace(/\s+/g, " ");
      const key = keyword.toLowerCase();
      if (keyword === "" || seen.has(key)) continue;
      seen.add(key);
      keywords.push(keyword);
    }
  }
  return keywords;
}
