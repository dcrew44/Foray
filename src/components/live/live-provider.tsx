"use client";

import { useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { toast } from "sonner";

import type {
  LiveApproval,
  LiveEvent,
  LiveRun,
  LiveSnapshot,
} from "@/lib/runs/live";
import { announceConversationsChanged } from "@/components/shell/recent-chats";
import { FOLLOW_UP_EVENT } from "@/lib/chat/follow-up-kinds";
import type { WorkerStatus } from "@/lib/system/heartbeat";

/**
 * The app's one connection to background work.
 *
 * A single poll of /api/live feeds the sidebar badges, every run card, every
 * approval card, the activity feed and the toasts, so they always agree. The
 * cadence adapts: quick while something is running or waiting, slow when the
 * app is idle, and paused while the tab is hidden.
 */

type LiveState = {
  ready: boolean;
  events: LiveEvent[];
  runs: Record<string, LiveRun>;
  approvals: LiveApproval[];
  worker: WorkerStatus | null;
};

type Listener = (event: LiveEvent) => void;

type LiveContextValue = LiveState & {
  /** Poll now — call after starting a run or deciding an approval. */
  refresh: () => void;
  subscribe: (listener: Listener) => () => void;
  /** Remember a run fetched outside the snapshot (an old run card). */
  rememberRun: (run: LiveRun) => void;
};

const LiveContext = createContext<LiveContextValue | null>(null);

const FAST_MS = 1500;
const SLOW_MS = 6000;
/** A hidden tab still checks in, just rarely — so it is current when shown. */
const HIDDEN_MS = 30_000;
const MAX_EVENTS = 300;

const ACTIVE = new Set(["queued", "running", "awaiting_approval"]);

export function LiveProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [state, setState] = useState<LiveState>({
    ready: false,
    events: [],
    runs: {},
    approvals: [],
    worker: null,
  });

  const cursor = useRef(0);
  const listeners = useRef(new Set<Listener>());
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlight = useRef(false);
  const first = useRef(true);
  const busy = useRef(false);
  const pollRef = useRef<() => Promise<void>>(async () => {});

  const schedule = useCallback((ms: number) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void pollRef.current(), ms);
  }, []);

  const poll = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;

    try {
      const response = await fetch(`/api/live?since=${cursor.current}`, {
        cache: "no-store",
      });
      if (response.status === 401 || response.redirected) {
        router.refresh();
        return;
      }
      if (!response.ok) return;

      const snapshot = (await response.json()) as LiveSnapshot;
      const initial = first.current;
      first.current = false;
      cursor.current = snapshot.cursor;

      setState((previous) => {
        const runs = { ...previous.runs };
        for (const run of snapshot.runs) runs[run.id] = run;
        const events = snapshot.events.length
          ? [...previous.events, ...snapshot.events].slice(-MAX_EVENTS)
          : previous.events;
        return {
          ready: true,
          events,
          runs,
          approvals: snapshot.approvals,
          worker: snapshot.worker,
        };
      });

      busy.current =
        snapshot.runs.some((run) => ACTIVE.has(run.status)) ||
        snapshot.approvals.length > 0;

      // Only events that happened while the user was here are news. The first
      // snapshot is history and would otherwise toast the last hour at once.
      if (!initial) {
        for (const event of snapshot.events) {
          for (const listener of listeners.current) listener(event);
          announce(event, (href) => router.push(href));
        }
      }
    } catch {
      // A failed poll is retried on the next tick; the UI keeps its last state.
    } finally {
      inFlight.current = false;
      const hidden = typeof document !== "undefined" && document.hidden;
      // While work is in flight a hidden tab still checks every few seconds,
      // so its title and toasts are current the moment the user looks back.
      schedule(
        busy.current ? (hidden ? SLOW_MS : FAST_MS) : hidden ? HIDDEN_MS : SLOW_MS,
      );
    }
  }, [router, schedule]);

  useEffect(() => {
    pollRef.current = poll;
  }, [poll]);

  const refresh = useCallback(() => {
    busy.current = true;
    schedule(0);
  }, [schedule]);

  useEffect(() => {
    void poll();
    const onVisible = () => {
      if (!document.hidden) schedule(0);
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    window.addEventListener("foray:live-refresh", refresh);
    return () => {
      if (timer.current) clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
      window.removeEventListener("foray:live-refresh", refresh);
    };
  }, [poll, refresh, schedule]);

  const subscribe = useCallback((listener: Listener) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  const rememberRun = useCallback((run: LiveRun) => {
    setState((previous) =>
      previous.runs[run.id] ? previous : { ...previous, runs: { ...previous.runs, [run.id]: run } },
    );
  }, []);

  const value = useMemo(
    () => ({ ...state, refresh, subscribe, rememberRun }),
    [state, refresh, subscribe, rememberRun],
  );

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

/**
 * Runs an open chat is showing inline. Their moments are not toasted: the
 * card and the agent's follow-up are already in front of the user.
 */
const watchedRuns = new Map<string, number>();

/** Marks runs as shown in the open chat; the returned function unmarks them. */
export function watchRuns(ids: Iterable<string>): () => void {
  const list = [...ids];
  for (const id of list) watchedRuns.set(id, (watchedRuns.get(id) ?? 0) + 1);
  return () => {
    for (const id of list) {
      const count = (watchedRuns.get(id) ?? 1) - 1;
      if (count > 0) watchedRuns.set(id, count);
      else watchedRuns.delete(id);
    }
  };
}

/** Toasts for the moments worth interrupting for, and nothing else. */
function announce(event: LiveEvent, navigate: (href: string) => void) {
  if (event.runId && watchedRuns.has(event.runId)) return;
  switch (event.kind) {
    case FOLLOW_UP_EVENT: {
      const data = event.data as { conversationId?: string; messageId?: string; label?: string } | null;
      if (!data?.conversationId || !data.messageId) return;
      // The sidebar's recent chats re-sort: this one just moved to the top.
      announceConversationsChanged();
      const href = `/chat/${data.conversationId}`;
      // Someone reading that chat is already watching the reply arrive.
      if (window.location.pathname === href) return;
      toast(`Foray followed up in chat${data.label ? `: ${data.label.toLowerCase()}` : ""}.`, {
        id: `event-${event.id}`,
        action: { label: "Open chat", onClick: () => navigate(href) },
      });
      return;
    }
    case "approval.requested":
      toast(event.message, {
        id: `approval-${event.approvalId ?? event.id}`,
        description: "Review it on Home or in the chat that started it.",
        classNames: { toast: "foray-toast-signal" },
      });
      return;
    case "run.completed":
    case "tailor.done":
    case "render.done":
      toast.success(event.message, { id: `event-${event.id}` });
      return;
    case "run.failed":
      toast.error(event.message, { id: `event-${event.id}` });
      return;
    case "company.discovered":
      toast(event.message, { id: `event-${event.id}` });
      return;
  }
}

export function useLive(): LiveContextValue {
  const value = useContext(LiveContext);
  if (!value) throw new Error("useLive must be used inside <LiveProvider>");
  return value;
}

/** Calls `handler` for each new event of the given kinds. */
export function useLiveEvent(
  kinds: string[] | ((event: LiveEvent) => boolean),
  handler: Listener,
) {
  const { subscribe } = useLive();
  const handlerRef = useRef(handler);
  const kindsRef = useRef(kinds);

  useEffect(() => {
    handlerRef.current = handler;
    kindsRef.current = kinds;
  });

  useEffect(
    () =>
      subscribe((event) => {
        const filter = kindsRef.current;
        const wanted =
          typeof filter === "function"
            ? filter(event)
            : filter.includes(event.kind);
        if (wanted) handlerRef.current(event);
      }),
    [subscribe],
  );
}

/**
 * One run's latest state. Runs in the live snapshot update with it; an older
 * run (a card in a conversation from yesterday) is fetched once.
 */
export function useRun(runId: string | null | undefined): LiveRun | null {
  const { runs, rememberRun } = useLive();
  const known = runId ? runs[runId] : undefined;
  const [fetched, setFetched] = useState<LiveRun | null>(null);

  useEffect(() => {
    if (!runId || known) return;
    let live = true;
    void fetch(`/api/runs/${runId}`, { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : null))
      .then((run: LiveRun | null) => {
        if (!live || !run) return;
        setFetched(run);
        rememberRun(run);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [runId, known, rememberRun]);

  return known ?? fetched;
}

/** Ask the shell's live provider to poll now, from anywhere. */
export function requestLiveRefresh() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event("foray:live-refresh"));
  }
}

export function isActiveRun(run: Pick<LiveRun, "status"> | null | undefined) {
  return Boolean(run && ACTIVE.has(run.status));
}
