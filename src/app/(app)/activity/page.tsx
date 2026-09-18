import { ActivityFeed } from "@/components/activity/activity-feed";
import { requireUser } from "@/lib/auth/session";

export default async function ActivityPage() {
  await requireUser();

  return (
    <main className="mx-auto w-full max-w-3xl space-y-8 p-8">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Activity</h1>
        <p className="text-muted-foreground text-sm">
          Everything the agents do, as they do it.
        </p>
      </div>
      <ActivityFeed />
    </main>
  );
}
