import { and, desc, eq } from "drizzle-orm";

import { db } from "@/lib/db";
import { documents, profiles, resumes } from "@/lib/db/schema";
import type { Resume } from "@/lib/resume/schema";

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

/** Replaces the profile's resume data — used by the editor. */
export async function updateProfileData(userId: string, data: Resume) {
  const [row] = await db
    .update(profiles)
    .set({ data, updatedAt: new Date() })
    .where(eq(profiles.userId, userId))
    .returning();
  return row ?? null;
}
