import { describe, expect, it } from "vitest";

import {
  findAtsLinks,
  findCareersLinks,
  hasUnsupportedAts,
  normalizeDomain,
} from "@/lib/companies/discovery";
import { slugCandidates } from "@/lib/companies/verify";

describe("findAtsLinks", () => {
  it("finds a Greenhouse board link", () => {
    expect(
      findAtsLinks('<a href="https://job-boards.greenhouse.io/stripe">Jobs</a>'),
    ).toEqual([{ source: "greenhouse", slug: "stripe" }]);
  });

  it("finds the older boards.greenhouse.io host too", () => {
    expect(
      findAtsLinks('<a href="https://boards.greenhouse.io/figma/jobs/123">x</a>'),
    ).toEqual([{ source: "greenhouse", slug: "figma" }]);
  });

  it("reads the slug out of a Greenhouse embed", () => {
    expect(
      findAtsLinks(
        '<script src="https://boards.greenhouse.io/embed/job_board?for=airtable"></script>',
      ),
    ).toEqual([{ source: "greenhouse", slug: "airtable" }]);
  });

  it("finds Lever and Ashby boards", () => {
    expect(findAtsLinks('href="https://jobs.lever.co/spotify"')).toEqual([
      { source: "lever", slug: "spotify" },
    ]);
    expect(findAtsLinks('href="https://jobs.ashbyhq.com/linear/abc"')).toEqual([
      { source: "ashby", slug: "linear" },
    ]);
  });

  it("de-duplicates a board linked many times on one page", () => {
    const html = Array.from(
      { length: 20 },
      (_, index) =>
        `<a href="https://job-boards.greenhouse.io/ramp/jobs/${index}">Role ${index}</a>`,
    ).join("");

    expect(findAtsLinks(html)).toEqual([{ source: "greenhouse", slug: "ramp" }]);
  });

  it("finds several distinct boards on one page", () => {
    const links = findAtsLinks(
      'a href="https://jobs.lever.co/alpha" b href="https://jobs.ashbyhq.com/beta"',
    );
    expect(links).toHaveLength(2);
  });

  it("is not confused by repeated calls, despite the global regexes", () => {
    // A global regex carries lastIndex between uses; forgetting to reset it
    // makes the second page on a discovery run silently find nothing.
    const html = 'href="https://jobs.lever.co/plaid"';
    expect(findAtsLinks(html)).toEqual(findAtsLinks(html));
    expect(findAtsLinks(html)).toHaveLength(1);
  });

  it("finds nothing in a page with no board", () => {
    expect(findAtsLinks("<html><body>We are not hiring.</body></html>")).toEqual(
      [],
    );
  });
});

describe("hasUnsupportedAts", () => {
  it("recognises boards we cannot read through an API", () => {
    expect(
      hasUnsupportedAts('href="https://acme.wd1.myworkdayjobs.com/careers"'),
    ).toBe(true);
    expect(hasUnsupportedAts('href="https://jobs.smartrecruiters.com/acme"')).toBe(
      true,
    );
  });

  it("does not flag a board we can read", () => {
    expect(hasUnsupportedAts('href="https://jobs.lever.co/spotify"')).toBe(false);
  });
});

describe("findCareersLinks", () => {
  it("absolutises a relative careers link", () => {
    expect(
      findCareersLinks('<a href="/about/careers">Join us</a>', "https://acme.com"),
    ).toEqual(["https://acme.com/about/careers"]);
  });

  it("picks up the usual wordings", () => {
    const links = findCareersLinks(
      '<a href="/jobs">Jobs</a><a href="/join-us">Join</a><a href="/work-with-us">Work</a>',
      "https://acme.com",
    );
    expect(links).toHaveLength(3);
  });

  it("ignores javascript: and mailto: hrefs", () => {
    expect(
      findCareersLinks(
        '<a href="javascript:openCareers()">Careers</a><a href="mailto:jobs@acme.com">Jobs</a>',
        "https://acme.com",
      ),
    ).toEqual([]);
  });

  it("ignores links that have nothing to do with careers", () => {
    expect(
      findCareersLinks('<a href="/pricing">Pricing</a>', "https://acme.com"),
    ).toEqual([]);
  });
});

describe("normalizeDomain", () => {
  it("strips scheme, www and path", () => {
    for (const input of [
      "https://www.stripe.com/careers",
      "stripe.com",
      "HTTPS://STRIPE.COM",
      "www.stripe.com",
    ]) {
      expect(normalizeDomain(input)).toBe("stripe.com");
    }
  });

  it("keeps a meaningful subdomain", () => {
    expect(normalizeDomain("https://jobs.acme.co.uk/list")).toBe(
      "jobs.acme.co.uk",
    );
  });

  it("returns null for nothing usable", () => {
    expect(normalizeDomain("   ")).toBeNull();
  });
});

describe("slugCandidates", () => {
  it("offers the obvious forms first", () => {
    const candidates = slugCandidates("Match Group");
    expect(candidates[0]).toBe("matchgroup");
    expect(candidates).toContain("match-group");
    expect(candidates).toContain("match");
  });

  it("drops legal suffixes and punctuation", () => {
    expect(slugCandidates("Stripe, Inc.")[0]).toBe("stripe");
    expect(slugCandidates("Ben & Jerry's")[0]).toBe("benandjerrys");
  });

  it("returns nothing for a name with no letters", () => {
    expect(slugCandidates("   ")).toEqual([]);
  });
});
