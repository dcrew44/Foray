"use client";

import { useCallback, useEffect, useRef, useState, useTransition } from "react";

import { pingWorker } from "@/app/actions/ping";
import { Button } from "@/components/ui/button";

type FeedEvent = {
  id: number;
  level: "debug" | "info" | "warn" | "error";
  kind: string;
  message: string;
  createdAt: string;
  data: unknown;
};

const POLL_MS = 2000;

const LEVEL_CLASS: Record<FeedEvent["level"], string> = {
  debug: "text-muted-foreground",
  info: "text-foreground",
  warn: "text-amber-600 dark:text-amber-500",
  error: "text-destructive",
};

export function ActivityFeed() {
  const [events, setEvents] = useState<FeedEvent[]>([]);
  const [pending, startTransition] = useTransition();
  const cursor = useRef(0);

  const poll = useCallback(async () => {
    const response = await fetch(`/api/events?since=${cursor.current}`, {
      cache: "no-store",
    });
    if (!response.ok) return;

    const body = (await response.json()) as {
      events: FeedEvent[];
      cursor: number;
    };
    cursor.current = body.cursor;
    if (body.events.length) {
      setEvents((previous) => [...body.events.reverse(), ...previous]);
    }
  }, []);

  useEffect(() => {
    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [poll]);

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Button
          disabled={pending}
          onClick={() =>
            startTransition(async () => {
              await pingWorker();
              await poll();
            })
          }
        >
          {pending ? "Pinging…" : "Ping worker"}
        </Button>
        <span className="text-muted-foreground text-sm">
          Polling every {POLL_MS / 1000}s
        </span>
      </div>

      {events.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          Nothing yet. Ping the worker to check it is running.
        </p>
      ) : (
        <ul className="divide-border divide-y text-sm">
          {events.map((event) => (
            <li className="flex gap-4 py-2" key={event.id}>
              <time className="text-muted-foreground shrink-0 tabular-nums">
                {new Date(event.createdAt).toLocaleTimeString()}
              </time>
              <code className="text-muted-foreground shrink-0">
                {event.kind}
              </code>
              <span className={LEVEL_CLASS[event.level]}>{event.message}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
