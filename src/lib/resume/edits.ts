import type { Resume } from "./schema";

/**
 * Marks what the user actually changed as theirs.
 *
 * Provenance is only useful if it is true. Stamping every item `user` whenever
 * the editor saves would make a later LinkedIn merge refuse to touch anything
 * (Step 9 never overwrites `source: user`), and stamping nothing would let a
 * merge silently undo a correction. So each item is compared against what was
 * stored and re-sourced only if its content actually differs.
 */

type Item = { id: string; source: string } & Record<string, unknown>;

/**
 * Order-independent serialization.
 *
 * The stored side of the comparison comes back from a jsonb column, which does
 * not preserve key order, while the incoming side has just been through
 * `ResumeSchema.parse` and is in schema order. A plain JSON.stringify would
 * therefore report every item as changed and stamp the whole resume `user`,
 * quietly destroying the provenance this function exists to protect.
 */
function stable(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(stable).join(",")}]`;
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      // `undefined` and a missing key mean the same thing here; a jsonb
      // round-trip turns one into the other.
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${stable(v)}`).join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

/** Content equality, ignoring the provenance fields themselves. */
function sameContent(a: Item, b: Item): boolean {
  const strip = (item: Item) => {
    const copy: Record<string, unknown> = { ...item };
    delete copy.id;
    delete copy.source;
    return copy;
  };
  return stable(strip(a)) === stable(strip(b));
}

function stampList<T extends Item>(next: T[], previous: T[]): T[] {
  const byId = new Map(previous.map((item) => [item.id, item]));

  return next.map((item) => {
    const before = byId.get(item.id);
    // A new item the user added, or one whose content they changed.
    if (!before || !sameContent(item, before)) {
      return { ...item, source: "user" as const };
    }
    return { ...item, source: before.source };
  });
}

/**
 * Returns `next` with `source: "user"` on every item the user added or edited,
 * and the original provenance preserved on everything they left alone.
 */
export function stampUserEdits(next: Resume, previous: Resume | null): Resume {
  if (!previous) return next;

  return {
    ...next,
    basics: {
      ...next.basics,
      links: stampList(next.basics.links, previous.basics.links),
    },
    work: stampList(next.work, previous.work),
    education: stampList(next.education, previous.education),
    projects: stampList(next.projects, previous.projects),
    skills: stampList(next.skills, previous.skills),
  };
}
