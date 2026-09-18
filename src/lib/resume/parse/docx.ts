import mammoth from "mammoth";

import { detectGarble, type GarbleReport } from "./garble";

/**
 * DOCX is structured text, so there is no image fallback here: if mammoth
 * cannot read it, the file is not a DOCX and the user should be told that
 * rather than charged for a vision call that will also fail.
 */
export async function parseDocx(data: Buffer): Promise<{
  text: string;
  garble: GarbleReport;
}> {
  const { value } = await mammoth.extractRawText({ buffer: data });
  // Word documents have no page count until they are laid out; treating the
  // whole document as one page only affects the characters-per-page signal,
  // which is about scans and cannot apply here anyway.
  return { text: value, garble: detectGarble(value, 1) };
}
