import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import { recordDocument, saveBaseResume } from "@/lib/db/queries/profile";
import { extractResume } from "@/lib/resume/extract";
import { sha256, storage, storageKey } from "@/lib/storage";

/** 10 MB. A resume that large is a scan, and scans go through the vision path. */
const MAX_BYTES = 10 * 1024 * 1024;

const ACCEPTED: Record<string, "resume_upload" | "linkedin_pdf"> = {
  "application/pdf": "resume_upload",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
    "resume_upload",
};

/**
 * Resume upload.
 *
 * Extraction runs inline rather than as a worker job: the user is watching an
 * upload spinner, it is one LLM call, and making them poll an activity feed for
 * their own resume would be worse. Bulk work still belongs in the worker.
 */
export async function POST(request: Request) {
  const user = await requireUser();

  const form = await request.formData();
  const file = form.get("file");
  const kindHint = form.get("kind");

  if (!(file instanceof File)) {
    return NextResponse.json({ error: "No file uploaded." }, { status: 400 });
  }
  if (file.size > MAX_BYTES) {
    return NextResponse.json(
      { error: `That file is ${(file.size / 1024 / 1024).toFixed(1)} MB; the limit is 10 MB.` },
      { status: 413 },
    );
  }

  const mime = file.type || "application/octet-stream";
  if (!(mime in ACCEPTED)) {
    return NextResponse.json(
      { error: "Upload a PDF or a Word (.docx) file." },
      { status: 415 },
    );
  }

  const kind =
    kindHint === "linkedin_pdf" ? "linkedin_pdf" : ACCEPTED[mime];

  const data = Buffer.from(await file.arrayBuffer());
  const digest = sha256(data);
  const path = await storage.put(
    storageKey(user.id, kind, file.name),
    data,
    mime,
  );

  try {
    const extraction = await extractResume({
      data,
      mime,
      source: kind === "linkedin_pdf" ? "linkedin_pdf" : "resume",
      context: { userId: user.id },
    });

    const document = await recordDocument({
      userId: user.id,
      kind,
      filename: file.name,
      mime,
      sizeBytes: file.size,
      storagePath: path,
      sha256: digest,
      textExtracted: extraction.text,
    });

    // Step 9 routes linkedin_pdf through the merge path instead; until then it
    // is stored and parsed but does not overwrite the base resume.
    const resume =
      kind === "resume_upload"
        ? await saveBaseResume({
            userId: user.id,
            resume: extraction.resume,
            documentId: document.id,
          })
        : null;

    return NextResponse.json({
      documentId: document.id,
      resumeId: resume?.id ?? null,
      usedVision: extraction.usedVision,
      garble: extraction.garble,
      counts: {
        work: extraction.resume.work.length,
        education: extraction.resume.education.length,
        skills: extraction.resume.skills.length,
        projects: extraction.resume.projects.length,
      },
    });
  } catch (error) {
    // The file is already stored, so a parsing failure is recoverable: the user
    // can be shown what went wrong without having to upload again.
    console.error("[uploads] extraction failed:", error);
    return NextResponse.json(
      {
        error:
          error instanceof Error
            ? `Could not read that resume: ${error.message}`
            : "Could not read that resume.",
      },
      { status: 422 },
    );
  }
}
