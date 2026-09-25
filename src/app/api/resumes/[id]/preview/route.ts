import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import { getResume } from "@/lib/db/queries/resumes";
import { renderResumeHtml } from "@/lib/resume/render/template";
import { ResumeSchema } from "@/lib/resume/schema";

/**
 * The resume as HTML, for an instant preview in an iframe — the same template
 * the PDF is printed from, so what the user sees is what they will send.
 *
 * The page is user- and model-written text in the app's origin, so the CSP
 * allows nothing but inline styles: no scripts, no network, no forms, no
 * framing by other sites. The template escapes everything; this is the
 * second lock.
 */
const CSP = [
  "default-src 'none'",
  "style-src 'unsafe-inline'",
  "img-src data:",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'self'",
].join("; ");

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await requireUser();
  const { id } = await params;

  const row = await getResume(user.id, id);
  if (!row) return NextResponse.json({ error: "Not found" }, { status: 404 });

  const parsed = ResumeSchema.safeParse(row.data);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "This resume could not be read." },
      { status: 422 },
    );
  }

  return new Response(renderResumeHtml(parsed.data), {
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Content-Security-Policy": CSP,
      "Cache-Control": "private, no-store",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
    },
  });
}
