"use client";

import { Play } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTransition } from "react";
import { toast } from "sonner";

import { startSearchAction } from "@/app/actions/runs";
import { Section, SignalDot } from "@/components/foray/primitives";
import { isActiveRun, requestLiveRefresh, useLive } from "@/components/live/live-provider";
import { ApprovalCard } from "@/components/runs/approval-card";
import { RunCard } from "@/components/runs/run-card";
import { Button } from "@/components/ui/button";
import { timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Home's live half: decisions waiting on the user, and runs in flight. Hidden
 * entirely when there is nothing to show — an empty "needs attention" box is
 * noise.
 */
export function AttentionPanel() {
  const { approvals, runs } = useLive();

  const active = Object.values(runs)
    .filter(isActiveRun)
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt));

  // Approvals render inside their run's card; only orphans get their own.
  const activeIds = new Set(active.map((run) => run.id));
  const standalone = approvals.filter((approval) => !activeIds.has(approval.runId));

  if (active.length === 0 && standalone.length === 0) return null;

  return (
    <Section
      description={
        approvals.length > 0
          ? "Foray paused to check with you before spending on these."
          : "These are running in the background. You can leave this page."
      }
      title={approvals.length > 0 ? "Waiting on you" : "In progress"}
    >
      <div className="space-y-3">
        {standalone.map((approval) => (
          <ApprovalCard approval={approval} key={approval.id} />
        ))}
        {active.map((run) => (
          <RunCard key={run.id} runId={run.id} />
        ))}
      </div>
    </Section>
  );
}

/** The last few things the agents did, newest first. */
export function RecentActivity() {
  const { events, ready } = useLive();

  const recent = events
    .filter((event) => event.level !== "debug")
    .slice(-6)
    .reverse();

  return (
    <Section
      action={
        <Link className="text-muted-foreground hover:text-foreground" href="/activity">
          All activity
        </Link>
      }
      title="Latest activity"
    >
      {!ready ? (
        <div className="bg-muted/40 h-24 animate-pulse rounded-lg" />
      ) : recent.length === 0 ? (
        <p className="text-muted-foreground text-[13px]">
          Nothing yet. Searches, reviews and tailored resumes show up here as they happen.
        </p>
      ) : (
        <ol className="space-y-3">
          {recent.map((event) => (
            <li className="flex gap-2.5 text-[13px] leading-snug" key={event.id}>
              <SignalDot
                className="mt-1.5"
                tone={
                  event.level === "error"
                    ? "error"
                    : event.level === "warn" || event.kind === "approval.requested"
                      ? "signal"
                      : "ok"
                }
              />
              <div className="min-w-0">
                <p className="text-foreground/90">{event.message}</p>
                <p className="text-muted-foreground mt-0.5 text-[12px]">{timeAgo(event.createdAt)}</p>
              </div>
            </li>
          ))}
        </ol>
      )}
    </Section>
  );
}

export function StartSearchButton({
  label = "Search now",
  variant = "default",
  className,
}: {
  label?: string;
  variant?: "default" | "outline" | "secondary";
  className?: string;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  return (
    <Button
      className={cn(className)}
      disabled={pending}
      onClick={() =>
        startTransition(async () => {
          const result = await startSearchAction();
          if (!result.ok) {
            toast.error(result.message);
            return;
          }
          toast.success(result.message);
          requestLiveRefresh();
          router.refresh();
        })
      }
      variant={variant}
    >
      <Play />
      {pending ? "Starting…" : label}
    </Button>
  );
}
