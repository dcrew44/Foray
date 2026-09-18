import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  atsFromApplyOptions,
  employerSlug,
  normalizeJSearchJob,
  type JSearchJob,
} from "@/lib/sources/jsearch";

const response = JSON.parse(
  readFileSync("src/test/fixtures/jsearch/entry-level-sf.json", "utf8"),
) as { data: { jobs: JSearchJob[]; cursor: string } };

const raw = response.data.jobs;
const jobs = raw.map(normalizeJSearchJob);

describe("jsearch envelope", () => {
  it("carries a cursor, not a page number", () => {
    // v5 replaced page/num_pages paging with an opaque cursor; a run budget
    // built around page counts would silently stop after one page.
    expect(typeof response.data.cursor).toBe("string");
    expect(response.data.cursor.length).toBeGreaterThan(0);
  });
});

describe("normalizeJSearchJob", () => {
  it("builds an id from the employer, since aggregator rows have no slug", () => {
    for (const job of jobs) {
      expect(job.id.startsWith("jsearch:")).toBe(true);
      expect(job.id.split(":")).toHaveLength(3);
      expect(job.source).toBe("jsearch");
    }
  });

  it("keeps the description exactly as sent", () => {
    expect(jobs[0].descriptionRaw).toBe(raw[0].job_description);
  });

  it("still strips trailing boilerplate from plain text", () => {
    for (const job of jobs) {
      if (!job.descriptionText) continue;
      expect(job.descriptionText.length).toBeLessThanOrEqual(
        job.descriptionRaw!.length,
      );
    }
  });

  it("trusts the explicit remote flag", () => {
    for (const [index, job] of jobs.entries()) {
      if (raw[index].job_is_remote === true) expect(job.remote).toBe("remote");
    }
  });

  it("discards an implausible reported salary rather than trusting it", () => {
    const job = normalizeJSearchJob({
      job_id: "x",
      job_title: "Engineer",
      employer_name: "Acme",
      // What an aggregator reports when the source listed a figure in
      // thousands. A $90 floor would filter the job out of every real search.
      job_min_salary: 90,
      job_max_salary: 120,
      job_salary_period: "YEAR",
    });
    expect(job.salary.min).toBeNull();
  });

  it("keeps a plausible reported salary", () => {
    const job = normalizeJSearchJob({
      job_id: "x",
      job_title: "Engineer",
      employer_name: "Acme",
      job_min_salary: 150_000,
      job_max_salary: 200_000,
      job_salary_period: "YEAR",
    });
    expect(job.salary).toMatchObject({
      min: 150_000,
      max: 200_000,
      period: "year",
    });
  });

  it("dedupes against the same job from a company board", () => {
    // The whole point of the dedupe key: one posting, two sources.
    const fromAggregator = normalizeJSearchJob({
      job_id: "abc",
      job_title: "Software Engineer, Payments (Remote)",
      employer_name: "Stripe, Inc.",
      job_city: "San Francisco",
      job_state: "CA",
      job_country: "US",
    });

    expect(fromAggregator.dedupeKey).toBe(
      normalizeJSearchJob({
        job_id: "different-id-entirely",
        job_title: "Software Engineer, Payments",
        employer_name: "Stripe",
        job_location: "San Francisco, CA",
      }).dedupeKey,
    );
  });
});

describe("atsFromApplyOptions", () => {
  it("spots a Greenhouse board in an apply link", () => {
    expect(
      atsFromApplyOptions({
        job_id: "1",
        job_title: "Engineer",
        apply_options: [
          { apply_link: "https://www.linkedin.com/jobs/view/123" },
          { apply_link: "https://job-boards.greenhouse.io/ramp/jobs/456" },
        ],
      }),
    ).toEqual({ source: "greenhouse", slug: "ramp" });
  });

  it("spots Lever and Ashby too", () => {
    expect(
      atsFromApplyOptions({
        job_id: "1",
        job_title: "E",
        job_apply_link: "https://jobs.lever.co/plaid/abc",
      }),
    ).toEqual({ source: "lever", slug: "plaid" });

    expect(
      atsFromApplyOptions({
        job_id: "1",
        job_title: "E",
        job_apply_link: "https://jobs.ashbyhq.com/vanta/abc/application",
      }),
    ).toEqual({ source: "ashby", slug: "vanta" });
  });

  it("finds nothing when every link is a job board aggregator", () => {
    expect(
      atsFromApplyOptions({
        job_id: "1",
        job_title: "E",
        apply_options: [
          { apply_link: "https://www.indeed.com/viewjob?jk=1" },
          { apply_link: "https://www.linkedin.com/jobs/view/2" },
        ],
      }),
    ).toBeNull();
  });
});

describe("employerSlug", () => {
  it("makes a stable slug out of a company name", () => {
    expect(employerSlug("Stripe, Inc.")).toBe("stripe-inc");
    expect(employerSlug("Ben & Jerry's")).toBe("ben-jerry-s");
  });

  it("never returns an empty slug, which would break the job id", () => {
    expect(employerSlug("   ")).toBe("unknown");
    expect(employerSlug("!!!")).toBe("unknown");
  });
});
