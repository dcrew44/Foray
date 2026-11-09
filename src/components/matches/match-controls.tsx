"use client";

import { ScanSearch } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState, useTransition } from "react";
import { toast } from "sonner";

import { requestLiveRefresh } from "@/components/live/live-provider";
import { RunCard } from "@/components/runs/run-card";
import { useRunToasts } from "@/components/runs/use-run-toasts";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { api, call } from "@/lib/api/client";
import { STAGE_LABEL, type StageId } from "@/lib/format";
import { LANES, LANE_LABEL, isLane, laneNote, type Lane } from "@/lib/lanes";
import { cn } from "@/lib/utils";

import { moveMatch, saveMatchNotes, setMatchLane } from "./match-api";
import { ALL_STAGES as STAGES } from "./stages";

/** Where this match sits on the board, changeable in place. */
export function StageSelect({ matchId, stage }: { matchId: string; stage: StageId }) {
  const router = useRouter();
  const [value, setValue] = useState(stage);
  const [pending, startTransition] = useTransition();

  return (
    <label className="flex items-center gap-2 text-[13px]">
      <span className="text-muted-foreground">Stage</span>
      <select
        className="bg-background h-8 rounded-lg border px-2 text-[13.5px] font-medium"
        disabled={pending}
        onChange={(event) => {
          const next = event.target.value as StageId;
          const previous = value;
          setValue(next);
          startTransition(async () => {
            const result = await moveMatch(matchId, next);
            if (!result.ok) {
              setValue(previous);
              toast.error(result.message ?? "That change did not save.");
              return;
            }
            toast(`Moved to ${STAGE_LABEL[next]}`);
            router.refresh();
          });
        }}
        value={value}
      >
        {STAGES.map((id) => (
          <option key={id} value={id}>
            {STAGE_LABEL[id]}
          </option>
        ))}
      </select>
    </label>
  );
}

/** What the page said about the lane when a choice was made, and the choice. */
type LaneChoice = { choice: Lane | null; lane: Lane; override: Lane | null };

/**
 * Which apply lane this match is in, changeable in place: it sits under the verdict it follows
 * from, so it is quiet (no box until it is pointed at) and says what the lane asks of the user.
 * The deep review decides the lane until the user chooses one; "Follow the review" is offered
 * once they have, and hands the match back. Django says where each change leaves the match, and
 * that sentence is the toast.
 */
export function LaneSelect({
  matchId,
  lane,
  override,
  reviewLane,
  className,
}: {
  matchId: string;
  /** The lane the match is in. */
  lane: Lane;
  /** The lane the user chose; null while the match follows the review. */
  override: Lane | null;
  /** The lane the review alone gives, which is where handing it back leaves it. */
  reviewLane: Lane;
  className?: string;
}) {
  const router = useRouter();
  const noteId = useId();
  const [pending, startTransition] = useTransition();
  // A choice shows at once, in place of what the page said when it was made. Once the page
  // says something else (the save's refresh, or a new review), the page is right again.
  const [saving, setSaving] = useState<LaneChoice | null>(null);
  const current = saving && saving.lane === lane && saving.override === override ? saving : null;
  if (saving && !current) setSaving(null);

  const shown = current ? (current.choice ?? reviewLane) : lane;
  const chosen = current ? current.choice !== null : override !== null;
  const note = laneNote(shown, chosen);

  function choose(choice: Lane | null) {
    setSaving({ choice, lane, override });
    startTransition(async () => {
      const result = await setMatchLane(matchId, choice);
      if (!result.ok) {
        setSaving(null);
        toast.error(result.message ?? "That change did not save.");
        return;
      }
      toast(result.message);
      router.refresh();
    });
  }

  return (
    <div
      className={cn(
        "text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-[13.5px]",
        className,
      )}
    >
      <label className="flex items-center gap-1.5">
        <span>Lane</span>
        <select
          aria-describedby={note ? noteId : undefined}
          className="text-foreground hover:border-border h-7 rounded-md border border-transparent bg-transparent px-1 text-[13.5px] font-medium"
          disabled={pending}
          onChange={(event) => choose(isLane(event.target.value) ? event.target.value : null)}
          value={shown}
        >
          {LANES.map((id) => (
            <option key={id} value={id}>
              {LANE_LABEL[id]}
            </option>
          ))}
          {chosen && <option value="review">Follow the review</option>}
        </select>
      </label>
      {note && <span id={noteId}>{note}</span>}
    </div>
  );
}

/** Starts a deep review of this one match and shows it running. */
export function EvaluateButton({ matchId, label = "Get a deep review" }: { matchId: string; label?: string }) {
  const router = useRouter();
  const [runId, setRunId] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  useRunToasts(runId);

  if (runId) return <RunCard compact runId={runId} />;

  return (
    <Button
      disabled={pending}
      onClick={() =>
        startTransition(async () => {
          // 202 when a review was queued, 200 when this match is already being reviewed: both
          // name the run, and the card follows it.
          const result = await call(
            api.POST("/api/matches/{id}/evaluate", { params: { path: { id: matchId } } }),
          );
          if (!result.ok) {
            toast.error(result.message);
            return;
          }
          setRunId(result.runId);
          requestLiveRefresh();
          router.refresh();
        })
      }
      variant="outline"
    >
      <ScanSearch />
      {pending ? "Starting…" : label}
    </Button>
  );
}

/** Private notes on a match, saved as you type (after a pause). */
export function MatchNotes({ matchId, initial }: { matchId: string; initial: string | null }) {
  const router = useRouter();
  const [value, setValue] = useState(initial ?? "");
  const [state, setState] = useState<"idle" | "saving" | "saved">("idle");
  const last = useRef(initial ?? "");

  useEffect(() => {
    if (value === last.current) return;
    const timer = setTimeout(async () => {
      setState("saving");
      const result = await saveMatchNotes(matchId, value.trim() ? value : null);
      if (result.ok) {
        last.current = value;
        setState("saved");
        // The page re-reads, as the server action's revalidation did; the text being typed is
        // this component's state, so it survives.
        router.refresh();
      } else {
        setState("idle");
        toast.error(result.message ?? "Notes did not save.");
      }
    }, 800);
    return () => clearTimeout(timer);
  }, [matchId, value, router]);

  return (
    <div className="space-y-1.5">
      <Textarea
        aria-label="Notes"
        className="min-h-28 text-[13.5px]"
        onChange={(event) => {
          setValue(event.target.value);
          setState("idle");
        }}
        placeholder="Recruiter names, referral contacts, what you asked in the interview…"
        value={value}
      />
      <p className="text-muted-foreground h-4 text-[12px]" role="status">
        {state === "saving" ? "Saving…" : state === "saved" ? "Saved" : ""}
      </p>
    </div>
  );
}
