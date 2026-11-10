import { LANE_HINT, isLane, laneLabel } from "@/lib/lanes";
import { cn } from "@/lib/utils";

/**
 * The apply lane a match is in, beside its verdict (`VerdictPill`). Django decides it for every
 * row (`apps/matching/lanes.py`), so the list, the board, Home and the match page agree.
 *
 * A quiet word, not a second pill: the verdict is what the row leads with, and the lane mostly
 * follows from it. A match in no lane says nothing. Server-safe, and a span: it sits inside
 * links.
 */
export function LaneLabel({
  lane,
  className,
}: {
  lane: string | null | undefined;
  className?: string;
}) {
  const label = laneLabel(lane);
  if (!label || !isLane(lane)) return null;
  const hint = LANE_HINT[lane];
  return (
    <span
      className={cn("text-muted-foreground text-[12px] whitespace-nowrap", className)}
      title={hint ? `${label} lane: ${hint}` : undefined}
    >
      {label}
      <span className="sr-only"> lane</span>
    </span>
  );
}
