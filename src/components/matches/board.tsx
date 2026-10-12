"use client";

import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { FileText, MapPin } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState, useTransition } from "react";
import { toast } from "sonner";

import { RecommendationPill, ScoreChip } from "@/components/foray/primitives";
import type { MatchRow } from "@/lib/api/types";
import {
  formatLocation,
  formatSalary,
  STAGE_HINT,
  STAGE_LABEL,
  timeAgo,
  type StageId,
} from "@/lib/format";
import { cn } from "@/lib/utils";

import { moveMatch } from "./match-api";
import { CLOSED_STAGES as CLOSED, OPEN_STAGES as OPEN } from "./stages";

/**
 * The pipeline as a board. Dragging a card moves the match; the move shows
 * immediately and is undone with a toast if the server refuses it.
 */

/** Found can hold hundreds after a big search; the rest render on demand. */
const COLUMN_PAGE = 40;

export type BoardMatch = Pick<
  MatchRow,
  | "matchId"
  | "stage"
  | "title"
  | "company"
  | "locations"
  | "remote"
  | "score"
  | "deepScore"
  | "recommendation"
  | "salaryMin"
  | "salaryMax"
  | "salaryCurrency"
  | "salaryPeriod"
  | "postedAt"
  | "stageChangedAt"
  | "tailoredResumeId"
>;

export function Board({ matches, showClosed }: { matches: BoardMatch[]; showClosed: boolean }) {
  const router = useRouter();
  const [overrides, setOverrides] = useState<Record<string, StageId>>({});
  const [dragging, setDragging] = useState<BoardMatch | null>(null);
  const [, startTransition] = useTransition();

  const sensors = useSensors(
    // A short travel threshold keeps clicks working as links.
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor),
  );

  const columns = useMemo(() => {
    const byStage = new Map<StageId, BoardMatch[]>();
    for (const stage of [...OPEN, ...CLOSED]) byStage.set(stage, []);
    for (const match of matches) {
      const stage = overrides[match.matchId] ?? match.stage;
      byStage.get(stage)?.push({ ...match, stage });
    }
    return byStage;
  }, [matches, overrides]);

  function onDragStart(event: DragStartEvent) {
    const match = matches.find((item) => item.matchId === event.active.id);
    setDragging(match ? { ...match, stage: overrides[match.matchId] ?? match.stage } : null);
  }

  function onDragEnd(event: DragEndEvent) {
    setDragging(null);
    const matchId = String(event.active.id);
    const target = event.over?.id as StageId | undefined;
    const match = matches.find((item) => item.matchId === matchId);
    if (!match || !target) return;

    const from = overrides[matchId] ?? match.stage;
    if (from === target) return;
    // When the match entered `from`, for an Undo. Known only while the server's row is in
    // `from`; after a move the page has not refreshed yet, the Undo stamps now.
    const enteredAt = from === match.stage ? match.stageChangedAt : undefined;

    setOverrides((previous) => ({ ...previous, [matchId]: target }));

    startTransition(async () => {
      const result = await moveMatch(matchId, target);
      if (!result.ok) {
        setOverrides((previous) => ({ ...previous, [matchId]: from }));
        toast.error(result.message ?? "That move did not save.");
        return;
      }
      toast(`Moved to ${STAGE_LABEL[target]}`, {
        description: match.title,
        action: {
          label: "Undo",
          onClick: () => {
            setOverrides((previous) => ({ ...previous, [matchId]: from }));
            void moveMatch(matchId, from, enteredAt).then((undone) => {
              if (!undone.ok) toast.error(undone.message);
              router.refresh();
            });
          },
        },
      });
      router.refresh();
    });
  }

  const visible = showClosed ? [...OPEN, ...CLOSED] : OPEN;

  return (
    <DndContext
      // A fixed id keeps the accessibility ids dnd-kit generates identical on the
      // server and the client; its default counter differs and breaks hydration.
      id="matches-board"
      onDragCancel={() => setDragging(null)}
      onDragEnd={onDragEnd}
      onDragStart={onDragStart}
      sensors={sensors}
    >
      <div className="scrollbar-thin -mx-5 flex snap-x scroll-px-5 items-start gap-3 overflow-x-auto px-5 pb-4 sm:-mx-8 sm:scroll-px-8 sm:px-8 lg:-mx-10 lg:scroll-px-10 lg:px-10">
        {visible.map((stage) => (
          <Column key={stage} matches={columns.get(stage) ?? []} stage={stage} />
        ))}
      </div>
      <DragOverlay dropAnimation={null}>
        {dragging && <Card dragging match={dragging} />}
      </DragOverlay>
    </DndContext>
  );
}

function Column({ stage, matches }: { stage: StageId; matches: BoardMatch[] }) {
  const { isOver, setNodeRef } = useDroppable({ id: stage });
  const [limit, setLimit] = useState(COLUMN_PAGE);
  const closed = CLOSED.includes(stage);

  return (
    <section
      aria-label={STAGE_LABEL[stage]}
      className={cn(
        "flex w-[284px] shrink-0 snap-start flex-col rounded-2xl border transition-colors",
        closed ? "bg-muted/30" : "bg-canvas/55",
        isOver && "border-primary/60 bg-accent",
      )}
      ref={setNodeRef}
    >
      <header className="px-3.5 pt-3 pb-2">
        <div className="flex items-baseline justify-between">
          <h2 className="text-[13.5px] font-semibold">{STAGE_LABEL[stage]}</h2>
          <span className="text-muted-foreground tabular text-[12.5px]">{matches.length}</span>
        </div>
        <p className="text-muted-foreground mt-0.5 text-[12px]">{STAGE_HINT[stage]}</p>
      </header>
      <div className="flex min-h-24 flex-1 flex-col gap-2 px-2 pb-2">
        {matches.slice(0, limit).map((match) => (
          <DraggableCard key={match.matchId} match={match} />
        ))}
        {matches.length > limit && (
          <button
            className="text-muted-foreground hover:text-foreground rounded-lg py-2 text-[12.5px]"
            onClick={() => setLimit(limit + COLUMN_PAGE)}
            type="button"
          >
            Show {Math.min(COLUMN_PAGE, matches.length - limit)} more
          </button>
        )}
        {matches.length === 0 && (
          <p className="text-muted-foreground/80 flex min-h-40 flex-1 items-center justify-center rounded-xl border border-dashed px-3 py-6 text-center text-[12.5px]">
            Drag a card here
          </p>
        )}
      </div>
    </section>
  );
}

function DraggableCard({ match }: { match: BoardMatch }) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ id: match.matchId });
  return (
    <div
      className={cn("touch-manipulation", isDragging && "opacity-30")}
      ref={setNodeRef}
      {...attributes}
      {...listeners}
    >
      <Card match={match} />
    </div>
  );
}

function Card({ match, dragging = false }: { match: BoardMatch; dragging?: boolean }) {
  const salary = formatSalary(match);
  return (
    <Link
      className={cn(
        "bg-background hover:border-primary/40 block rounded-xl border p-3 transition-colors",
        dragging && "rotate-[1.5deg] shadow-xl",
      )}
      draggable={false}
      href={`/matches/${match.matchId}`}
    >
      <div className="flex items-start gap-3">
        <ScoreChip evaluated={match.deepScore != null} score={match.score} size="sm" />
        <div className="min-w-0 flex-1">
          <p className="line-clamp-2 text-[13.5px] leading-snug font-medium">{match.title}</p>
          <p className="text-foreground/75 mt-0.5 truncate text-[12.5px]">{match.company}</p>
        </div>
      </div>
      <div className="text-muted-foreground mt-2.5 flex items-center gap-1 text-[12px]">
        <MapPin className="size-3 shrink-0 opacity-60" />
        <span className="truncate">{formatLocation(match.locations, match.remote)}</span>
      </div>
      {(match.recommendation || salary || match.tailoredResumeId) && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <RecommendationPill value={match.recommendation} />
          {salary && <span className="text-muted-foreground tabular text-[12px]">{salary}</span>}
          {match.tailoredResumeId && (
            <span className="text-primary ml-auto inline-flex items-center gap-1 text-[11.5px]" title="Tailored resume ready">
              <FileText className="size-3" />
              Resume
            </span>
          )}
        </div>
      )}
      {match.postedAt && !match.recommendation && !salary && (
        <p className="text-muted-foreground mt-1.5 text-[11.5px]">{timeAgo(match.postedAt)}</p>
      )}
    </Link>
  );
}
