import { extractText, getDocumentProxy, renderPageAsImage } from "unpdf";

import { detectGarble, type GarbleReport } from "./garble";

/**
 * Text first, pictures only if the text is unusable.
 *
 * Reading the text layer is free; a vision call is not. So every PDF is parsed
 * as text, measured, and only rendered to images when the measurement says the
 * text cannot be trusted.
 */

export type PdfParse = {
  text: string;
  pageCount: number;
  garble: GarbleReport;
};

export async function parsePdf(data: Buffer): Promise<PdfParse> {
  const bytes = new Uint8Array(data);
  const { text, totalPages } = await extractText(bytes, { mergePages: true });

  return {
    text,
    pageCount: totalPages,
    garble: detectGarble(text, totalPages),
  };
}

/** How many pages we are willing to send to a vision model. */
const MAX_VISION_PAGES = 4;

/**
 * Renders pages as PNGs for the vision fallback.
 *
 * Scale 2 is the cheapest setting at which 9–10pt resume body text stays
 * legible; below that the model starts inventing plausible-looking dates.
 */
export async function renderPdfPages(
  data: Buffer,
  maxPages = MAX_VISION_PAGES,
): Promise<Buffer[]> {
  const bytes = new Uint8Array(data);
  const document = await getDocumentProxy(bytes);
  const pages = Math.min(document.numPages, maxPages);

  const images: Buffer[] = [];
  for (let page = 1; page <= pages; page += 1) {
    const rendered = await renderPageAsImage(document, page, {
      scale: 2,
      // unpdf has no DOM canvas in Node and will not guess a backend, so the
      // native one is handed to it explicitly.
      canvasImport: () => import("@napi-rs/canvas"),
    });
    images.push(Buffer.from(rendered));
  }
  return images;
}
