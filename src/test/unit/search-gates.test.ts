import { randomUUID } from "node:crypto";

import { and, eq, inArray } from "drizzle-orm";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * A search run's life after scoring — the deep-evaluation gate, its
 * continuation, the shortlist gate and its continuation — plus on-demand
 * evaluation, against the real database with a throwaway user.
 *
 * Two things are faked: the queue (a live worker must not pick these runs up;
 * the tests call each continuation by hand with the payload that would have
 * been enqueued), and the model call inside `evaluateMatch`, which here scores
 * a match from its fast score so the outcome is predictable and free.
 */

const fake = vi.hoisted(() => ({
  failing: new Set<string>(),
  evaluated: [] as string[],
}));

vi.mock("@/lib/queue/send", () => ({ send: vi.fn(async () => "job-id") }));

vi.mock("@/lib/ranking/deep-eval", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/ranking/deep-eval")>();
  const { getMatchRows, saveDeepEval } = await import("@/lib/db/queries/matches");

  return {
    ...actual,
    loadEvalCandidate: vi.fn(async () => ({
      resumeId: "resume-1",
      resumeVersion: 1,
      resumeText: "[work-1] Backend Engineer — Initech",
      preferences: {
        target_roles: ["Backend engineer"],
        seniority: "mid",
        locations: ["San Francisco, CA"],
        remote_pref: "any",
        salary_floor: null,
        work_auth: "US citizen",
        start_date: null,
        must_haves: null,
        dealbreakers: null,
      },
      factIds: new Set(["work-1"]),
    })),
    evaluateMatch: vi.fn(async ({ userId, matchId }: { userId: string; matchId: string }) => {
      const [row] = await getMatchRows(userId, [matchId]);
      if (!row) throw new Error("That match does not exist.");
      if (fake.failing.has(matchId)) throw new Error("model timed out");

      const score = row.fastScore ?? 50;
      const evaluation = {
        matched_requirements: [],
        gaps: [],
        seniority_fit: "match" as const,
        location_fit: "match" as const,
        salary_fit: "unstated" as const,
        risk_flags: [],
        talking_points: [],
        summary: `Why ${row.title} fits.`,
        score,
        recommendation:
          score >= 85
            ? ("strong" as const)
            : score >= 70
              ? ("apply" as const)
              : score >= 50
                ? ("maybe" as const)
                : ("skip" as const),
      };
      await saveDeepEval(userId, matchId, evaluation);
      fake.evaluated.push(matchId);
      return { matchId, jobId: row.jobId, title: row.title, company: row.company, evaluation };
    }),
  };
});

import { db } from "@/lib/db";
import {
  agentEvents,
  approvals,
  jobMatches,
  jobs,
  resumes,
  runs,
  user,
  type Run,
} from "@/lib/db/schema";
import {
  getMatchRows,
  isShortlistCandidate,
  listShortlistCandidates,
  moveMatchesFromStage,
  selectForDeepEval,
  topMatchIdsForJobs,
  type Stage,
} from "@/lib/db/queries/matches";
import { DEFAULT_SETTINGS, type Settings } from "@/lib/profile/settings";
import type { SearchContinuationPayload, MatchEvaluatePayload } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";
import { NoResumeError } from "@/lib/ranking/deep-eval";
import type { DeepEval } from "@/lib/ranking/schemas";
import { decideApproval, type DeepEvalBatchPayload, type ShortlistPayload } from "@/lib/runs/approvals";
import {
  continueSearchEvaluate,
  continueSearchShortlist,
  describeEvaluations,
  executeMatchEvaluate,
  startMatchEvaluation,
  summarizeSearchRun,
} from "@/lib/runs/evaluate-run";
import { createRun, requestCancel } from "@/lib/runs/runs";
import { gateDeepEvaluation } from "@/lib/runs/search-run";

const userId = `test-gates-${randomUUID()}`;
const sent = vi.mocked(send);

/** Real, active postings, only ever read — the tests' matches point at them. */
let pool: Array<{ id: string; title: string }> = [];
let next = 0;

function takeJobs(n: number) {
  const taken = pool.slice(next, next + n);
  next += n;
  if (taken.length < n) throw new Error("Not enough active jobs in the dev database");
  return taken;
}

beforeAll(async () => {
  await db
    .insert(user)
    .values({ id: userId, name: "Gates test", email: `${userId}@example.test` });

  pool = await db
    .select({ id: jobs.id, title: jobs.title })
    .from(jobs)
    .where(eq(jobs.isActive, true))
    .limit(60);
});

afterAll(async () => {
  await db.delete(user).where(eq(user.id, userId));
});

beforeEach(async () => {
  sent.mockClear();
  fake.failing.clear();
  fake.evaluated.length = 0;
  // A clean board per test, so one test's leftovers never muddy the next's
  // counts.
  await db.delete(jobMatches).where(eq(jobMatches.userId, userId));
});

type Seed = {
  stage?: Stage;
  fastScore?: number | null;
  deepEval?: Partial<DeepEval> | null;
};

/** One match per seed, each on its own posting. Returns match ids in seed order. */
async function seed(seeds: Seed[]) {
  const postings = takeJobs(seeds.length);
  const rows = await db
    .insert(jobMatches)
    .values(
      seeds.map((s, index) => ({
        userId,
        jobId: postings[index].id,
        stage: s.stage ?? "found",
        fastScore: s.fastScore ?? null,
        deepScore: s.deepEval?.score ?? null,
        deepEval: s.deepEval ?? null,
      })),
    )
    .returning({ id: jobMatches.id, jobId: jobMatches.jobId });
  return {
    matchIds: rows.map((row) => row.id),
    jobIds: rows.map((row) => row.jobId),
  };
}

async function searchRun(
  mode: Run["mode"],
  jobIds: string[],
  stats: Record<string, unknown> = {},
) {
  return createRun({
    userId,
    kind: "search",
    status: "running",
    mode,
    input: { candidateJobIds: jobIds },
    stats: { phase: "scoring", ...stats },
  });
}

async function runRow(id: string) {
  const [row] = await db.select().from(runs).where(eq(runs.id, id));
  return row;
}

async function approvalsFor(runId: string) {
  return db
    .select()
    .from(approvals)
    .where(eq(approvals.runId, runId))
    .orderBy(approvals.createdAt);
}

async function eventsFor(runId: string) {
  return db
    .select()
    .from(agentEvents)
    .where(eq(agentEvents.runId, runId))
    .orderBy(agentEvents.id);
}

async function stageOf(matchIds: string[]) {
  const rows = await db
    .select({ id: jobMatches.id, stage: jobMatches.stage })
    .from(jobMatches)
    .where(inArray(jobMatches.id, matchIds));
  const byId = new Map(rows.map((row) => [row.id, row.stage]));
  return matchIds.map((id) => byId.get(id));
}

/** The payload the last `send` would have enqueued for this queue. */
function lastSent(queue: string): SearchContinuationPayload {
  const call = [...sent.mock.calls].reverse().find(([name]) => name === queue);
  if (!call) throw new Error(`nothing was sent to ${queue}`);
  return call[1] as SearchContinuationPayload;
}

const settings = (patch: Partial<Settings> = {}): Settings => ({
  ...DEFAULT_SETTINGS,
  scoreThreshold: 55,
  deepEvalTopN: 3,
  ...patch,
});

describe("selecting what to evaluate", () => {
  it("takes this run's open, unevaluated matches at or above the threshold, and nothing else", async () => {
    const { matchIds, jobIds } = await seed([
      { fastScore: 90 }, // 0: selected
      { stage: "shortlisted", fastScore: 70 }, // 1: selected
      { fastScore: 40 }, // 2: below threshold
      { fastScore: 95, deepEval: { score: 90, recommendation: "strong", summary: "x" } }, // 3: already evaluated
      { stage: "archived", fastScore: 99 }, // 4: closed
      { fastScore: 60 }, // 5: selected
      { fastScore: null }, // 6: unscored
      { fastScore: 98 }, // 7: not this run's: from an earlier search
    ]);
    const runJobIds = jobIds.slice(0, 7);

    const all = await selectForDeepEval(userId, {
      jobIds: runJobIds,
      minFastScore: 55,
      limit: 10,
    });
    expect(all.map((row) => row.matchId)).toEqual([matchIds[0], matchIds[1], matchIds[5]]);
    expect(all[0].descriptionChars).toBeGreaterThanOrEqual(0);

    const top = await selectForDeepEval(userId, { jobIds: runJobIds, minFastScore: 55, limit: 1 });
    expect(top.map((row) => row.matchId)).toEqual([matchIds[0]]);

    expect(await selectForDeepEval(userId, { jobIds: runJobIds, minFastScore: 55, limit: 0 })).toEqual([]);
    expect(await selectForDeepEval(userId, { jobIds: [], minFastScore: 55, limit: 10 })).toEqual([]);

    // Clear these so later selections in this file start clean.
    await db.delete(jobMatches).where(inArray(jobMatches.id, matchIds));
  });
});

describe("the shortlist rule", () => {
  it("proposes found matches the evaluation recommends, and nothing the user already moved", () => {
    expect(isShortlistCandidate({ stage: "found", recommendation: "strong" })).toBe(true);
    expect(isShortlistCandidate({ stage: "found", recommendation: "apply" })).toBe(true);
    expect(isShortlistCandidate({ stage: "found", recommendation: "maybe" })).toBe(false);
    expect(isShortlistCandidate({ stage: "found", recommendation: "skip" })).toBe(false);
    expect(isShortlistCandidate({ stage: "found", recommendation: null })).toBe(false);
    expect(isShortlistCandidate({ stage: "shortlisted", recommendation: "strong" })).toBe(false);
    expect(isShortlistCandidate({ stage: "applied", recommendation: "strong" })).toBe(false);
  });

  it("reads the same from the database, best first", async () => {
    const { matchIds } = await seed([
      { deepEval: { score: 75, recommendation: "apply", summary: "a" } },
      { deepEval: { score: 91, recommendation: "strong", summary: "b" } },
      { deepEval: { score: 60, recommendation: "maybe", summary: "c" } },
      { stage: "shortlisted", deepEval: { score: 95, recommendation: "strong", summary: "d" } },
      { fastScore: 88 },
    ]);

    const picks = await listShortlistCandidates(userId, matchIds);
    expect(picks.map((row) => row.matchId)).toEqual([matchIds[1], matchIds[0]]);
    expect(picks[0].headline).toBe("b");

    // Moving is conditional on the stage, so a match moved meanwhile stays put.
    await db
      .update(jobMatches)
      .set({ stage: "applied" })
      .where(eq(jobMatches.id, matchIds[0]));
    const moved = await moveMatchesFromStage(userId, [matchIds[0], matchIds[1]], "found", "shortlisted");
    expect(moved).toEqual([matchIds[1]]);
    expect(await stageOf([matchIds[0], matchIds[1]])).toEqual(["applied", "shortlisted"]);

    await db.delete(jobMatches).where(inArray(jobMatches.id, matchIds));
  });
});

describe("a search run in approve_each", () => {
  it("asks, evaluates the approved subset, proposes a shortlist, shortlists what was approved, completes", async () => {
    const { matchIds, jobIds } = await seed([
      { fastScore: 92 }, // 0 → strong
      { fastScore: 78 }, // 1 → apply
      { fastScore: 64 }, // 2 → not in the top 3 once 4 is counted
      { stage: "archived", fastScore: 40 }, // 3
      { stage: "shortlisted", fastScore: 88 }, // 4 → selected, then left out by the user
    ]);
    const run = await searchRun("approve_each", jobIds, {
      candidates: 5,
      newMatches: 5,
      scored: 5,
      autoArchived: 1,
    });

    // Gate 1.
    await gateDeepEvaluation(run, settings(), jobIds);

    const [gate1] = await approvalsFor(run.id);
    expect(gate1.kind).toBe("deep_eval_batch");
    expect(gate1.status).toBe("pending");
    expect(gate1.title).toMatch(/^Deep-evaluate 3 roles \((about \$\d+\.\d{2}|under a cent)\)$/);

    const batch = gate1.payload as DeepEvalBatchPayload;
    expect(batch.items.map((item) => item.matchId)).toEqual([matchIds[0], matchIds[4], matchIds[1]]);
    expect(batch.items[0]).toMatchObject({ jobId: jobIds[0], fastScore: 92 });
    expect(batch.estimate.model).toBe("openai:gpt-5.6-terra");
    expect(batch.estimate.usd).toBeGreaterThan(0);
    expect(batch.estimate.usd).toBeCloseTo(batch.estimate.perItemUsd! * 3, 3);
    expect(batch.note).toBeNull();

    let paused = await runRow(run.id);
    expect(paused.status).toBe("awaiting_approval");
    expect(paused.stats).toMatchObject({ phase: "awaiting_evaluation", toEvaluate: 3 });
    expect(sent).not.toHaveBeenCalled();

    const requested = (await eventsFor(run.id)).find((e) => e.kind === "approval.requested");
    expect(requested?.approvalId).toBe(gate1.id);
    expect(requested?.message).toMatch(/^Waiting for your go-ahead to deep-evaluate 3 roles/);

    // The user leaves out the one they had already shortlisted.
    const decided = await decideApproval(
      userId,
      gate1.id,
      { approved: true, itemIds: [matchIds[0], matchIds[1]] },
      "ui",
    );
    expect(decided.ok).toBe(true);

    const evaluatePayload = lastSent("search.evaluate");
    expect(evaluatePayload).toEqual({ userId, runId: run.id, approvalId: gate1.id });
    await continueSearchEvaluate(evaluatePayload);

    expect(fake.evaluated.sort()).toEqual([matchIds[0], matchIds[1]].sort());

    // Gate 2.
    const [, gate2] = await approvalsFor(run.id);
    expect(gate2.kind).toBe("shortlist_proposal");
    expect(gate2.status).toBe("pending");
    expect(gate2.title).toBe("Shortlist 2 roles");
    const proposal = gate2.payload as ShortlistPayload;
    expect(proposal.items.map((item) => [item.matchId, item.recommendation, item.deepScore])).toEqual([
      [matchIds[0], "strong", 92],
      [matchIds[1], "apply", 78],
    ]);
    expect(proposal.items[0].summary).toMatch(/^Why /);

    paused = await runRow(run.id);
    expect(paused.status).toBe("awaiting_approval");
    expect(paused.stats).toMatchObject({
      phase: "awaiting_shortlist",
      evaluated: 2,
      evaluationFailed: 0,
      recommended: 2,
      proposedShortlist: 2,
    });

    // A repeat delivery of the first continuation changes nothing.
    await continueSearchEvaluate(evaluatePayload);
    expect(await approvalsFor(run.id)).toHaveLength(2);

    await decideApproval(userId, gate2.id, { approved: true, itemIds: [matchIds[0]] }, "chat");
    await continueSearchShortlist(lastSent("search.shortlist"));

    expect(await stageOf([matchIds[0], matchIds[1], matchIds[4]])).toEqual([
      "shortlisted",
      "found",
      "shortlisted",
    ]);

    const done = await runRow(run.id);
    expect(done.status).toBe("completed");
    expect(done.finishedAt).not.toBeNull();
    expect(done.stats).toMatchObject({ phase: "done", shortlisted: 1 });
    expect(done.output).toEqual({
      candidates: 5,
      newMatches: 5,
      scored: 5,
      autoArchived: 1,
      evaluated: 2,
      shortlisted: 1,
      // Deep score where there is one, else fast; closed matches excluded.
      topMatchIds: [matchIds[0], matchIds[4], matchIds[1], matchIds[2]],
    });

    const kinds = (await eventsFor(run.id)).map((event) => event.kind);
    expect(kinds).toEqual([
      "approval.requested",
      "approval.decided",
      "evaluation.start",
      "evaluation.progress",
      "evaluation.done",
      "approval.requested",
      "approval.decided",
      "shortlist.done",
      "run.completed",
    ]);
    const completed = (await eventsFor(run.id)).at(-1);
    expect(completed?.message).toBe(
      "Found 5 roles, scored 5, deep-evaluated 2. 2 look worth applying to; 1 shortlisted.",
    );
  });

  it("completes with nothing evaluated when the gate is declined", async () => {
    const { jobIds } = await seed([{ fastScore: 90 }, { fastScore: 80 }]);
    const run = await searchRun("approve_each", jobIds, { candidates: 2, newMatches: 2, scored: 2 });

    await gateDeepEvaluation(run, settings(), jobIds);
    const [gate] = await approvalsFor(run.id);
    await decideApproval(userId, gate.id, { approved: false }, "ui");
    await continueSearchEvaluate(lastSent("search.evaluate"));

    expect(fake.evaluated).toEqual([]);
    const done = await runRow(run.id);
    expect(done.status).toBe("completed");
    expect(done.stats).toMatchObject({ phase: "done", evaluated: 0 });
    expect(done.output).toMatchObject({ evaluated: 0, shortlisted: 0 });

    const kinds = (await eventsFor(run.id)).map((event) => event.kind);
    expect(kinds).toEqual([
      "approval.requested",
      "approval.decided",
      "evaluation.skipped",
      "run.completed",
    ]);
  });

  it("does nothing for a run cancelled while it waited", async () => {
    const { jobIds } = await seed([{ fastScore: 90 }]);
    const run = await searchRun("approve_each", jobIds);

    await gateDeepEvaluation(run, settings(), jobIds);
    const [gate] = await approvalsFor(run.id);

    await requestCancel(userId, run.id);
    await continueSearchEvaluate({ userId, runId: run.id, approvalId: gate.id });

    expect(fake.evaluated).toEqual([]);
    expect((await runRow(run.id)).status).toBe("cancelled");
    expect((await approvalsFor(run.id))[0].status).toBe("expired");
  });

  it("keeps going past a failed evaluation, and fails the run when every one fails", async () => {
    const partial = await seed([{ fastScore: 90 }, { fastScore: 86 }]);
    const run = await searchRun("approve_each", partial.jobIds);
    fake.failing.add(partial.matchIds[1]);

    await gateDeepEvaluation(run, settings(), partial.jobIds);
    const [gate] = await approvalsFor(run.id);
    await decideApproval(userId, gate.id, { approved: true }, "ui");
    await continueSearchEvaluate(lastSent("search.evaluate"));

    expect((await runRow(run.id)).stats).toMatchObject({ evaluated: 1, evaluationFailed: 1 });
    expect((await eventsFor(run.id)).map((e) => e.kind)).toContain("evaluation.failed");
    // Still reaches the shortlist gate with the one that worked.
    const [, proposal] = await approvalsFor(run.id);
    expect((proposal.payload as ShortlistPayload).items.map((i) => i.matchId)).toEqual([
      partial.matchIds[0],
    ]);

    const doomed = await seed([{ fastScore: 90 }]);
    const failing = await searchRun("approve_each", doomed.jobIds);
    fake.failing.add(doomed.matchIds[0]);

    await gateDeepEvaluation(failing, settings(), doomed.jobIds);
    const [failingGate] = await approvalsFor(failing.id);
    await decideApproval(userId, failingGate.id, { approved: true }, "ui");
    await continueSearchEvaluate(lastSent("search.evaluate"));

    const failed = await runRow(failing.id);
    expect(failed.status).toBe("failed");
    expect(failed.error).toBe("Deep evaluation failed for every role: model timed out");
    expect((await eventsFor(failing.id)).at(-1)?.kind).toBe("run.failed");
  });
});

describe("a search run on autopilot", () => {
  it("evaluates and shortlists without stopping", async () => {
    const { matchIds, jobIds } = await seed([
      { fastScore: 91 }, // strong
      { fastScore: 72 }, // apply
      { fastScore: 58 }, // maybe
    ]);
    const run = await searchRun("autopilot", jobIds, { candidates: 3, newMatches: 3, scored: 3 });

    await gateDeepEvaluation(run, settings(), jobIds);

    const [gate1] = await approvalsFor(run.id);
    expect(gate1.status).toBe("auto_approved");
    expect(gate1.decidedVia).toBe("autopilot");
    const running = await runRow(run.id);
    expect(running.status).toBe("running");
    expect(running.stats).toMatchObject({ phase: "evaluating", toEvaluate: 3 });

    await continueSearchEvaluate(lastSent("search.evaluate"));

    const [, gate2] = await approvalsFor(run.id);
    expect(gate2.kind).toBe("shortlist_proposal");
    expect(gate2.status).toBe("auto_approved");

    await continueSearchShortlist(lastSent("search.shortlist"));

    expect(await stageOf(matchIds)).toEqual(["shortlisted", "shortlisted", "found"]);
    const done = await runRow(run.id);
    expect(done.status).toBe("completed");
    expect(done.output).toMatchObject({ evaluated: 3, shortlisted: 2 });

    const kinds = (await eventsFor(run.id)).map((event) => event.kind);
    expect(kinds).not.toContain("approval.requested");
    expect(kinds.filter((kind) => kind === "approval.auto")).toHaveLength(2);
  });

  it("completes straight away when there is nothing to evaluate", async () => {
    const { jobIds } = await seed([{ fastScore: 30 }, { stage: "archived", fastScore: 20 }]);
    const run = await searchRun("autopilot", jobIds, {
      candidates: 2,
      newMatches: 2,
      scored: 2,
      autoArchived: 1,
    });

    await gateDeepEvaluation(run, settings(), jobIds);

    expect(await approvalsFor(run.id)).toEqual([]);
    expect(sent).not.toHaveBeenCalled();
    const done = await runRow(run.id);
    expect(done.status).toBe("completed");
    expect(done.output).toMatchObject({ evaluated: 0, topMatchIds: expect.any(Array) });
    expect((await eventsFor(run.id)).at(-1)?.message).toBe(
      "Found 2 roles, scored 2. 1 was archived as too weak.",
    );
  });

  it("skips deep evaluation entirely when it is turned off", async () => {
    const { jobIds } = await seed([{ fastScore: 95 }]);
    const run = await searchRun("autopilot", jobIds, { candidates: 1, newMatches: 1, scored: 1 });

    await gateDeepEvaluation(run, settings({ deepEvalTopN: 0 }), jobIds);

    expect(await approvalsFor(run.id)).toEqual([]);
    expect((await runRow(run.id)).status).toBe("completed");
  });
});

describe("evaluation on demand", () => {
  it("refuses without a resume, then evaluates, reuses an in-flight run, and names the role", async () => {
    const { matchIds } = await seed([{ fastScore: 92 }]);

    await expect(startMatchEvaluation(userId, matchIds)).rejects.toBeInstanceOf(NoResumeError);

    await db.insert(resumes).values({
      userId,
      kind: "base",
      version: 1,
      data: { basics: { name: "Gates test", links: [] } },
    });

    const first = await startMatchEvaluation(userId, matchIds);
    expect(first.reused).toBe(false);
    expect(first.run.kind).toBe("rank_deep");
    expect(first.run.input).toEqual({ matchIds });

    // Asking again while it is queued returns the same run rather than paying twice.
    const again = await startMatchEvaluation(userId, matchIds);
    expect(again.reused).toBe(true);
    expect(again.run.id).toBe(first.run.id);
    expect(sent).toHaveBeenCalledTimes(1);

    const payload = sent.mock.calls[0][1] as MatchEvaluatePayload;
    expect(payload).toEqual({ userId, runId: first.run.id, matchIds });

    await executeMatchEvaluate(payload);

    const done = await runRow(first.run.id);
    expect(done.status).toBe("completed");
    expect(done.output).toEqual({ evaluated: 1, matchIds });
    expect(done.stats).toMatchObject({ phase: "done", evaluated: 1, evaluationFailed: 0 });

    const [row] = await getMatchRows(userId, matchIds);
    expect(row.deepScore).toBe(92);
    expect(row.recommendation).toBe("strong");

    const completed = (await eventsFor(first.run.id)).at(-1);
    expect(completed?.kind).toBe("run.completed");
    expect(completed?.message).toBe(`Evaluated ${row.title} at ${row.company}: 92, a strong match.`);

    // A repeat delivery does not run it twice.
    await executeMatchEvaluate(payload);
    expect(fake.evaluated).toEqual(matchIds);
  });

  it("ignores matches that are not this user's", async () => {
    await expect(startMatchEvaluation(userId, [randomUUID(), "junk"])).rejects.toThrow(
      "There is no such match to evaluate.",
    );
  });
});

describe("wording", () => {
  it("summarises a run in plain English", () => {
    const base = {
      candidates: 60,
      newMatches: 60,
      scored: 60,
      autoArchived: 20,
      evaluated: 12,
      shortlisted: 4,
      topMatchIds: [],
      recommended: 5,
      strong: 0,
      good: 0,
    };
    expect(summarizeSearchRun(base)).toBe(
      "Found 60 roles, scored 60, deep-evaluated 12. 5 look worth applying to; 4 shortlisted.",
    );
    expect(summarizeSearchRun({ ...base, newMatches: 3, scored: 3, shortlisted: 0, recommended: 1 })).toBe(
      "Found 60 roles (3 new), scored 3, deep-evaluated 12. 1 looks worth applying to.",
    );
    expect(summarizeSearchRun({ ...base, recommended: 0, shortlisted: 0 })).toBe(
      "Found 60 roles, scored 60, deep-evaluated 12. None looked worth applying to.",
    );
    expect(summarizeSearchRun({ ...base, evaluated: 0, strong: 8, good: 12 })).toBe(
      "Found 60 roles, scored 60. 8 look strong, 12 are worth a look, 20 were archived as too weak.",
    );
    expect(summarizeSearchRun({ ...base, candidates: 0 })).toBe(
      "No open roles matched your criteria this time.",
    );
    // A repeat search: nothing new, nothing rescored.
    expect(
      summarizeSearchRun({ ...base, candidates: 12, newMatches: 0, scored: 0, evaluated: 1, recommended: 1, shortlisted: 1 }),
    ).toBe("Found 12 roles (none new), deep-evaluated 1. 1 looks worth applying to; 1 shortlisted.");
    expect(
      summarizeSearchRun({ ...base, candidates: 12, newMatches: 0, scored: 0, evaluated: 0, autoArchived: 0, strong: 1, good: 0 }),
    ).toBe("Found 12 roles (none new). 1 looks strong.");
  });

  it("names the roles an on-demand evaluation covered", () => {
    const item = (title: string, score: number, recommendation: DeepEval["recommendation"]) => ({
      matchId: title,
      jobId: title,
      title,
      company: "Stripe",
      evaluation: { score, recommendation } as DeepEval,
    });

    expect(describeEvaluations([item("Backend Engineer", 82, "apply")])).toBe(
      "Evaluated Backend Engineer at Stripe: 82, worth applying.",
    );
    expect(
      describeEvaluations(
        [
          item("A", 40, "skip"),
          item("B", 90, "strong"),
          item("C", 60, "maybe"),
          item("D", 75, "apply"),
        ],
        1,
      ),
    ).toBe(
      "Evaluated 4 roles: B at Stripe (90, a strong match); D at Stripe (75, worth applying); " +
        "C at Stripe (60, a maybe); and 1 more. 1 could not be evaluated.",
    );
  });
});

describe("topMatchIdsForJobs", () => {
  it("ranks open matches by deep score, else fast score", async () => {
    const { matchIds, jobIds } = await seed([
      { fastScore: 70 },
      { fastScore: 60, deepEval: { score: 95, recommendation: "strong", summary: "x" } },
      { stage: "rejected", fastScore: 99 },
      { fastScore: null },
    ]);

    expect(await topMatchIdsForJobs(userId, jobIds, 5)).toEqual([matchIds[1], matchIds[0]]);
    expect(await topMatchIdsForJobs(userId, jobIds, 1)).toEqual([matchIds[1]]);
    expect(await topMatchIdsForJobs(userId, [], 5)).toEqual([]);

    await db.delete(jobMatches).where(and(eq(jobMatches.userId, userId), inArray(jobMatches.id, matchIds)));
  });
});
