import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { htmlToText, htmlToTextWithLinks } from "@/lib/sources/clean";

/**
 * The careers-page extractor reads *text*, but is asked to copy each posting's
 * real link rather than invent one. That only works if the links survive the
 * HTML-to-text conversion — and the first version of this pipeline used
 * `htmlToText`, which strips every href. The model dutifully returned null for
 * every URL, every posting was discarded as unusable, and a page full of real
 * jobs extracted as zero. These tests pin the difference.
 */

const retool = readFileSync(
  "src/test/fixtures/careers-pages/retool.html",
  "utf8",
);
const crusoe = readFileSync(
  "src/test/fixtures/careers-pages/crusoe.html",
  "utf8",
);

describe("htmlToTextWithLinks", () => {
  it("keeps a link's target, which plain text conversion throws away", () => {
    const html = '<a href="/careers/swe">Software Engineer</a>';

    expect(htmlToText(html)).toBe("Software Engineer");
    expect(htmlToTextWithLinks(html)).toContain(
      "[Software Engineer](/careers/swe)",
    );
  });

  it("absolutises against the page it came from", () => {
    expect(
      htmlToTextWithLinks('<a href="/careers/swe">SWE</a>', "https://acme.com/careers"),
    ).toContain("[SWE](https://acme.com/careers/swe)");
  });

  it("keeps the label but drops the target for navigation links", () => {
    // Anchors and mailto: are not postings; keeping them as links would invite
    // the extractor to emit an unusable URL.
    const text = htmlToTextWithLinks(
      '<a href="#top">Back to top</a><a href="mailto:jobs@acme.com">Email us</a>',
      "https://acme.com",
    );
    expect(text).toContain("Back to top");
    expect(text).not.toContain("](#top)");
    expect(text).not.toContain("mailto:");
  });

  it("strips markup inside a link's label", () => {
    expect(
      htmlToTextWithLinks('<a href="/x"><span>Staff</span> <b>Engineer</b></a>'),
    ).toContain("[Staff Engineer](/x)");
  });

  it("drops a link with no readable label", () => {
    expect(htmlToTextWithLinks('<a href="/x"><img src="y.png"></a>')).not.toContain(
      "](/x)",
    );
  });

  it("surfaces real posting links from a real careers page", () => {
    const text = htmlToTextWithLinks(retool, "https://retool.com/careers");

    // The page really does list engineering roles, and their links have to be
    // visible in what the extractor reads.
    expect(text).toMatch(/\[[^\]]*Engineer[^\]]*\]\(https:\/\/retool\.com\/careers\//);

    // The same page through the link-stripping conversion offers nothing to copy.
    expect(htmlToText(retool)).not.toContain("](https://retool.com/careers/");
  });

  it("has nothing posting-shaped to copy on a JavaScript-rendered page", () => {
    // Crusoe renders its listing client-side. The served HTML carries only
    // navigation links — /about/careers, /about/careers/culture — and no
    // individual postings. This is the case that must flag needs_browser
    // rather than look like a company with no open jobs.
    const distinct = (html: string, base: string) =>
      new Set(
        [...htmlToTextWithLinks(html, base).matchAll(/\]\((https?:\/\/[^)]+)\)/g)]
          .map((match) => match[1])
          .filter((url) => /job|career|position|opening/i.test(url)),
      );

    const crusoeLinks = distinct(crusoe, "https://www.crusoe.ai/careers");
    const retoolLinks = distinct(retool, "https://retool.com/careers");

    // Counted distinctly: repeats in the page are not more jobs.
    expect(crusoeLinks.size).toBeLessThan(6);
    expect(retoolLinks.size).toBeGreaterThan(10);
  });
});
