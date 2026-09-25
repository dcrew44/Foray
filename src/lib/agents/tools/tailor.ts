import { tool } from "ai";
import { z } from "zod";

import { startTailorRun } from "@/lib/runs/tailor-run";

/**
 * Chat's handle on tailoring.
 *
 * Returns a run id straight away — tailoring is a strong-model draft, a
 * line-by-line verification and a PDF render, about a minute of work — and
 * the chat shows a live card for any tool output carrying a `runId`.
 */
export function tailorResume(userId: string, conversationId?: string | null) {
  return tool({
    description:
      "Tailor the user's resume to one job: select, reorder and reword what " +
      "their resume already says for that posting, verify every line against " +
      "the original, and render a PDF. Never adds skills or experience. " +
      "Accepts a match id or a job id (from list_jobs or get_job). Takes about " +
      "a minute; returns a run id immediately and the chat shows its progress.",
    inputSchema: z.object({
      id: z.string().describe("A matchId or jobId."),
    }),
    execute: async ({ id }) => {
      const result = await startTailorRun(userId, id, { conversationId });
      if (!result.ok) return { ok: false as const, message: result.message };
      return {
        ok: true as const,
        runId: result.runId,
        matchId: result.matchId,
        message: result.message,
      };
    },
  });
}
