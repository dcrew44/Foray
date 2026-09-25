import { and, desc, eq, isNotNull, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import {
  documents,
  jobMatches,
  jobs,
  resumes,
  type Document,
  type Resume as ResumeRow,
} from "@/lib/db/schema";
import type { TailorChange } from "@/lib/resume/tailor";
import type { Resume } from "@/lib/resume/schema";
import type { Verification } from "@/lib/resume/verify";

/**
 * Resume rows — base versions and tailored ones — and the PDFs rendered from
 * them. `userId` first, always: a resume id from the URL must never read
 * another user's resume.
 */

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function getResume(
  userId: string,
  id: string,
): Promise<ResumeRow | null> {
  if (!UUID.test(id)) return null;
  const [row] = await db
    .select()
    .from(resumes)
    .where(and(eq(resumes.id, id), eq(resumes.userId, userId)))
    .limit(1);
  return row ?? null;
}

/** The newest tailored version for a match. */
export async function getTailoredForMatch(
  userId: string,
  matchId: string,
): Promise<ResumeRow | null> {
  if (!UUID.test(matchId)) return null;
  const [row] = await db
    .select()
    .from(resumes)
    .where(
      and(
        eq(resumes.userId, userId),
        eq(resumes.kind, "tailored"),
        eq(resumes.jobMatchId, matchId),
      ),
    )
    .orderBy(desc(resumes.version), desc(resumes.createdAt))
    .limit(1);
  return row ?? null;
}

export type TailoredSummary = {
  id: string;
  matchId: string;
  jobId: string;
  title: string;
  company: string;
  version: number;
  renderedDocumentId: string | null;
  passed: boolean | null;
  unsupported: number;
  changes: number;
  createdAt: Date;
};

/** Tailored resumes, newest first, with the job each was written for. */
export async function listTailored(
  userId: string,
  limit = 20,
): Promise<TailoredSummary[]> {
  const rows = await db
    .select({
      id: resumes.id,
      matchId: jobMatches.id,
      jobId: jobs.id,
      title: jobs.title,
      company: jobs.companyName,
      version: resumes.version,
      renderedDocumentId: resumes.renderedDocumentId,
      verification: resumes.verification,
      changes: resumes.changes,
      createdAt: resumes.createdAt,
    })
    .from(resumes)
    .innerJoin(jobMatches, eq(jobMatches.id, resumes.jobMatchId))
    .innerJoin(jobs, eq(jobs.id, jobMatches.jobId))
    .where(and(eq(resumes.userId, userId), eq(resumes.kind, "tailored")))
    .orderBy(desc(resumes.createdAt))
    .limit(limit);

  return rows.map(({ verification, changes, ...row }) => {
    const report = verification as Partial<Verification> | null;
    return {
      ...row,
      passed: typeof report?.passed === "boolean" ? report.passed : null,
      unsupported: Array.isArray(report?.unsupported) ? report.unsupported.length : 0,
      changes: Array.isArray(changes) ? changes.length : 0,
    };
  });
}

/** The newest base resume that has a PDF, with that PDF. */
export async function latestBaseRender(
  userId: string,
): Promise<{ resume: ResumeRow; document: Document } | null> {
  const [row] = await db
    .select({ resume: resumes, document: documents })
    .from(resumes)
    .innerJoin(
      documents,
      and(
        eq(documents.id, resumes.renderedDocumentId),
        eq(documents.userId, userId),
      ),
    )
    .where(
      and(
        eq(resumes.userId, userId),
        eq(resumes.kind, "base"),
        isNotNull(resumes.renderedDocumentId),
      ),
    )
    .orderBy(desc(resumes.version))
    .limit(1);
  return row ?? null;
}

/** Points a resume at its freshly rendered PDF. */
export async function markRendered(
  userId: string,
  resumeId: string,
  documentId: string,
  engine: "html" | "latex" = "html",
): Promise<void> {
  await db
    .update(resumes)
    .set({
      renderedDocumentId: documentId,
      renderEngine: engine,
      updatedAt: new Date(),
    })
    .where(and(eq(resumes.id, resumeId), eq(resumes.userId, userId)));
}

/**
 * Saves a tailored resume as the next version for its match and makes it the
 * match's current one, in one transaction — a match never points at a resume
 * that failed to save.
 */
export async function saveTailoredResume(
  userId: string,
  input: {
    matchId: string;
    resume: Resume;
    changes: TailorChange[];
    verification: Verification;
  },
): Promise<ResumeRow> {
  return db.transaction(async (tx) => {
    const [latest] = await tx
      .select({
        version: sql<number>`coalesce(max(${resumes.version}), 0)::int`,
      })
      .from(resumes)
      .where(
        and(
          eq(resumes.userId, userId),
          eq(resumes.kind, "tailored"),
          eq(resumes.jobMatchId, input.matchId),
        ),
      );

    const [row] = await tx
      .insert(resumes)
      .values({
        userId,
        kind: "tailored",
        jobMatchId: input.matchId,
        version: (latest?.version ?? 0) + 1,
        data: input.resume,
        changes: input.changes,
        verification: input.verification,
      })
      .returning();

    await tx
      .update(jobMatches)
      .set({ tailoredResumeId: row.id, updatedAt: new Date() })
      .where(and(eq(jobMatches.id, input.matchId), eq(jobMatches.userId, userId)));

    return row;
  });
}
