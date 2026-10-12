import type { Metadata } from "next";

import { ActivityView } from "@/components/activity/activity-view";
import { PageBody, PageHeader } from "@/components/foray/primitives";
import { RefreshOn } from "@/components/live/refresh-on";
import type { components } from "@/lib/api/schema";
import { django } from "@/lib/api/server";

export const metadata: Metadata = { title: "Activity" };

type ActivityPageData = components["schemas"]["ActivityPage"];

/**
 * Everything the agents did: runs, what each was about and what it cost, and
 * the event log they wrote as they went.
 *
 * One read (`GET /api/pages/activity`): the last 30 runs, each with its `target` and `costUsd`
 * worked out by Django. The live feed keeps them current from there.
 */
export default async function ActivityPage() {
  const { runs } = await django<ActivityPageData>("/api/pages/activity");

  return (
    <PageBody>
      {/* Every kind of run ends with one of these three, tailoring and rendering included. */}
      <RefreshOn kinds={["run.completed", "run.failed", "run.cancelled"]} />
      <PageHeader
        description="Everything the agents have done for you: searches, deep reviews, tailored resumes and board refreshes, step by step."
        title="Activity"
      />
      <div className="mt-10 max-w-4xl">
        <ActivityView initialRuns={runs} />
      </div>
    </PageBody>
  );
}
