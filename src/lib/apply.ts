import type { components } from "@/lib/api/schema";

/**
 * Prepared applications (#60, #62): what the backend planned for each field of a form
 * (`apps/applications/prepare.py`). These helpers only sort and word it for the page.
 */
export type ApplyItem = components["schemas"]["ApplyItem"];
export type ApplyAnswer = components["schemas"]["ApplyAnswer"];
export type ApplySent = components["schemas"]["ApplySent"];
export type ApplyVariant = components["schemas"]["ApplyVariant"];

/** The required questions nothing could be put in: what the user has to answer. */
export function missingAnswers(answers: readonly ApplyAnswer[]): ApplyAnswer[] {
  return answers.filter((answer) => answer.source === "missing" && answer.required);
}

/** The fields that will be filled, in the form's order. */
export function plannedAnswers(answers: readonly ApplyAnswer[]): ApplyAnswer[] {
  return answers.filter((answer) => answer.value !== null && answer.source !== "missing");
}

const SOURCE: Record<ApplyAnswer["source"], string> = {
  profile: "From your resume",
  file: "Your resume",
  saved: "Your saved answer",
  declined: "Declined, as you have not said",
  skipped: "Left empty",
  missing: "Needs your answer",
};

/** Where an answer came from, in a few words. */
export function sourceLabel(source: ApplyAnswer["source"]): string {
  return SOURCE[source];
}

/** What a field will hold, as text: the resume is a file, not a value to read out. */
export function answerText(answer: ApplyAnswer): string {
  if (answer.source === "file") return "Attached";
  return answer.value ?? "";
}

const BOARD: Record<string, string> = {
  greenhouse: "Greenhouse",
  lever: "Lever",
  ashby: "Ashby",
  workday: "Workday",
  smartrecruiters: "SmartRecruiters",
  icims: "iCIMS",
  jibe: "iCIMS",
  gem: "Gem",
  workable: "Workable",
  recruitee: "Recruitee",
  jsearch: "another site",
};

/** Whose form it is, for a sentence: "Greenhouse", or "the employer's site". */
export function formName(source: string): string {
  return BOARD[source] ?? "the employer's site";
}

/** "the same day", "1 day after it was posted", "12 days after it was posted". */
export function afterPosted(seconds: number | null | undefined): string | null {
  if (seconds == null) return null;
  const days = Math.floor(seconds / 86400);
  if (days === 0) return "the same day it was posted";
  return `${days.toLocaleString()} ${days === 1 ? "day" : "days"} after it was posted`;
}
