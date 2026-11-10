"use client";

import { Copy, PenLine, RotateCcw, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";
import { toast } from "sonner";

import { requestLiveRefresh, useRun } from "@/components/live/live-provider";
import { RunCard } from "@/components/runs/run-card";
import { useRunToasts } from "@/components/runs/use-run-toasts";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { api, call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { NOTE_MAX_LENGTH, firstName } from "@/lib/contacts";

type MatchContact = components["schemas"]["MatchContact"];
type MatchNote = components["schemas"]["MatchNote"];

/**
 * A short note to one person the user knows at this posting's company (#25).
 *
 * "Draft a note" starts an `outreach` run: one model call that writes three to five sentences
 * from the user's resume and the posting. The run's card shows while it works, its outcome is
 * toasted once, and the page re-reads on `run.completed`, which brings the note. From then on
 * the note is the user's: they edit it here (saved as they type), copy it, and send it
 * themself. Foray has no way to send it.
 */
export function ContactNote({
  matchId,
  person,
  hasResume,
}: {
  matchId: string;
  person: MatchContact;
  hasResume: boolean;
}) {
  const router = useRouter();
  const [runId, setRunId] = useState<string | null>(null);
  useRunToasts(runId);
  const run = useRun(runId);
  const [pending, startTransition] = useTransition();
  /** Asked before a new draft replaces the user's own edits. */
  const [asking, setAsking] = useState(false);
  // The draft on screen when a run was started ("" for none). Once the page shows another,
  // that run has delivered and the note takes over from its card.
  const [startedFrom, setStartedFrom] = useState<string | null>(null);
  const note = person.note;
  const delivered = runId !== null && (note?.draftedAt ?? "") !== startedFrom;
  // A run that failed or was stopped delivers nothing: its card stays to say why, and the
  // button is offered again.
  const ended = run?.status === "failed" || run?.status === "cancelled";
  const drafting = runId !== null && !delivered && !ended;
  const first = firstName(person.name);

  function draft() {
    setAsking(false);
    startTransition(async () => {
      const result = await call(
        api.POST("/api/outreach", { body: { matchId, contactId: person.id } }),
      );
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      setStartedFrom(note?.draftedAt ?? "");
      setRunId(result.runId);
      requestLiveRefresh();
    });
  }

  function discard(id: string) {
    startTransition(async () => {
      const result = await call(api.DELETE("/api/outreach/{id}", { params: { path: { id } } }));
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      toast(result.message);
      setRunId(null);
      router.refresh();
    });
  }

  if (drafting && runId) {
    return <RunCard className="mt-2.5" compact runId={runId} />;
  }

  if (!note) {
    return (
      <div className="mt-2.5 space-y-2">
        {ended && runId && <RunCard compact runId={runId} />}
        <Button disabled={pending || !hasResume} onClick={draft} size="sm" variant="outline">
          {pending ? <Spinner /> : <PenLine />}
          {ended ? "Try again" : `Draft a note to ${first}`}
        </Button>
        {!hasResume && (
          <p className="text-muted-foreground text-[12.5px] leading-snug">
            Upload a resume first: the note is written from what it says about you.
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="mt-2.5 space-y-2">
      {/* A new draft is a new editor: its text is the draft's, not what was typed before. */}
      <NoteEditor key={`${note.id}:${note.draftedAt}`} name={first} note={note} />
      {ended && runId && <RunCard compact runId={runId} />}
      {asking ? (
        <div
          aria-label="Draft again"
          className="flex flex-wrap items-center gap-2 text-[12.5px]"
          role="group"
        >
          <span>A new draft replaces this note, your edits included.</span>
          <Button disabled={pending} onClick={draft} size="xs" variant="outline">
            Replace it
          </Button>
          <Button disabled={pending} onClick={() => setAsking(false)} size="xs" variant="ghost">
            Keep this one
          </Button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            disabled={pending || !hasResume}
            onClick={() => setAsking(true)}
            size="xs"
            variant="ghost"
          >
            {pending ? <Spinner /> : <RotateCcw />}
            Draft again
          </Button>
          <Button disabled={pending} onClick={() => discard(note.id)} size="xs" variant="ghost">
            <Trash2 />
            Discard
          </Button>
        </div>
      )}
    </div>
  );
}

/**
 * The note as text the user owns: saved a moment after they stop typing, and copied whole.
 * The copy is what is in the box, saved or not.
 */
function NoteEditor({ note, name }: { note: MatchNote; name: string }) {
  const router = useRouter();
  const [value, setValue] = useState(note.body);
  const [state, setState] = useState<"idle" | "saving" | "saved" | "empty">("idle");
  const last = useRef(note.body);

  useEffect(() => {
    if (value === last.current) return;
    if (!value.trim()) return;
    const timer = setTimeout(async () => {
      setState("saving");
      const result = await call(
        api.PATCH("/api/outreach/{id}", { params: { path: { id: note.id } }, body: { body: value } }),
      );
      if (result.ok) {
        last.current = value;
        setState("saved");
        // The page re-reads so "edited" is true for the next draft; the text being typed is
        // this component's state, so it survives.
        router.refresh();
      } else {
        setState("idle");
        toast.error(result.message);
      }
    }, 800);
    return () => clearTimeout(timer);
  }, [note.id, value, router]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      toast.success(`Copied. Send it to ${name} yourself, wherever you talk.`);
    } catch {
      toast.error("Could not copy. Select the text and copy it yourself.");
    }
  }

  return (
    <div className="space-y-1.5">
      <Textarea
        aria-label={`Note to ${name}`}
        className="min-h-36 text-[13.5px] leading-relaxed md:text-[13.5px]"
        maxLength={NOTE_MAX_LENGTH}
        onChange={(event) => {
          setValue(event.target.value);
          setState(event.target.value.trim() ? "idle" : "empty");
        }}
        value={value}
      />
      <div className="flex items-center justify-between gap-3">
        <p className="text-muted-foreground min-h-4 text-[12px]" role="status">
          {state === "saving"
            ? "Saving…"
            : state === "saved"
              ? "Saved"
              : state === "empty"
                ? "An empty note is not saved. Discard it instead."
                : note.edited
                  ? "Your edits are saved."
                  : "Drafted from your resume and the posting. Edit it before you send it."}
        </p>
        <Button disabled={!value.trim()} onClick={copy} size="sm" variant="outline">
          <Copy />
          Copy
        </Button>
      </div>
    </div>
  );
}
