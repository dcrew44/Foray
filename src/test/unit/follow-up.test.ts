import { describe, expect, it } from "vitest";

import { describeUpdate } from "@/lib/chat/follow-up";

/**
 * What the agent is told when a run it started pauses, finishes or fails. The
 * wording is the prompt for the follow-up turn, so the parts that steer it —
 * which run, what happened, what to do and not do — are pinned here.
 */

const search = {
  id: "run-1",
  kind: "search" as const,
  input: {
    prompt: "entry-level roles at Anthropic",
    brief: { companies: ["Anthropic"], seniority: "entry" },
  },
  output: { candidates: 20 },
};

describe("the automatic run update", () => {
  it("hands over the roles at the deep-review gate, so the reply does not list them again", () => {
    const update = describeUpdate(
      search,
      { kind: "approval.requested", message: "Waiting for your go-ahead." },
      { kind: "deep_eval_batch", title: "Deep-evaluate 8 roles (about $0.25)", payload: { items: [] } },
      [
        {
          title: "Software Engineer, Research Data Platform",
          company: "Anthropic",
          score: 78,
          headline: "Strong data platform overlap",
          locations: "San Francisco, CA",
        },
      ],
    );
    expect(update.label).toBe("Search paused for your go-ahead");
    expect(update.text).toContain("[Automatic update about run run-1, not written by the user]");
    expect(update.text).toContain("Entry-level roles at Anthropic");
    expect(update.text).toContain(
      "- Software Engineer, Research Data Platform at Anthropic (78), San Francisco, CA: Strong data platform overlap",
    );
    expect(update.text).toContain("do not call list_jobs");
    expect(update.text).toContain("Do not answer the approval yourself");
    expect(update.text).toContain("can only look things up");
  });

  it("names the roles a shortlist proposal covers", () => {
    const update = describeUpdate(
      search,
      { kind: "approval.requested", message: "" },
      {
        kind: "shortlist_proposal",
        title: "Shortlist 2 roles",
        payload: { items: [{ title: "Software Engineer", company: "Anthropic" }, { title: "Fellow", company: "Anthropic" }] },
      },
    );
    expect(update.label).toBe("Deep review done");
    // Without match details, the payload's own titles are used.
    expect(update.text).toContain("- Software Engineer at Anthropic\n- Fellow at Anthropic");
    expect(update.text).toContain("do not call list_jobs");
  });

  it("shows results for a finished search, and suggests widening an empty one", () => {
    const found = describeUpdate(search, { kind: "run.completed", message: "Found 20 roles." }, null);
    expect(found.label).toBe("Search finished");
    expect(found.text).toContain("limit 5");

    const empty = describeUpdate(
      { ...search, output: { candidates: 0 } },
      { kind: "run.completed", message: "No open roles matched." },
      null,
    );
    expect(empty.text).toContain("widen the search");
    expect(empty.text).not.toContain("list_jobs");
  });

  it("explains failures and offers another try", () => {
    const update = describeUpdate(
      { id: "run-2", kind: "tailor", input: { prompt: "Tailor your resume for Engineer at Stripe" }, output: null },
      { kind: "tailor.failed", message: "The model timed out." },
      null,
    );
    expect(update.label).toBe("Tailoring stopped");
    expect(update.text).toContain("Tailoring your resume for Engineer at Stripe stopped with an error: The model timed out.");
    expect(update.text).toContain("offer to try again");
  });

  it("reports a finished tailored resume", () => {
    const update = describeUpdate(
      { id: "run-3", kind: "tailor", input: { prompt: "Tailor your resume for Engineer at Stripe" }, output: null },
      { kind: "tailor.done", message: "Your resume is ready." },
      null,
    );
    expect(update.label).toBe("Tailored resume ready");
    expect(update.text).toContain("Tailoring your resume for Engineer at Stripe is done.");
  });
});
