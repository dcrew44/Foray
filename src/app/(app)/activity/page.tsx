import { and, eq, inArray, sql } from "drizzle-orm";
import type { Metadata } from "next";

import { ActivityView } from "@/components/activity/activity-view";
import type { RunSummary } from "@/components/activity/run-summary";
import { PageBody, PageHeader } from "@/components/foray/primitives";
import { RefreshOn } from "@/components/live/refresh-on";
import { requireUser } from "@/lib/auth/session";
import { db } from "@/lib/db";
import { getMatchRows } from "@/lib/db/queries/matches";
import { llmCalls, resumes, type Run } from "@/lib/db/schema";
import { describeBrief, parseBrief } from "@/lib/ranking/brief";
import { toLiveRun } from "@/lib/runs/live";
import { listRuns } from "@/lib/runs/runs";

export const metadata: Metadata = { title: "Activity" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Everything the agents did: runs, what each was about and what it cost, and
 * the event log they wrote as they went.
 */
export default async function ActivityPage() {
  const user = await requireUser();
  const runs = await listRuns(user.id, 30);
  const summaries = await describeRuns(user.id, runs);

  return (
    <PageBody>
      <RefreshOn kinds={["run.completed", "run.failed", "run.cancelled", "tailor.done", "render.done"]} />
      <PageHeader
        description="Everything the agents have done for you: searches, deep reviews, tailored resumes and board refreshes, step by step."
        title="Activity"
      />
      <div className="mt-10 max-w-4xl">
        <ActivityView initialRuns={summaries} />
      </div>
    </PageBody>
  );
}

type Input = {
  prompt?: unknown;
  brief?: unknown;
  hints?: unknown;
  matchId?: unknown;
  matchIds?: unknown;
  resumeId?: unknown;
  names?: unknown;
  all?: unknown;
};

/**
 * What each run was about, in words, and what it cost. Runs store ids, not
 * titles, so the matches and resumes they point at are looked up in two
 * batched reads rather than one per row.
 */
async function describeRuns(userId: string, runs: Run[]): Promise<RunSummary[]> {
  if (runs.length === 0) return [];

  const inputs = new Map(runs.map((run) => [run.id, (run.input ?? {}) as Input]));
  const resumeIds = [...inputs.values()]
    .map((input) => input.resumeId)
    .filter((id): id is string => typeof id === "string" && UUID.test(id));

  const [resumeRows, costRows] = await Promise.all([
    resumeIds.length
      ? db
          .select({ id: resumes.id, kind: resumes.kind, jobMatchId: resumes.jobMatchId })
          .from(resumes)
          .where(and(eq(resumes.userId, userId), inArray(resumes.id, resumeIds)))
      : Promise.resolve([]),
    db
      .select({
        runId: llmCalls.runId,
        usd: sql<number>`coalesce(sum(${llmCalls.costUsd}), 0)::float8`,
      })
      .from(llmCalls)
      .where(
        and(
          eq(llmCalls.userId, userId),
          inArray(
            llmCalls.runId,
            runs.map((run) => run.id),
          ),
        ),
      )
      .groupBy(llmCalls.runId),
  ]);

  const resumeById = new Map(resumeRows.map((row) => [row.id, row]));
  const matchIds = new Set<string>();
  for (const input of inputs.values()) {
    if (typeof input.matchId === "string") matchIds.add(input.matchId);
    if (Array.isArray(input.matchIds) && typeof input.matchIds[0] === "string") {
      matchIds.add(input.matchIds[0]);
    }
  }
  for (const row of resumeRows) if (row.jobMatchId) matchIds.add(row.jobMatchId);

  const matchRows = await getMatchRows(
    userId,
    [...matchIds].filter((id) => UUID.test(id)),
  );
  const roleById = new Map(matchRows.map((row) => [row.matchId, `${row.title} at ${row.company}`]));
  const costById = new Map(costRows.map((row) => [row.runId, row.usd]));

  return runs.map((run) => {
    const input = inputs.get(run.id) ?? {};
    return {
      run: toLiveRun(run),
      target: target(run.kind, input, roleById, resumeById),
      costUsd: costById.get(run.id) ?? 0,
      conversationId: run.conversationId,
    };
  });
}

function target(
  kind: Run["kind"],
  input: Input,
  roleById: Map<string, string>,
  resumeById: Map<string, { kind: "base" | "tailored"; jobMatchId: string | null }>,
): string | null {
  switch (kind) {
    case "search":
      return (
        describeBrief(parseBrief(input.brief ?? input.hints)) ??
        (typeof input.prompt === "string" ? input.prompt : null)
      );
    case "rank_deep": {
      const ids = Array.isArray(input.matchIds) ? input.matchIds : [];
      const first = typeof ids[0] === "string" ? roleById.get(ids[0]) : undefined;
      if (ids.length === 0) return null;
      if (ids.length === 1) return first ?? "1 role";
      return first ? `${first} and ${ids.length - 1} more` : `${ids.length} roles`;
    }
    case "tailor":
      return typeof input.matchId === "string" ? (roleById.get(input.matchId) ?? null) : null;
    case "resume_render": {
      const resume = typeof input.resumeId === "string" ? resumeById.get(input.resumeId) : undefined;
      if (!resume) return null;
      if (resume.kind === "base") return "Your base resume";
      const role = resume.jobMatchId ? roleById.get(resume.jobMatchId) : undefined;
      return role ? `Tailored for ${role}` : "A tailored resume";
    }
    case "company_discovery":
      return Array.isArray(input.names)
        ? input.names.filter((name): name is string => typeof name === "string").join(", ")
        : null;
    case "company_refresh":
      return input.all ? "Every active board" : "Boards not fetched lately";
    default:
      return null;
  }
}
