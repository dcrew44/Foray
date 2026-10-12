"use client";

import { Check } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState, useTransition } from "react";
import { toast } from "sonner";

import {
  RecommendationPill,
  ScoreChip,
} from "@/components/foray/primitives";
import { requestLiveRefresh } from "@/components/live/live-provider";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { api, call } from "@/lib/api/client";
import type { LiveApproval } from "@/lib/api/types";
import { formatUsd } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * A decision the agents are waiting on.
 *
 * Marigold marks it as the user's move. Every item starts selected — the
 * common answer is "yes, all of them" — and unticking narrows the approval
 * rather than refusing it.
 */

type Item = {
  matchId: string;
  title: string;
  company: string;
  fastScore?: number | null;
  deepScore?: number | null;
  recommendation?: string | null;
  summary?: string | null;
};

type Estimate = { usd: number | null; perItemUsd: number | null; model: string } | null;

const COPY: Record<string, { lead: string; approve: (n: number) => string; skip: string }> = {
  deep_eval_batch: {
    lead: "Foray will read each of these postings in full against your resume with the stronger model, quoting the evidence for every requirement.",
    approve: (n) => `Review ${n} ${n === 1 ? "role" : "roles"}`,
    skip: "Skip review",
  },
  shortlist_proposal: {
    lead: "The deep review recommends applying to these. Shortlisted roles move to the Shortlisted column on your board.",
    approve: (n) => `Shortlist ${n}`,
    skip: "Not now",
  },
};

const PREVIEW = 6;

export function ApprovalCard({
  approval,
  className,
}: {
  approval: Pick<LiveApproval, "id" | "kind" | "title" | "payload">;
  className?: string;
}) {
  const items = useMemo(
    () => ((approval.payload.items as Item[] | undefined) ?? []).filter((item) => item?.matchId),
    [approval.payload],
  );
  const estimate = (approval.payload.estimate as Estimate) ?? null;
  const copy = COPY[approval.kind] ?? {
    lead: "Foray is waiting for your decision before it continues.",
    approve: () => "Approve",
    skip: "Decline",
  };

  const router = useRouter();
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(items.map((item) => item.matchId)),
  );
  const [expanded, setExpanded] = useState(false);
  const [decided, setDecided] = useState<null | "approved" | "skipped">(null);
  const [pending, startTransition] = useTransition();

  const visible = expanded ? items : items.slice(0, PREVIEW);
  const count = selected.size;
  const cost =
    estimate?.perItemUsd != null ? estimate.perItemUsd * count : estimate?.usd ?? null;

  function decide(approved: boolean) {
    startTransition(async () => {
      const result = await call(
        api.POST("/api/approvals/{id}/decision", {
          params: { path: { id: approval.id } },
          body: { approved, itemIds: approved ? [...selected] : undefined },
        }),
      );
      // The pages that show this decision re-read, whether it went through or someone (the
      // chat, another tab) answered first.
      requestLiveRefresh();
      router.refresh();
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      setDecided(approved && count > 0 ? "approved" : "skipped");
    });
  }

  if (decided) {
    return (
      <div className={cn("text-muted-foreground flex items-center gap-2 rounded-xl border px-4 py-3 text-[13.5px]", className)}>
        <Check className="text-success size-4" />
        {decided === "approved" ? "Approved. Foray is on it." : "Skipped."}
      </div>
    );
  }

  return (
    <div
      className={cn(
        "bg-signal-soft/45 border-signal/50 overflow-hidden rounded-xl border border-l-[3px]",
        className,
      )}
    >
      <div className="space-y-1 px-4 pt-3.5 pb-3">
        <p className="text-[14.5px] font-semibold">{approval.title}</p>
        <p className="text-foreground/75 text-[13px] leading-relaxed">{copy.lead}</p>
      </div>

      {items.length > 0 && (
        <ul className="bg-background/70 divide-y border-y">
          {visible.map((item) => {
            const checked = selected.has(item.matchId);
            return (
              <li key={item.matchId}>
                <label className="hover:bg-muted/50 flex cursor-pointer items-center gap-3 px-4 py-2.5">
                  <Checkbox
                    checked={checked}
                    onCheckedChange={(value) =>
                      setSelected((previous) => {
                        const next = new Set(previous);
                        if (value) next.add(item.matchId);
                        else next.delete(item.matchId);
                        return next;
                      })
                    }
                  />
                  <ScoreChip
                    evaluated={item.deepScore != null}
                    score={item.deepScore ?? item.fastScore ?? null}
                    size="sm"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13.5px] font-medium">{item.title}</span>
                    <span className="text-muted-foreground block truncate text-[12.5px]">
                      {item.company}
                    </span>
                  </span>
                  <RecommendationPill className="hidden sm:inline-flex" value={item.recommendation} />
                </label>
              </li>
            );
          })}
          {items.length > PREVIEW && (
            <li>
              <button
                className="text-muted-foreground hover:text-foreground w-full px-4 py-2 text-left text-[12.5px]"
                onClick={() => setExpanded(!expanded)}
                type="button"
              >
                {expanded ? "Show fewer" : `Show all ${items.length}`}
              </button>
            </li>
          )}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <Button disabled={pending || count === 0} onClick={() => decide(true)} size="sm">
          {copy.approve(count)}
        </Button>
        <Button disabled={pending} onClick={() => decide(false)} size="sm" variant="ghost">
          {copy.skip}
        </Button>
        {approval.kind === "deep_eval_batch" && cost != null && (
          <span className="text-muted-foreground ml-auto text-[12.5px]">
            About {formatUsd(cost)}
            {estimate?.model ? ` on ${estimate.model.split(":").pop()}` : ""}
          </span>
        )}
      </div>
    </div>
  );
}
