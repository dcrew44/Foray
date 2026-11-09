"use client";

import { Check, Send } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore, useTransition } from "react";
import { toast } from "sonner";

import { requestLiveRefresh, useLiveEvent } from "@/components/live/live-provider";
import { FormRow, SaveBar, useUnsavedChanges } from "@/components/profile/form-controls";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";
import type { NotificationTestData, SavedSettings } from "@/lib/api/types";
import { clockLabel, clockOptions } from "@/lib/format";

/**
 * New matches on the user's phone, through ntfy: the topic their phone is
 * subscribed to, the hours in which nothing should buzz, and a button that
 * proves a notification gets through. With no topic nothing is ever sent.
 *
 * Quiet hours are read on the user's own clock. The server keeps the browser's
 * time zone for that, so every save here sends it along.
 */

/** ntfy's rule for a topic name; the server checks the same. */
const TOPIC = /^[A-Za-z0-9_-]{0,64}$/;

/**
 * The test is a job, like the worker's own test: the request queues it, the
 * worker sends the notification, and its answer (sent, or why not) comes back
 * through the event feed. So a test that works has taken the same path a real
 * notification takes.
 */
type Test =
  | { state: "idle" }
  | { state: "waiting"; sentAt: number; jobId: number | null }
  | { state: "sent"; message: string }
  | { state: "failed"; message: string }
  | { state: "silent" };

/** How long to wait for the worker's answer before saying there was none. */
const PATIENCE_MS = 20_000;

const NOT_QUEUED = "The test could not be queued. Try again in a moment.";

/** The browser's IANA time zone; nothing on the server, where there is no browser. */
function browserZone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

const subscribe = () => () => {};

export function NotificationsForm({
  initial,
  server,
}: {
  initial: SavedSettings;
  /** The ntfy server this install publishes to, e.g. "https://ntfy.sh". */
  server: string;
}) {
  const router = useRouter();
  const baseline = {
    topic: initial.ntfyTopic,
    start: initial.quietHoursStart,
    end: initial.quietHoursEnd,
  };
  const [topic, setTopic] = useState(baseline.topic);
  const [start, setStart] = useState(baseline.start);
  const [end, setEnd] = useState(baseline.end);
  const [keepZone, setKeepZone] = useState(false);
  const [test, setTest] = useState<Test>({ state: "idle" });
  const [pending, startTransition] = useTransition();
  const zone = useSyncExternalStore(subscribe, browserZone, () => null);

  // Someone who is being notified, and whose saved time zone is not this browser's, has
  // quiet hours on the wrong clock: that counts as something to save.
  const zoneMoved = zone !== null && zone !== initial.timeZone;
  const edited = topic !== baseline.topic || start !== baseline.start || end !== baseline.end;
  const dirty = edited || (zoneMoved && baseline.topic !== "" && !keepZone);
  const topicValid = TOPIC.test(topic);
  const host = server.replace(/^https?:\/\//, "");
  // The clock the times below are read on: the saved one, or this browser's once a save is due.
  const clockZone = dirty && zone ? zone : initial.timeZone;

  function save() {
    if (!dirty || pending) return;
    startTransition(async () => {
      const result = await call(
        api.PATCH("/api/settings", {
          body: {
            ntfyTopic: topic,
            quietHoursStart: start,
            quietHoursEnd: end,
            ...(zone ? { timeZone: zone } : {}),
          },
        }),
      );
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      toast.success(result.message);
      setTest({ state: "idle" });
      // The page re-renders with what was saved, which makes the form clean again.
      router.refresh();
    });
  }

  function discard() {
    setTopic(baseline.topic);
    setStart(baseline.start);
    setEnd(baseline.end);
    setKeepZone(true);
  }

  useUnsavedChanges(dirty, save);

  useLiveEvent(["notification.test"], (event) => {
    const answer = event.data as NotificationTestData | null;
    setTest((current) => {
      if (current.state !== "waiting") return current;
      // Another tab's test is not this one's answer. Before the job id is known (the answer
      // beat the reply), any answer will do.
      if (current.jobId != null && answer?.jobId != null && answer.jobId !== current.jobId) {
        return current;
      }
      return { state: answer?.sent ? "sent" : "failed", message: event.message };
    });
  });

  // While waiting, ask the shared poll to look sooner than its idle pace, and give up after
  // a while rather than spinning forever.
  useEffect(() => {
    if (test.state !== "waiting") return;
    const nudge = setInterval(requestLiveRefresh, 1500);
    const giveUp = setTimeout(() => setTest({ state: "silent" }), PATIENCE_MS);
    return () => {
      clearInterval(nudge);
      clearTimeout(giveUp);
    };
  }, [test.state]);

  async function sendTest() {
    const sentAt = Date.now();
    setTest({ state: "waiting", sentAt, jobId: null });
    const result = await call(api.POST("/api/notifications/test"));
    if (!result.ok) {
      setTest({ state: "failed", message: result.message || NOT_QUEUED });
      return;
    }
    setTest((current) =>
      current.state === "waiting" && current.sentAt === sentAt
        ? { ...current, jobId: result.jobId }
        : current,
    );
    requestLiveRefresh();
  }

  const waiting = test.state === "waiting";
  const canTest = baseline.topic !== "" && !edited && !waiting;

  return (
    <div className="space-y-4">
      <fieldset className="min-w-0 divide-y rounded-xl border">
        <FormRow
          hint={
            <>
              Install the{" "}
              <a
                className="text-primary underline-offset-4 hover:underline"
                href="https://ntfy.sh"
                rel="noreferrer"
                target="_blank"
              >
                ntfy
              </a>{" "}
              app on your phone, subscribe to a topic on {host} with a name only you know, and
              paste the name here. Anyone who knows the name can read what is sent to it. Leave
              it empty for no notifications.
            </>
          }
          id="ntfy-topic"
          label="Your ntfy topic"
          note={
            !topicValid && (
              <p className="text-destructive text-[12.5px] leading-snug">
                Letters, digits, hyphens and underscores only, 64 at most.
              </p>
            )
          }
        >
          <Input
            aria-invalid={!topicValid}
            autoCapitalize="none"
            autoComplete="off"
            autoCorrect="off"
            className="h-9 text-[13.5px] md:text-[13.5px]"
            id="ntfy-topic"
            onChange={(event) => setTopic(event.target.value.replace(/\s+/g, ""))}
            placeholder="foray-matches-x7k2m9qp"
            spellCheck={false}
            value={topic}
          />
        </FormRow>

        <FormRow
          hint={
            start === end
              ? "No quiet hours: a match is sent as soon as it is found."
              : `A match found between these times is sent when they end, on ${clockZone} time. Choose the same time twice for no quiet hours.`
          }
          id="quiet-start"
          label="Quiet hours"
          note={
            zoneMoved &&
            baseline.topic !== "" && (
              <p className="text-muted-foreground text-[12.5px] leading-snug">
                Saved on {initial.timeZone} time. Saving switches to this browser&rsquo;s.
              </p>
            )
          }
        >
          <div className="flex flex-wrap items-center gap-2">
            <ClockSelect id="quiet-start" label="Quiet hours start" onChange={setStart} value={start} />
            <span className="text-muted-foreground text-[13px]">to</span>
            <ClockSelect id="quiet-end" label="Quiet hours end" onChange={setEnd} value={end} />
          </div>
        </FormRow>

        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 px-5 py-4">
          <div className="min-w-0 space-y-1">
            <p className="text-[13.5px] font-medium">Check that it reaches you</p>
            <p className="text-muted-foreground text-[12.5px] leading-snug">
              {baseline.topic === ""
                ? "Save a topic first."
                : edited
                  ? "Save your changes first: the test goes to the saved topic."
                  : "Sends one notification to the saved topic now, quiet hours or not."}
            </p>
          </div>
          <Button disabled={!canTest} onClick={() => void sendTest()} variant="outline">
            {waiting ? <Spinner /> : <Send />}
            {waiting ? "Sending…" : "Send a test notification"}
          </Button>
        </div>

        {test.state !== "idle" && (
          <div className="px-5 py-3.5 text-[13px] leading-relaxed" role="status">
            {test.state === "sent" ? (
              <p className="flex items-center gap-2">
                <Check className="text-success size-4 shrink-0" />
                {test.message}
              </p>
            ) : test.state === "failed" ? (
              <p className="text-destructive">{test.message}</p>
            ) : test.state === "waiting" ? (
              <p className="text-muted-foreground">
                Queued. The worker sends it, and says here how it went.
              </p>
            ) : (
              <p className="text-muted-foreground">
                No answer after 20 seconds. Notifications are sent by the worker: see under
                Worker, further down, whether it is running. The test goes out when it is.
              </p>
            )}
          </div>
        )}
      </fieldset>

      <SaveBar
        message="Unsaved changes to phone notifications."
        onDiscard={discard}
        onSave={save}
        pending={pending}
        saveLabel="Save notifications"
        visible={dirty}
      />
    </div>
  );
}

function ClockSelect({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const items = clockOptions(value).map((option) => ({ value: option, label: clockLabel(option) }));
  return (
    <Select
      items={items}
      onValueChange={(next) => {
        if (typeof next === "string") onChange(next);
      }}
      value={value}
    >
      <SelectTrigger aria-label={label} className="tabular h-9 w-32 text-[13.5px]" id={id}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {items.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            {item.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
