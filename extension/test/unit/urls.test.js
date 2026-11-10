import { describe, expect, it } from "vitest";

import { lookupUrl } from "../../lib/urls.js";

describe("the address Foray is asked about", () => {
  it("is the page's own address", () => {
    const posting = "https://jobs.lever.co/northwind/11111111-2222-4333-8444-555555555555/apply";
    expect(lookupUrl(posting)).toBe(posting);
    expect(lookupUrl("https://job-boards.greenhouse.io/northwind/jobs/4000001?gh_src=x")).toBe(
      "https://job-boards.greenhouse.io/northwind/jobs/4000001?gh_src=x",
    );
  });

  it("is the posting on the board for a Greenhouse form embedded in an employer's site", () => {
    const posting = "https://job-boards.greenhouse.io/northwind/jobs/4000001";
    expect(lookupUrl("https://job-boards.greenhouse.io/embed/job_app?for=northwind&token=4000001")).toBe(posting);
    expect(lookupUrl("https://boards.greenhouse.io/embed/job_app?token=4000001&for=northwind&b=x")).toBe(posting);
  });

  it("does not make an address up from a frame that names no job", () => {
    const bare = "https://job-boards.greenhouse.io/embed/job_app?for=northwind";
    expect(lookupUrl(bare)).toBe(bare);
    const other = "https://example.test/embed/job_app?for=northwind&token=4000001";
    expect(lookupUrl(other)).toBe(other);
    expect(lookupUrl("https://job-boards.greenhouse.io/embed/job_app?for=a&token=../me")).toContain("token=");
  });

  it("asks nothing for what is not a web page", () => {
    expect(lookupUrl("chrome://extensions")).toBe("");
    expect(lookupUrl("about:blank")).toBe("");
    expect(lookupUrl("not a url")).toBe("");
  });
});
