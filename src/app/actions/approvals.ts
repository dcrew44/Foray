"use server";

import { revalidatePath } from "next/cache";
import { z } from "zod";

import { requireUser } from "@/lib/auth/session";
import { decideApproval, payloadItemIds } from "@/lib/runs/approvals";

/**
 * Answering an approval card from the UI.
 *
 * The same `decideApproval` the chat tool uses, so a card answered in one
 * place reads as answered in the other, and answering twice — a double click,
 * or the UI racing chat — decides once.
 */

const Decision = z.object({
  approved: z.boolean(),
  // null is read as "all items", the same as leaving it out.
  itemIds: z
    .array(z.string())
    .nullish()
    .transform((ids) => ids ?? undefined),
});

export async function decideApprovalAction(
  approvalId: string,
  decision: { approved: boolean; itemIds?: string[] },
): Promise<{ ok: true; message: string } | { ok: false; message: string }> {
  const user = await requireUser();

  // It arrives from the browser, so it is validated rather than trusted.
  const parsed = Decision.safeParse(decision);
  if (typeof approvalId !== "string" || !parsed.success) {
    return { ok: false, message: "That answer could not be read." };
  }

  let result: Awaited<ReturnType<typeof decideApproval>>;
  try {
    result = await decideApproval(user.id, approvalId, parsed.data, "ui");
  } catch (error) {
    console.error("[approvals] decide failed:", error);
    return {
      ok: false,
      message: "Could not resume the run just now. Try again in a moment.",
    };
  }

  revalidatePath("/home");
  revalidatePath("/activity");
  revalidatePath("/matches", "layout");

  if (!result.ok) {
    return {
      ok: false,
      message:
        result.reason === "already_decided"
          ? "That was already answered."
          : result.reason === "expired"
            ? "That request has expired."
            : "That request no longer exists.",
    };
  }

  const approval = result.approval;
  const decided = approval.decision as { approved: boolean; itemIds?: string[] };
  const total = payloadItemIds(approval.payload).length;
  const chosen = decided.itemIds?.length ?? total;

  if (!decided.approved) {
    return {
      ok: true,
      message:
        approval.kind === "deep_eval_batch"
          ? "Skipped the deep evaluation."
          : approval.kind === "shortlist_proposal"
            ? "Left the shortlist as it was."
            : "Declined.",
    };
  }

  return {
    ok: true,
    message:
      approval.kind === "deep_eval_batch"
        ? `Approved. Deep-evaluating ${chosen} ${chosen === 1 ? "role" : "roles"} in the background.`
        : approval.kind === "shortlist_proposal"
          ? `Approved. Shortlisting ${chosen} ${chosen === 1 ? "role" : "roles"}.`
          : "Approved.",
  };
}
