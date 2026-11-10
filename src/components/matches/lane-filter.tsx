"use client";

import { useRouter } from "next/navigation";
import { useOptimistic, useTransition } from "react";

import type { StageId } from "@/lib/format";
import { LANES, LANE_LABEL, isLane, matchesHref, type Lane } from "@/lib/lanes";
import { cn } from "@/lib/utils";

/**
 * Narrows the Matches page, board or list, to one apply lane. The lane lives in the address
 * (`?lane=`) and Django does the narrowing in the page's one read, so a filtered view can be
 * linked to and survives a refresh; everything else in the address is kept.
 */
export function LaneFilter({
  lane,
  view,
  stage,
  showClosed,
  className,
}: {
  /** The lane the page is narrowed to, as Django read it from the address. */
  lane: Lane | null;
  view: "board" | "list";
  stage: StageId | null;
  showClosed: boolean;
  className?: string;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  // The choice shows at once and holds until the page it asked for has arrived.
  const [shown, setShown] = useOptimistic(lane);

  return (
    <select
      aria-busy={pending}
      aria-label="Lane"
      className={cn("bg-background h-8 rounded-lg border px-2 text-[13px]", className)}
      onChange={(event) => {
        const next = isLane(event.target.value) ? event.target.value : null;
        startTransition(() => {
          setShown(next);
          router.replace(matchesHref({ view, stage, closed: showClosed, lane: next }), {
            scroll: false,
          });
        });
      }}
      value={shown ?? "all"}
    >
      <option value="all">Every lane</option>
      {LANES.map((id) => (
        <option key={id} value={id}>
          {LANE_LABEL[id]}
        </option>
      ))}
    </select>
  );
}
