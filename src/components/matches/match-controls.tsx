"use client";

import { ScanSearch } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";
import { toast } from "sonner";

import { evaluateMatchAction, moveMatchStageAction, updateMatchAction } from "@/app/actions/matches";
import { requestLiveRefresh } from "@/components/live/live-provider";
import { RunCard } from "@/components/runs/run-card";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { STAGE_LABEL, type StageId } from "@/lib/format";

const STAGES: StageId[] = [
  "found",
  "shortlisted",
  "applied",
  "interviewing",
  "offer",
  "rejected",
  "archived",
];

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
            const result = await moveMatchStageAction(matchId, next);
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

/** Starts a deep review of this one match and shows it running. */
export function EvaluateButton({ matchId, label = "Get a deep review" }: { matchId: string; label?: string }) {
  const [runId, setRunId] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  if (runId) return <RunCard compact runId={runId} />;

  return (
    <Button
      disabled={pending}
      onClick={() =>
        startTransition(async () => {
          const result = await evaluateMatchAction(matchId);
          if (!result.ok) {
            toast.error(result.message);
            return;
          }
          setRunId(result.runId);
          requestLiveRefresh();
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
  const [value, setValue] = useState(initial ?? "");
  const [state, setState] = useState<"idle" | "saving" | "saved">("idle");
  const last = useRef(initial ?? "");

  useEffect(() => {
    if (value === last.current) return;
    const timer = setTimeout(async () => {
      setState("saving");
      const result = await updateMatchAction(matchId, { notes: value.trim() ? value : null });
      if (result.ok) {
        last.current = value;
        setState("saved");
      } else {
        setState("idle");
        toast.error(result.message ?? "Notes did not save.");
      }
    }, 800);
    return () => clearTimeout(timer);
  }, [matchId, value]);

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
