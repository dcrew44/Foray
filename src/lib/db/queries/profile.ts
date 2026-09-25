import { and, desc, eq } from "drizzle-orm";

import { db } from "@/lib/db";
import { documents, profiles, resumes } from "@/lib/db/schema";
import { ResumeSchema, type Resume } from "@/lib/resume/schema";

/**
 * Profile, document and resume reads/writes. `userId` first, always.
 */

export async function getProfile(userId: string) {
  const [row] = await db
    .select()
    .from(profiles)
    .where(eq(profiles.userId, userId))
    .limit(1);
  return row ?? null;
}

export async function getBaseResume(userId: string) {
  const [row] = await db
    .select()
    .from(resumes)
    .where(and(eq(resumes.userId, userId), eq(resumes.kind, "base")))
    .orderBy(desc(resumes.version))
    .limit(1);
  return row ?? null;
}

export async function getDocument(userId: string, id: string) {
  const [row] = await db
    .select()
    .from(documents)
    .where(and(eq(documents.id, id), eq(documents.userId, userId)))
    .limit(1);
  return row ?? null;
}

/**
 * Records an uploaded file. The same bytes uploaded twice are the same
 * document, so a re-upload returns the existing row rather than a duplicate.
 */
export async function recordDocument(input: {
  userId: string;
  kind: (typeof documents.$inferInsert)["kind"];
  filename: string;
  mime: string;
  sizeBytes: number;
  storagePath: string;
  sha256: string;
  textExtracted?: string | null;
}) {
  const [row] = await db
    .insert(documents)
    .values(input)
    .onConflictDoUpdate({
      target: [documents.userId, documents.sha256],
      set: { textExtracted: input.textExtracted ?? null },
    })
    .returning();
  return row;
}

/**
 * Stores a newly extracted resume as the user's base resume and mirrors it
 * into the profile.
 *
 * Base resumes are versioned rather than overwritten: a re-upload should never
 * destroy the resume a tailored version was derived from, and `changes[]` on a
 * tailored resume cites fact ids that have to keep resolving.
 */
export async function saveBaseResume(input: {
  userId: string;
  resume: Resume;
  documentId?: string | null;
}) {
  const previous = await getBaseResume(input.userId);

  const [row] = await db
    .insert(resumes)
    .values({
      userId: input.userId,
      kind: "base",
      version: (previous?.version ?? 0) + 1,
      data: input.resume,
    })
    .returning();

  await db
    .insert(profiles)
    .values({
      userId: input.userId,
      data: input.resume,
    })
    .onConflictDoUpdate({
      target: profiles.userId,
      set: { data: input.resume, updatedAt: new Date() },
    });

  return row;
}

/**
 * The user's current base resume, parsed — the one set of facts scoring,
 * evaluation, tailoring and the chat agent all read.
 *
 * There used to be two: the editor wrote `profiles.data` while everything
 * downstream read `resumes(base)`, so a correction made in the editor was
 * silently ignored by every model that judged the user. Edits now land here as
 * a new version, and `profiles.data` is only a mirror of the latest one.
 */
export async function loadBaseResume(userId: string): Promise<{
  id: string;
  version: number;
  resume: Resume;
} | null> {
  const row = await getBaseResume(userId);
  if (!row) return null;
  const parsed = ResumeSchema.safeParse(row.data);
  if (!parsed.success) return null;
  return { id: row.id, version: row.version, resume: parsed.data };
}

/**
 * Saves the editor's resume as a new base version, unless nothing changed.
 * Versioned rather than overwritten so a tailored resume's citations keep
 * resolving against the version it was built from.
 */
export async function saveEditedResume(userId: string, resume: Resume) {
  const current = await getBaseResume(userId);
  if (current && sameJson(current.data, resume)) return current;
  return saveBaseResume({ userId, resume });
}

function sameJson(a: unknown, b: unknown): boolean {
  const stable = (value: unknown): string => {
    if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
    if (value && typeof value === "object") {
      return `{${Object.entries(value as Record<string, unknown>)
        .filter(([, v]) => v !== undefined && v !== null)
        .sort(([x], [y]) => (x < y ? -1 : x > y ? 1 : 0))
        .map(([k, v]) => `${JSON.stringify(k)}:${stable(v)}`)
        .join(",")}}`;
    }
    return JSON.stringify(value ?? null);
  };
  return stable(a) === stable(b);
}
