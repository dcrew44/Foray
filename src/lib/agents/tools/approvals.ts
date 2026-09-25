import { tool } from "ai";
import { z } from "zod";

import {
  decideApproval,
  getApproval,
  listPendingApprovals as pendingApprovals,
  type CostEstimate,
  type DeepEvalBatchItem,
  type ShortlistItem,
  type StoredDecision,
} from "@/lib/runs/approvals";

/**
 * Chat's handles on approval gates.
 *
 * A search in `approve_each` pauses twice — before spending on deep
 * evaluation, and before moving roles onto the shortlist — and the user can
 * answer either in the activity feed or here. Both paths go through the same
 * `decideApproval`, so answering in one place reads as answered in the other.
 *
 * The agent answers only what the user has actually said. Approving deep
 * evaluation spends their money, which is exactly the decision the gate exists
 * to leave with them.
 */

type PayloadShape = {
  items?: Array<DeepEvalBatchItem | ShortlistItem>;
  estimate?: CostEstimate;
};

export function listPendingApprovals(userId: string) {
  return tool({
    description:
      "List what is waiting on the user's answer. deep_eval_batch: a search " +
      "paused before deep-evaluating its best roles with the strong model — " +
      "items are the roles, estimate is the cost in USD. shortlist_proposal: " +
      "roles the deep evaluation recommends, waiting to be moved onto the " +
      "shortlist. Answer one with answer_approval once the user decides.",
    inputSchema: z.object({}),
    execute: async () => {
      const rows = await pendingApprovals(userId);

      return {
        approvals: rows.map((row) => {
          const payload = (row.payload ?? {}) as PayloadShape;
          return {
            id: row.id,
            kind: row.kind,
            title: row.title,
            runId: row.runId,
            createdAt: row.createdAt,
            items: Array.isArray(payload.items) ? payload.items : [],
            estimate: payload.estimate ?? null,
          };
        }),
      };
    },
  });
}

export function answerApproval(userId: string) {
  return tool({
    description:
      "Answer a pending approval (from list_pending_approvals) with the " +
      "user's decision. Only call this after the user has clearly said yes or " +
      "no to it in this conversation — never decide for them, and ask if it " +
      "is ambiguous. approve=true with item_ids approves just those items " +
      "(each item's matchId; a jobId also works); item_ids=null approves all " +
      "of them. approve=false declines the whole request. Approving deep " +
      "evaluation spends a few cents per role; the paused search then resumes " +
      "in the background. An approval can only be answered once.",
    inputSchema: z.object({
      approval_id: z.string().describe("The approval's id."),
      approve: z.boolean(),
      item_ids: z
        .array(z.string())
        .nullable()
        .describe(
          "With approve=true: only these items. Null means every item. Ignored when declining.",
        ),
    }),
    execute: async ({ approval_id, approve, item_ids }) => {
      const approval = await getApproval(userId, approval_id);
      if (!approval) {
        return {
          ok: false,
          message:
            "No approval with that id. Call list_pending_approvals for the current ones.",
        };
      }

      // Item ids from the model are mapped onto the payload's own, accepting
      // job ids for match ids. Approving with ids that match nothing would
      // otherwise count as a rejection — an irreversible answer the user
      // never gave — so that is refused instead.
      let itemIds: string[] | undefined;
      if (approve && item_ids) {
        itemIds = matchItems(approval.payload, item_ids);
        if (itemIds.length === 0) {
          return {
            ok: false,
            message:
              "None of those ids are items of this approval, so nothing was " +
              "answered. Use the matchId values from list_pending_approvals.",
          };
        }
      }

      let result: Awaited<ReturnType<typeof decideApproval>>;
      try {
        result = await decideApproval(
          userId,
          approval_id,
          { approved: approve, itemIds },
          "chat",
        );
      } catch (error) {
        console.error("[approvals] chat decide failed:", error);
        return {
          ok: false,
          message: "Could not resume the run just now; the request is still open. Try again.",
        };
      }

      if (!result.ok) {
        return {
          ok: false,
          message:
            result.reason === "already_decided"
              ? "That request was already answered, so nothing changed."
              : result.reason === "expired"
                ? "That request has expired and can no longer be answered."
                : "No approval with that id.",
        };
      }

      return { ok: true, message: confirmation(result.approval) };
    },
  });
}

/** The payload item ids a set of model-supplied ids refers to, in payload order. */
function matchItems(payload: unknown, ids: string[]): string[] {
  const items = ((payload ?? {}) as PayloadShape).items ?? [];
  const wanted = new Set(ids.map((id) => id.trim()));
  return items
    .filter((item) => wanted.has(item.matchId) || wanted.has(item.jobId))
    .map((item) => item.matchId);
}

function confirmation(approval: {
  kind: string;
  payload: unknown;
  decision: unknown;
}): string {
  const decision = (approval.decision ?? { approved: false }) as StoredDecision;
  const total = ((approval.payload ?? {}) as PayloadShape).items?.length ?? 0;
  const chosen = decision.itemIds?.length ?? total;
  const count =
    chosen === total
      ? `${chosen} ${chosen === 1 ? "role" : "roles"}`
      : `${chosen} of ${total} roles`;

  if (approval.kind === "deep_eval_batch") {
    return decision.approved
      ? `Approved deep evaluation of ${count}. It runs in the background; the search card shows progress, and the shortlist proposal follows.`
      : "Skipped the deep evaluation. The search will wrap up without it.";
  }
  if (approval.kind === "shortlist_proposal") {
    return decision.approved
      ? `Approved: shortlisting ${count}.`
      : "Left the shortlist as it was. The search will wrap up.";
  }
  return decision.approved ? "Approved." : "Declined.";
}
