import { describe, expect, it } from "vitest";

import {
  dateRange,
  displayUrl,
  escapeHtml,
  renderResumeHtml,
  safeHref,
} from "@/lib/resume/render/template";
import { ResumeSchema, type Resume } from "@/lib/resume/schema";

/**
 * The template is what recruiters and their parsers see. These pin that it is
 * a complete, safe document with the sections in the agreed order, that every
 * value is escaped (the same HTML is served to the browser for the preview),
 * and that stored text — dates especially — comes out exactly as stored.
 */

function resume(overrides: Partial<Resume> = {}): Resume {
  return ResumeSchema.parse({
    basics: {
      name: "Dana Reyes",
      email: "dana@example.com",
      phone: "(555) 0134-2087",
      location: "Sacramento, CA",
      links: [
        { id: "l1", url: "github.com/dana", label: "GitHub" },
        { id: "l2", url: "https://www.linkedin.com/in/dana/", label: "LinkedIn" },
      ],
    },
    summary: "Backend-leaning engineer.",
    work: [
      {
        id: "w1",
        company: "Kestrel Logistics Group",
        position: "Software Engineer II",
        location: "Sacramento, CA",
        startDate: "March 2023",
        endDate: "Present",
        highlights: ["Cut latency from 840 ms to 190 ms."],
      },
    ],
    education: [
      {
        id: "e1",
        institution: "Bellwater State University",
        studyType: "B.S.",
        area: "Computer Science",
        startDate: "2017",
        endDate: "2021",
        highlights: [],
      },
    ],
    projects: [{ id: "p1", name: "Tideline", url: "github.com/dana/tideline", description: "A CLI." }],
    skills: [
      { id: "s1", name: "TypeScript", category: "Languages" },
      { id: "s2", name: "PostgreSQL", category: "Data" },
      { id: "s3", name: "Python", category: "Languages" },
    ],
    ...overrides,
  });
}

describe("escapeHtml", () => {
  it("escapes every character that can break out of text or an attribute", () => {
    expect(escapeHtml(`<a href="x" onclick='y'>&</a>`)).toBe(
      "&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;&lt;/a&gt;",
    );
  });

  it("renders null and undefined as empty", () => {
    expect(escapeHtml(null)).toBe("");
    expect(escapeHtml(undefined)).toBe("");
  });
});

describe("safeHref", () => {
  it("adds https to a bare domain and keeps http(s) and mailto", () => {
    expect(safeHref("github.com/dana")).toBe("https://github.com/dana");
    expect(safeHref("https://example.com/x")).toBe("https://example.com/x");
    expect(safeHref("dana@example.com")).toBe("mailto:dana@example.com");
    expect(safeHref("mailto:dana@example.com")).toBe("mailto:dana@example.com");
  });

  it("refuses other schemes and junk", () => {
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,<script>")).toBeNull();
    expect(safeHref("not a url")).toBeNull();
    expect(safeHref("")).toBeNull();
  });
});

describe("helpers", () => {
  it("shows a URL the way a reader expects", () => {
    expect(displayUrl("https://www.linkedin.com/in/dana/")).toBe("linkedin.com/in/dana");
  });

  it("keeps dates exactly as stored", () => {
    expect(dateRange("March 2023", "Present")).toBe("March 2023 – Present");
    expect(dateRange("2021", null)).toBe("2021");
    expect(dateRange(null, "June 2024")).toBe("June 2024");
    expect(dateRange(null, null)).toBe("");
  });
});

describe("renderResumeHtml", () => {
  const html = renderResumeHtml(resume());

  it("is a complete document with inline CSS and a Letter page", () => {
    expect(html.startsWith("<!doctype html>")).toBe(true);
    expect(html).toContain("<style>");
    expect(html).toContain("@page { size: Letter; margin: 0.5in 0.6in; }");
    expect(html).not.toMatch(/<script|<link |<img /i);
    expect(html).toContain("<title>Dana Reyes — Resume</title>");
  });

  it("puts the name first and the sections in order", () => {
    const order = ["Dana Reyes", ">Summary<", ">Experience<", ">Projects<", ">Education<", ">Skills<"].map(
      (marker) => html.indexOf(marker),
    );
    expect(order.every((index) => index >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it("writes contact details as readable text with working links", () => {
    expect(html).toContain('<a href="mailto:dana@example.com">dana@example.com</a>');
    expect(html).toContain('<a href="https://github.com/dana">github.com/dana</a>');
    expect(html).toContain('<a href="https://www.linkedin.com/in/dana/">linkedin.com/in/dana</a>');
    expect(html).toContain('href="tel:5550134');
  });

  it("keeps stored text verbatim", () => {
    expect(html).toContain("March 2023 – Present");
    expect(html).toContain("Software Engineer II");
    expect(html).toContain("Cut latency from 840 ms to 190 ms.");
    expect(html).toContain("B.S. in Computer Science");
  });

  it("groups skills by the resume's own categories, in order of first appearance", () => {
    expect(html).toMatch(/Languages:<\/span> TypeScript, Python/);
    expect(html.indexOf("Languages:")).toBeLessThan(html.indexOf("Data:"));
  });

  it("puts uncategorised skills on one plain line", () => {
    const plain = renderResumeHtml(
      resume({
        skills: [
          { id: "a", source: "resume", name: "Go", category: null },
          { id: "b", source: "resume", name: "Rust", category: null },
        ],
      }),
    );
    expect(plain).toContain('<p class="skills-line">Go, Rust</p>');
  });

  it("escapes hostile text everywhere, including a script in a bullet", () => {
    const hostile = renderResumeHtml(
      resume({
        summary: "<img src=x onerror=alert(1)>",
        work: [
          {
            id: "w1",
            source: "resume",
            company: 'Acme "Corp"',
            position: "<b>Boss</b>",
            startDate: "2020",
            endDate: "2021",
            highlights: ["<script>alert('x')</script>"],
          },
        ],
        basics: {
          name: "</title><script>alert(1)</script>",
          links: [{ id: "l", source: "resume", url: "javascript:alert(1)", label: "x" }],
        },
      }),
    );
    expect(hostile).not.toContain("<script>");
    expect(hostile).not.toContain("<img");
    expect(hostile).not.toContain("<b>Boss");
    expect(hostile).toContain("&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;");
    expect(hostile).toContain("Acme &quot;Corp&quot;");
    expect(hostile).not.toContain('href="javascript:');
  });

  it("leaves out empty sections", () => {
    const sparse = renderResumeHtml(
      resume({ summary: null, projects: [], skills: [], education: [] }),
    );
    expect(sparse).not.toContain(">Summary<");
    expect(sparse).not.toContain(">Projects<");
    expect(sparse).not.toContain(">Skills<");
    expect(sparse).toContain(">Experience<");
  });

  it("offers a compact density without changing content", () => {
    const compact = renderResumeHtml(resume(), { density: "compact" });
    expect(compact).toContain('<main class="page compact">');
    const text = (doc: string) => doc.replace(/<style>[\s\S]*<\/style>/, "").replace(/ compact/, "");
    expect(text(compact)).toBe(text(html));
  });

  it("takes a custom title", () => {
    expect(renderResumeHtml(resume(), { title: "Custom" })).toContain("<title>Custom</title>");
  });
});
