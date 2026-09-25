import type { Job } from "pg-boss";

import { recordDocument } from "@/lib/db/queries/profile";
import { getResume, markRendered } from "@/lib/db/queries/resumes";
import type {
  ResumeRenderPayload,
  ResumeTailorPayload,
} from "@/lib/queue/queues";
import { renderResumeHtml } from "@/lib/resume/render/template";
import { ResumeSchema, type Resume } from "@/lib/resume/schema";
import { emit } from "@/lib/runs/events";
import { finishRun, getRun, mergeStats, startRun } from "@/lib/runs/runs";
import { executeTailorRun } from "@/lib/runs/tailor-run";
import { sha256, storage, storageKey } from "@/lib/storage";

import { renderPdf } from "../pdf";

/**
 * Tailoring and PDF rendering.
 *
 * Tailoring itself lives in lib/runs/tailor-run.ts; what this adds is the one
 * step only the worker may do, rendering the PDF. That step comes last and is
 * allowed to fail on its own: the verified tailored resume is already saved,
 * and the app can preview it as HTML while the user retries the PDF.
 */

export async function handleResumeTailor(jobs: Job<ResumeTailorPayload>[]) {
  for (const job of jobs) {
    const { userId, runId } = job.data;
    const run = await getRun(userId, runId);
    if (!run) {
      console.error(`[worker] resume.tailor: no run ${runId}`);
      continue;
    }
    // A run cancelled before the worker picked it up should not start.
    if (run.status === "cancelled") continue;

    console.log(`[worker] tailor run ${run.id}`);
    const tailored = await executeTailorRun(run);
    // Failures and cancellations are already recorded on the run.
    if (!tailored) continue;

    await mergeStats(run.id, { phase: "rendering" });

    let documentId: string | null = null;
    let pages: number | null = null;
    try {
      ({ documentId, pages } = await renderStoredResume(userId, tailored.resumeId));
    } catch (error) {
      console.error(`[worker] tailor run ${run.id}: render failed:`, error);
      await emit({
        userId,
        runId: run.id,
        kind: "render.failed",
        level: "warn",
        message:
          "Your tailored resume was saved, but the PDF could not be made. The preview still works; try rendering it again.",
        data: { resumeId: tailored.resumeId },
      });
    }

    await mergeStats(run.id, { phase: "done", documentId, pages });
    await finishRun(run.id, {
      status: "completed",
      output: {
        resumeId: tailored.resumeId,
        documentId,
        pages,
        changes: tailored.changes,
        unsupported: tailored.unsupported,
        matchId: tailored.matchId,
      },
    });
    await emit({
      userId,
      runId: run.id,
      kind: "tailor.done",
      level: tailored.unsupported > 0 ? "warn" : "info",
      message: doneMessage(tailored.label, pages, tailored.unsupported),
      data: {
        matchId: tailored.matchId,
        resumeId: tailored.resumeId,
        documentId,
      },
    });
  }
}

export async function handleResumeRender(jobs: Job<ResumeRenderPayload>[]) {
  for (const job of jobs) {
    const { userId, runId, resumeId } = job.data;
    const run = await getRun(userId, runId);
    if (!run) {
      console.error(`[worker] resume.render: no run ${runId}`);
      continue;
    }
    if (run.status === "cancelled") continue;

    await startRun(run.id);
    await mergeStats(run.id, { phase: "rendering" });

    try {
      const { documentId, pages } = await renderStoredResume(userId, resumeId);

      await mergeStats(run.id, { phase: "done", documentId, pages });
      await finishRun(run.id, {
        status: "completed",
        output: { resumeId, documentId, pages },
      });
      await emit({
        userId,
        runId: run.id,
        kind: "render.done",
        message: `Rendered your resume to a ${pages}-page PDF.`,
        data: { resumeId, documentId, pages },
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.error(`[worker] render run ${run.id} failed:`, error);
      await finishRun(run.id, { status: "failed", error: message });
      await emit({
        userId,
        runId: run.id,
        kind: "render.failed",
        level: "error",
        message: `Could not render the PDF: ${message}`,
        data: { resumeId },
      });
    }
  }
}

/**
 * Renders a stored resume to PDF, stores it, and points the resume at it.
 *
 * When the normal layout runs just past one page, the compact density is
 * tried and kept if it saves a page. It changes type size and spacing only,
 * never content, and a resume that spills three lines onto a second page
 * looks careless in a way a slightly smaller font does not.
 */
export async function renderStoredResume(
  userId: string,
  resumeId: string,
): Promise<{ documentId: string; pages: number }> {
  const row = await getResume(userId, resumeId);
  if (!row) throw new Error("That resume no longer exists.");

  const parsed = ResumeSchema.safeParse(row.data);
  if (!parsed.success) throw new Error("The stored resume could not be read.");
  const resume = parsed.data;

  let rendered = await renderPdf(renderResumeHtml(resume));
  if (rendered.pages > 1) {
    const compact = await renderPdf(renderResumeHtml(resume, { density: "compact" }));
    if (compact.pages < rendered.pages) rendered = compact;
  }

  const filename = pdfFilename(resume);
  const path = await storage.put(
    storageKey(userId, "resume_pdf", filename),
    rendered.pdf,
    "application/pdf",
  );
  const document = await recordDocument({
    userId,
    kind: "resume_pdf",
    filename,
    mime: "application/pdf",
    sizeBytes: rendered.pdf.byteLength,
    storagePath: path,
    sha256: sha256(rendered.pdf),
  });
  // Identical bytes were already stored (documents are unique per hash), so
  // the copy just written is redundant.
  if (document.storagePath !== path) await storage.delete(path);

  await markRendered(userId, resumeId, document.id, "html");
  return { documentId: document.id, pages: rendered.pages };
}

/** "Dana Reyes - Resume.pdf": what a recruiter sees when they download it. */
function pdfFilename(resume: Resume): string {
  const name = resume.basics.name
    ?.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  return name ? `${name} - Resume.pdf` : "Resume.pdf";
}

function doneMessage(label: string, pages: number | null, unsupported: number): string {
  const length = pages === 1 ? " It fits on one page." : pages ? ` It runs to ${pages} pages.` : "";
  const review =
    unsupported > 0
      ? ` ${unsupported} ${unsupported === 1 ? "line needs" : "lines need"} your review before you send it.`
      : "";
  return `Your resume for ${label} is ready.${length}${review}`;
}
