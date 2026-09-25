"use client";

import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { refreshAllAction } from "@/app/actions/companies";
import { requestLiveRefresh, useLiveEvent } from "@/components/live/live-provider";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

/** Give up waiting for the worker's report after this long. */
const PATIENCE_MS = 3 * 60 * 1000;

/**
 * Queues a refresh of every board not fetched in the last couple of hours.
 * The worker does the fetching; its report arrives through the live feed.
 */
export function RefreshStaleButton() {
  const [waiting, setWaiting] = useState(false);

  useLiveEvent(["company.refresh.done"], (event) => {
    if (!waiting) return;
    setWaiting(false);
    toast.success(event.message);
  });

  useEffect(() => {
    if (!waiting) return;
    const timer = setTimeout(() => setWaiting(false), PATIENCE_MS);
    return () => clearTimeout(timer);
  }, [waiting]);

  return (
    <Button
      disabled={waiting}
      onClick={async () => {
        setWaiting(true);
        const result = await refreshAllAction();
        if (!result.ok) {
          setWaiting(false);
          toast.error(result.message);
          return;
        }
        toast(result.message);
        if (result.queued === 0) {
          setWaiting(false);
          return;
        }
        requestLiveRefresh();
      }}
      variant="outline"
    >
      {waiting ? <Spinner /> : <RefreshCw />}
      {waiting ? "Refreshing boards…" : "Refresh stale boards"}
    </Button>
  );
}
