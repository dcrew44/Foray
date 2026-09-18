import type { LlmContext } from "@/lib/llm/context";
import { generateStructured } from "@/lib/llm/structured";
import {
  ExtractedResumeSchema,
  withIds,
  type ExtractedResume,
  type ItemSource,
  type Resume,
} from "./schema";
import { parseDocx } from "./parse/docx";
import { parsePdf, renderPdfPages } from "./parse/pdf";
import type { GarbleReport } from "./parse/garble";

/**
 * A document in, a structured resume out.
 *
 * The one rule that matters here is that nothing may be invented. A resume is
 * the factual basis for every later claim the product makes on the user's
 * behalf — tailored bullets cite these items by id — so a plausible guess is
 * worse than an omission.
 */

const INSTRUCTIONS = `You transcribe resumes into structured data. You are a transcriber, not an editor.

Rules, in order of importance:
1. Never invent. If a field is not in the document, leave it null or omit the
   item. Do not infer an end date from context, do not guess a degree from a
   university name, do not expand an abbreviation you are not certain of.
2. Copy text verbatim. Bullets, job titles, company names and dates appear
   exactly as written — including "Present", "2021 – 2023", and any unusual
   capitalisation. Do not rewrite, summarise, shorten or improve wording.
3. Keep dates in the document's own format. Do not normalise "June 2024" to
   "2024-06".
4. The text may be in the wrong reading order. PDF extraction interleaves
   multi-column layouts, so a line from a sidebar can appear in the middle of a
   job description. Reassemble by meaning: a bullet belongs to the job it
   describes, a skill to the skills list. Do not drop content because it
   appeared in an odd place.
5. Skills keep the document's own grouping in \`category\` when it has one
   (e.g. "Languages", "Tools"). Never invent a grouping.
6. A line that is a section heading ("EXPERIENCE", "EDUCATION") is structure,
   not content — do not emit it as a bullet or a skill.
7. Letter-spaced text extracts one glyph at a time. "S U M M A R Y" is the
   heading "SUMMARY", and "I L O N A  B E R G Q U I S T" is the name "Ilona
   Bergquist". Close the gaps when a run of single characters clearly spells
   words. This applies to headings and names, not to genuine acronyms or
   initials — "B S" in a degree line is "BS", but "A P I" is "API".
8. Justified text is hyphenated at line breaks, so a word may arrive split as
   "ap-\nplied". Rejoin it into "applied". Keep the hyphen only where the word
   is genuinely hyphenated, as in "full-stack" or "event-driven".`;

const VISION_INSTRUCTIONS = `${INSTRUCTIONS}
7. You are reading page images because the document's text layer was unusable.
   Read what is visibly printed. If a character is genuinely illegible, omit
   the item rather than guessing at it.`;

export type ExtractionResult = {
  resume: Resume;
  /** Raw text pulled from the file, stored on the document row. */
  text: string;
  /** True when the text layer was unusable and page images were used instead. */
  usedVision: boolean;
  garble: GarbleReport;
  retried: boolean;
};

export async function extractResume(input: {
  data: Buffer;
  mime: string;
  source?: ItemSource;
  context?: LlmContext;
  /**
   * Read the page images even when the text layer looks fine. The garble
   * heuristic cannot see wrong reading order, so this is the escape hatch when
   * a user says the parse is wrong — and it is how the vision path gets
   * exercised without hunting for a scanned PDF.
   */
  forceVision?: boolean;
}): Promise<ExtractionResult> {
  const source = input.source ?? "resume";
  const context = input.context ?? {};

  const isPdf = input.mime === "application/pdf";
  const parsed = isPdf
    ? await parsePdf(input.data)
    : { ...(await parseDocx(input.data)), pageCount: 1 };

  const useVision = isPdf && (input.forceVision || parsed.garble.garbled);

  if (useVision && !input.forceVision) {
    console.warn(
      `[resume] text layer unusable, falling back to page images: ${parsed.garble.reasons.join("; ")}`,
    );
  }

  const { value, retried } = useVision
    ? await extractFromImages(input.data, context)
    : await extractFromText(parsed.text, context);

  return {
    resume: withIds(value, source),
    text: parsed.text,
    usedVision: useVision,
    garble: parsed.garble,
    retried,
  };
}

// callLLM already waits for the purpose's provider rate-limit slot and
// establishes the ledger attribution context, so neither is repeated here.
async function extractFromText(text: string, context: LlmContext) {
  return generateStructured<ExtractedResume>(
    "extract",
    ExtractedResumeSchema,
    {
      instructions: INSTRUCTIONS,
      prompt: `Transcribe this resume.\n\n<resume>\n${text}\n</resume>`,
    },
    context,
  );
}

async function extractFromImages(data: Buffer, context: LlmContext) {
  const images = await renderPdfPages(data);

  return generateStructured<ExtractedResume>(
    "extract",
    ExtractedResumeSchema,
    {
      instructions: VISION_INSTRUCTIONS,
      messages: [
        {
          role: "user",
          content: [
            { type: "text", text: "Transcribe this resume." },
            ...images.map((image) => ({
              type: "file" as const,
              mediaType: "image/png",
              data: image,
            })),
          ],
        },
      ],
    },
    context,
  );
}
