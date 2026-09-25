import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import { getDocument } from "@/lib/db/queries/profile";
import { storage } from "@/lib/storage";

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * A stored file — an uploaded resume or a rendered PDF — for its owner only.
 *
 * Inline by default so a PDF opens in the browser's viewer (and in an
 * iframe); `?download=1` makes it an attachment. Never cached: these are
 * personal documents, and a shared cache must not keep a copy.
 */
export async function GET(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await requireUser();
  const { id } = await params;

  // Someone else's document is indistinguishable from one that does not exist.
  const document = UUID.test(id) ? await getDocument(user.id, id) : null;
  if (!document) return NextResponse.json({ error: "Not found" }, { status: 404 });

  let data: Buffer;
  try {
    data = await storage.get(document.storagePath);
  } catch (error) {
    console.error(`[documents] ${document.id}: stored file unreadable:`, error);
    return NextResponse.json({ error: "The file is missing from storage." }, { status: 404 });
  }

  const download = new URL(request.url).searchParams.get("download") === "1";

  return new Response(new Uint8Array(data), {
    headers: {
      "Content-Type": document.mime,
      "Content-Length": String(data.byteLength),
      "Content-Disposition": contentDisposition(
        download ? "attachment" : "inline",
        document.filename,
      ),
      "Cache-Control": "private, no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

/**
 * RFC 6266: a plain-ASCII `filename` for old clients plus a UTF-8
 * `filename*`, so "Zoë Müller - Resume.pdf" survives the trip.
 */
function contentDisposition(type: "inline" | "attachment", filename: string): string {
  const ascii = filename.replace(/[^\x20-\x7e]/g, "_").replace(/["\\]/g, "_");
  const encoded = encodeURIComponent(filename).replace(
    /['()*]/g,
    (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`,
  );
  return `${type}; filename="${ascii}"; filename*=UTF-8''${encoded}`;
}
