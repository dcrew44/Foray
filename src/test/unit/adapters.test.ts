import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { normalizeAshbyJob } from "@/lib/sources/ashby";
import { normalizeGreenhouseJob } from "@/lib/sources/greenhouse";
import { normalizeLeverJob } from "@/lib/sources/lever";

/**
 * Adapters against real captured payloads.
 *
 * The fixtures are genuine API responses, trimmed to a few postings. They exist
 * because every one of these providers has a quirk that only shows up in real
 * data — escaped HTML, a description that omits the requirements, a
 * compensation block that is present but empty.
 */

const fixture = <T>(path: string): T =>
  JSON.parse(readFileSync(`src/test/fixtures/${path}`, "utf8")) as T;

const greenhouseBoard = fixture<{ jobs: Parameters<typeof normalizeGreenhouseJob>[1][] }>(
  "greenhouse/anthropic.json",
);
const leverBoard = fixture<Parameters<typeof normalizeLeverJob>[1][]>(
  "lever/matchgroup.json",
);
const ashbyLinear = fixture<{ jobs: Parameters<typeof normalizeAshbyJob>[1][] }>(
  "ashby/linear.json",
);
const ashbyVanta = fixture<{ jobs: Parameters<typeof normalizeAshbyJob>[1][] }>(
  "ashby/vanta.json",
);

describe("greenhouse adapter", () => {
  const jobs = greenhouseBoard.jobs.map((job) =>
    normalizeGreenhouseJob("anthropic", job),
  );

  it("builds source:company:id, coercing the numeric id to a string", () => {
    for (const job of jobs) {
      expect(job.id).toMatch(/^greenhouse:anthropic:\d+$/);
      expect(job.externalId).toEqual(expect.any(String));
    }
  });

  it("leaves description_raw exactly as the API sent it", () => {
    const [first] = jobs;
    expect(first.descriptionRaw).toBe(greenhouseBoard.jobs[0].content);
    // Which for Greenhouse means still entity-escaped.
    expect(first.descriptionRaw).toContain("&lt;");
  });

  it("unescapes the HTML for description_text", () => {
    const [first] = jobs;
    expect(first.descriptionText).not.toContain("&lt;");
    expect(first.descriptionText).not.toContain("<div");
    expect(first.descriptionText?.length ?? 0).toBeGreaterThan(200);
  });

  it("splits a pipe-separated multi-city location", () => {
    const multi = jobs.find((job) => job.locations.length > 1);
    expect(multi?.locations).not.toContain("|");
    for (const job of jobs) {
      for (const location of job.locations) {
        expect(location).not.toMatch(/\|/);
      }
    }
  });

  it("splits on semicolons as well as pipes, and de-duplicates", () => {
    // Real value: "New York City, NY; San Francisco, CA | New York City, NY"
    const mixed = jobs.find((job) =>
      greenhouseBoard.jobs.some(
        (raw) =>
          raw.id === Number(job.externalId) &&
          (raw.location?.name ?? "").includes(";"),
      ),
    );
    expect(mixed).toBeDefined();
    expect(mixed!.locations).toContain("San Francisco, CA");
    expect(mixed!.locations).toContain("New York City, NY");
    expect(new Set(mixed!.locations).size).toBe(mixed!.locations.length);
    // And the Bay Area is now visible, which a pipe-only split would have hidden.
    expect(mixed!.locationNorm?.metro).toBe("bay-area");
  });

  it("recognises the Bay Area", () => {
    const sf = jobs.find((job) =>
      job.locations.some((l) => l.includes("San Francisco")),
    );
    expect(sf?.locationNorm?.metro).toBe("bay-area");
  });

  it("parses a date and a dedupe key for every posting", () => {
    for (const job of jobs) {
      expect(job.postedAt).toBeInstanceOf(Date);
      expect(Number.isNaN(job.postedAt!.getTime())).toBe(false);
      expect(job.dedupeKey).toMatch(/^[0-9a-f]{40}$/);
      expect(job.contentHash).toMatch(/^[0-9a-f]{40}$/);
    }
  });
});

describe("lever adapter", () => {
  const jobs = leverBoard.map((posting) =>
    normalizeLeverJob("matchgroup", posting),
  );

  it("uses `text` as the title and the UUID as the external id", () => {
    expect(jobs[0].title).toBe(leverBoard[0].text);
    expect(jobs[0].id).toBe(`lever:matchgroup:${leverBoard[0].id}`);
  });

  it("includes the requirements lists, which `description` omits", () => {
    const withLists = leverBoard.findIndex((p) => (p.lists?.length ?? 0) > 0);
    expect(withLists).toBeGreaterThanOrEqual(0);

    const posting = leverBoard[withLists];
    const job = jobs[withLists];

    // The whole point: `description` alone would drop these.
    expect(job.descriptionRaw!.length).toBeGreaterThan(
      posting.description!.length,
    );
    for (const list of posting.lists ?? []) {
      if (list.text) expect(job.descriptionRaw).toContain(list.text);
    }
  });

  it("reads the structured salary range rather than guessing from text", () => {
    const index = leverBoard.findIndex((p) => p.salaryRange?.min);
    if (index === -1) return;

    const posting = leverBoard[index];
    expect(jobs[index].salary).toMatchObject({
      min: posting.salaryRange!.min,
      max: posting.salaryRange!.max,
      currency: posting.salaryRange!.currency,
    });
  });

  it("maps workplaceType straight onto remote", () => {
    for (const [index, posting] of leverBoard.entries()) {
      if (!posting.workplaceType) continue;
      expect(jobs[index].remote).toBe(posting.workplaceType.toLowerCase());
    }
  });

  it("keeps the apply URL separate from the posting URL", () => {
    for (const job of jobs) {
      expect(job.applyUrl).not.toBe(job.url);
      expect(job.applyUrl).toContain("/apply");
    }
  });

  it("reads createdAt as epoch milliseconds", () => {
    expect(jobs[0].postedAt?.getTime()).toBe(leverBoard[0].createdAt);
  });
});

describe("ashby adapter", () => {
  const linear = ashbyLinear.jobs.map((job) => normalizeAshbyJob("linear", job));
  const vanta = ashbyVanta.jobs.map((job) => normalizeAshbyJob("vanta", job));

  it("keeps the complete description as sent", () => {
    expect(linear[0].descriptionRaw).toBe(ashbyLinear.jobs[0].descriptionHtml);
    expect(linear[0].descriptionText).not.toContain("<p");
  });

  it("reads structured compensation where the board publishes it", () => {
    const index = ashbyVanta.jobs.findIndex((job) =>
      job.compensation?.summaryComponents?.some(
        (c) => c.compensationType?.toLowerCase() === "salary",
      ),
    );
    expect(index).toBeGreaterThanOrEqual(0);

    const salary = vanta[index].salary;
    expect(salary.min).toBeGreaterThan(0);
    expect(salary.currency).toBe("USD");
    expect(salary.period).toBe("year");
  });

  it("survives a board that publishes no compensation at all", () => {
    // linear sets shouldDisplayCompensationOnJobPostings: false.
    for (const job of linear) {
      expect(job.salary).toHaveProperty("min");
      expect(job.id).toMatch(/^ashby:linear:/);
    }
  });

  it("collects secondary locations alongside the primary one", () => {
    const index = ashbyLinear.jobs.findIndex(
      (job) => (job.secondaryLocations?.length ?? 0) > 0,
    );
    if (index === -1) return;
    expect(linear[index].locations.length).toBeGreaterThan(1);
  });

  it("keeps the apply URL separate from the posting URL", () => {
    for (const job of [...linear, ...vanta]) {
      expect(job.applyUrl).not.toBe(job.url);
    }
  });
});

describe("across adapters", () => {
  it("never leaves description_raw as cleaned text", () => {
    const all = [
      ...greenhouseBoard.jobs.map((j) => normalizeGreenhouseJob("anthropic", j)),
      ...leverBoard.map((p) => normalizeLeverJob("matchgroup", p)),
      ...ashbyLinear.jobs.map((j) => normalizeAshbyJob("linear", j)),
    ];

    for (const job of all) {
      if (!job.descriptionRaw || !job.descriptionText) continue;
      // Cleaning strips markup, so the cleaned copy is always shorter and the
      // raw copy always still carries its original markup.
      expect(job.descriptionText.length).toBeLessThanOrEqual(
        job.descriptionRaw.length,
      );
      expect(job.descriptionText).not.toMatch(/<(p|div|li|ul|h[1-6])\b/);
    }
  });

  it("gives every job a well-formed id and a remote verdict", () => {
    const all = [
      ...greenhouseBoard.jobs.map((j) => normalizeGreenhouseJob("anthropic", j)),
      ...leverBoard.map((p) => normalizeLeverJob("matchgroup", p)),
      ...ashbyVanta.jobs.map((j) => normalizeAshbyJob("vanta", j)),
    ];

    for (const job of all) {
      expect(job.id.split(":")).toHaveLength(3);
      expect(["remote", "hybrid", "onsite", "unknown"]).toContain(job.remote);
      expect(job.url).toMatch(/^https?:\/\//);
    }
  });
});
