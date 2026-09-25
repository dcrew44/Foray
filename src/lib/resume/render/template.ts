import type {
  EducationItem,
  ProjectItem,
  Resume,
  SkillItem,
  WorkItem,
} from "../schema";

/**
 * The resume as a printable HTML document.
 *
 * A pure string function rather than a React component: it runs in the worker
 * (which renders it to PDF with headless Chromium), in a Next route handler
 * (the instant preview), and in unit tests, and none of those should need a
 * React renderer to produce one static page.
 *
 * The layout is deliberately ATS-friendly — one column, real text in reading
 * order, no tables, icons or images — because the first reader of a resume is
 * usually a parser. Everything the resume says comes from the stored data
 * verbatim, dates included; this file only decides how it looks.
 *
 * Every interpolated value goes through `escapeHtml`. Resume text is
 * user-supplied (and, for tailored resumes, model-written), and the same HTML
 * is served to the browser for the preview, so an unescaped `<script>` in a
 * bullet would be stored XSS.
 */

export type RenderOptions = {
  /**
   * The document title. Chromium writes it into the PDF's metadata, where a
   * recruiter's viewer may show it, so the default is the plain
   * "<Name> — Resume" rather than anything that mentions tailoring.
   */
  title?: string;
  /**
   * `compact` trims type size and spacing slightly. The PDF renderer retries
   * with it when the normal layout runs just past a page, which is common and
   * looks careless; it never changes a word of content.
   */
  density?: "normal" | "compact";
};

/** The accent used for section headings — Foray's pine green. */
const ACCENT = "#1E5C45";

export function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * A link target that is safe to put in an href, or null.
 *
 * Resumes store links the way people write them — "github.com/someone",
 * rarely with a scheme — so a bare domain becomes https. Anything with a
 * scheme other than http(s) or mailto (javascript:, data:, file:) is refused
 * and rendered as plain text instead.
 */
export function safeHref(raw: string | null | undefined): string | null {
  const value = (raw ?? "").trim();
  if (!value || /\s/.test(value)) return null;
  if (/^https?:\/\//i.test(value)) return value;
  if (/^mailto:[^\s@]+@[^\s@]+$/i.test(value)) return value;
  if (/^[^\s@/:]+@[^\s@/:]+\.[a-z]{2,}$/i.test(value)) return `mailto:${value}`;
  // Any other scheme ("javascript:", "data:") — but not a port ("host:8080").
  if (/^[a-z][a-z0-9+.-]*:(?!\d)/i.test(value)) return null;
  if (/^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}(:\d+)?([/?#]\S*)?$/i.test(value)) {
    return `https://${value}`;
  }
  return null;
}

/** "https://www.github.com/x/" → "github.com/x": what a reader should see. */
export function displayUrl(raw: string): string {
  return raw
    .trim()
    .replace(/^https?:\/\//i, "")
    .replace(/^mailto:/i, "")
    .replace(/^www\./i, "")
    .replace(/\/+$/, "");
}

/** Dates exactly as stored: "March 2023 – Present", or whichever end exists. */
export function dateRange(
  start: string | null | undefined,
  end: string | null | undefined,
): string {
  const from = start?.trim();
  const to = end?.trim();
  if (from && to) return from === to ? from : `${from} – ${to}`;
  return from || to || "";
}

const text = (value: string | null | undefined): string =>
  value?.trim() ? escapeHtml(value.trim()) : "";

const present = (value: string | null | undefined): value is string =>
  Boolean(value?.trim());

function link(url: string, label?: string | null): string {
  const href = safeHref(url);
  const shown = escapeHtml(label?.trim() || displayUrl(url));
  return href ? `<a href="${escapeHtml(href)}">${shown}</a>` : shown;
}

/** Joins inline parts with a separator that is real text, so parsers keep it. */
function inline(parts: string[], className = "sep"): string {
  return parts
    .filter(Boolean)
    .join(`<span class="${className}" aria-hidden="true"> · </span>`);
}

function bullets(items: string[]): string {
  const kept = items.filter(present);
  if (kept.length === 0) return "";
  return `<ul class="bullets">${kept.map((item) => `<li>${text(item)}</li>`).join("")}</ul>`;
}

function entry(input: {
  title: string;
  dates: string;
  body: string;
}): string {
  return `<article class="entry">
<div class="entry-head"><p class="entry-title">${input.title}</p>${
    input.dates ? `<p class="dates">${escapeHtml(input.dates)}</p>` : ""
  }</div>${input.body}
</article>`;
}

function section(id: string, heading: string, content: string): string {
  if (!content.trim()) return "";
  return `<section class="section" aria-labelledby="h-${id}">
<h2 id="h-${id}">${escapeHtml(heading)}</h2>
${content}
</section>`;
}

/**
 * Roughly how many characters of 9pt contact text fit across the page. Used
 * only to decide whether links get their own line: a deliberate break reads
 * as designed, where a wrap mid-list leaves a separator dangling at the end of
 * the first line.
 */
const CONTACT_LINE_CHARS = 100;

function masthead(resume: Resume): string {
  const basics = resume.basics;

  const personal = [
    present(basics.location) ? { html: text(basics.location), chars: basics.location!.trim().length } : null,
    present(basics.email)
      ? { html: link(basics.email!.trim(), basics.email), chars: basics.email!.trim().length }
      : null,
    present(basics.phone) ? { html: phone(basics.phone!), chars: basics.phone!.trim().length } : null,
  ].filter((part) => part !== null);

  const links = basics.links
    .filter((l) => present(l.url))
    .map((l) => ({ html: link(l.url), chars: displayUrl(l.url).length }));

  const width = (parts: Array<{ chars: number }>) =>
    parts.reduce((sum, part) => sum + part.chars + 3, 0);

  const lines =
    personal.length > 0 && links.length > 0 && width([...personal, ...links]) > CONTACT_LINE_CHARS
      ? [personal, links]
      : [[...personal, ...links]];

  const contact = lines
    .filter((line) => line.length > 0)
    .map((line) => `<p class="contact">${inline(line.map((part) => part.html))}</p>`)
    .join("\n");

  return `<header class="masthead">
<h1 class="name">${text(basics.name) || "Resume"}</h1>${
    present(basics.headline)
      ? `\n<p class="headline">${text(basics.headline)}</p>`
      : ""
  }${contact ? `\n${contact}` : ""}
</header>`;
}

function phone(value: string): string {
  const digits = value.replace(/[^\d+]/g, "");
  return digits.length >= 7
    ? `<a href="tel:${escapeHtml(digits)}">${text(value)}</a>`
    : text(value);
}

function work(item: WorkItem): string {
  return entry({
    title: inline([
      present(item.position) ? `<strong class="role">${text(item.position)}</strong>` : "",
      text(item.company),
      present(item.location) ? `<span class="where">${text(item.location)}</span>` : "",
    ]),
    dates: dateRange(item.startDate, item.endDate),
    body:
      (present(item.summary) ? `<p class="entry-summary">${text(item.summary)}</p>` : "") +
      bullets(item.highlights),
  });
}

function degree(item: EducationItem): string {
  const type = item.studyType?.trim();
  const area = item.area?.trim();
  if (type && area) return `${type} in ${area}`;
  return type || area || "";
}

function education(item: EducationItem): string {
  const qualification = degree(item);
  return entry({
    title: inline([
      qualification
        ? `<strong class="role">${escapeHtml(qualification)}</strong>`
        : `<strong class="role">${text(item.institution)}</strong>`,
      qualification ? text(item.institution) : "",
      present(item.score) ? `<span class="where">${text(item.score)}</span>` : "",
    ]),
    dates: dateRange(item.startDate, item.endDate),
    body: bullets(item.highlights),
  });
}

function project(item: ProjectItem): string {
  return entry({
    title: inline([
      `<strong class="role">${text(item.name)}</strong>`,
      present(item.url) ? `<span class="where">${link(item.url!)}</span>` : "",
    ]),
    dates: dateRange(item.startDate, item.endDate),
    body:
      (present(item.description)
        ? `<p class="entry-summary">${text(item.description)}</p>`
        : "") + bullets(item.highlights),
  });
}

/**
 * Skills in the resume's own grouping, in order of first appearance — so a
 * tailored resume that leads with the most relevant skill leads with its
 * group too. Uncategorised skills get one plain line, or an "Other" row when
 * they sit alongside labelled groups.
 */
function skills(items: SkillItem[]): string {
  const kept = items.filter((skill) => present(skill.name));
  if (kept.length === 0) return "";

  const groups = new Map<string, string[]>();
  for (const skill of kept) {
    const key = skill.category?.trim() ?? "";
    groups.set(key, [...(groups.get(key) ?? []), skill.name.trim()]);
  }

  if (groups.size === 1 && groups.has("")) {
    return `<p class="skills-line">${kept.map((s) => text(s.name)).join(", ")}</p>`;
  }

  const rows = [...groups]
    .sort(([a], [b]) => Number(a === "") - Number(b === ""))
    .map(
      ([category, names]) =>
        `<p class="skills-row"><span class="skills-label">${escapeHtml(category || "Other")}:</span> ${names
          .map((name) => escapeHtml(name))
          .join(", ")}</p>`,
    );
  return `<div class="skills">${rows.join("")}</div>`;
}

export function renderResumeHtml(
  resume: Resume,
  options: RenderOptions = {},
): string {
  const name = resume.basics.name?.trim();
  const title = options.title ?? (name ? `${name} — Resume` : "Resume");

  const body = [
    masthead(resume),
    section(
      "summary",
      "Summary",
      present(resume.summary) ? `<p class="summary">${text(resume.summary)}</p>` : "",
    ),
    section("experience", "Experience", resume.work.map(work).join("\n")),
    section("projects", "Projects", resume.projects.map(project).join("\n")),
    section("education", "Education", resume.education.map(education).join("\n")),
    section("skills", "Skills", skills(resume.skills)),
  ]
    .filter(Boolean)
    .join("\n");

  const density = options.density === "compact" ? " compact" : "";

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>${STYLES}</style>
</head>
<body>
<main class="page${density}">
${body}
</main>
</body>
</html>
`;
}

/**
 * Print-first CSS. Sizes are in points because the PDF is the product; the
 * screen rules only make the same document look like a sheet of paper in the
 * preview iframe, and fall back to plain reflowing text when that iframe is
 * narrower than the page.
 */
const STYLES = `
@page { size: Letter; margin: 0.5in 0.6in; }

:root {
  --ink: #1b211e;
  --body: #2b312e;
  --muted: #5f6964;
  --faint: #9aa8a1;
  --rule: #b9cbc2;
  --accent: ${ACCENT};
}

* { box-sizing: border-box; }

html {
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
  -webkit-text-size-adjust: 100%;
}

body {
  margin: 0;
  background: #ffffff;
  color: var(--body);
  font-family: "Helvetica Neue", Helvetica, Arial, "Liberation Sans", sans-serif;
  font-kerning: normal;
  text-rendering: optimizeLegibility;
  hyphens: manual;
  overflow-wrap: break-word;
}

/* Sizes live in variables so the compact density is a handful of overrides
   rather than a second stylesheet. */
.page {
  --size: 10pt;
  --small: 9.25pt;
  --leading: 1.3;
  --name: 22pt;
  --gap-masthead: 10pt;
  --gap-section: 10pt;
  --gap-heading: 5pt;
  --gap-entry: 7pt;
  --gap-bullet: 1.5pt;
  font-size: var(--size);
  line-height: var(--leading);
}

.page.compact {
  --size: 9.5pt;
  --small: 8.75pt;
  --leading: 1.26;
  --name: 20pt;
  --gap-masthead: 8pt;
  --gap-section: 8pt;
  --gap-heading: 4pt;
  --gap-entry: 5pt;
  --gap-bullet: 1pt;
}

p, h1, h2, ul { margin: 0; }

a { color: inherit; text-decoration: none; }

.masthead + .section { margin-top: var(--gap-masthead); }

.name {
  color: var(--ink);
  font-size: var(--name);
  font-weight: 700;
  line-height: 1.1;
  letter-spacing: -0.01em;
}

.headline {
  margin-top: 2pt;
  font-size: calc(var(--size) + 1pt);
}

.contact {
  color: var(--muted);
  font-size: var(--small);
}
.name + .contact, .headline + .contact { margin-top: 4pt; }
.contact + .contact { margin-top: 1pt; }

.sep { color: var(--rule); }
.contact .sep { color: var(--faint); }

.section { margin-top: var(--gap-section); }

.section > h2 {
  color: var(--accent);
  font-size: 8.75pt;
  font-weight: 700;
  /* Tracking above ~0.1em makes PDF text extraction read "S U M M A R Y",
     and a parser that cannot find the section headings cannot parse the
     resume. */
  letter-spacing: 0.07em;
  line-height: 1.2;
  text-transform: uppercase;
  padding-bottom: 2.5pt;
  margin-bottom: var(--gap-heading);
  border-bottom: 0.6pt solid var(--rule);
  break-after: avoid;
  page-break-after: avoid;
}

.entry, .skills, .summary {
  break-inside: avoid;
  page-break-inside: avoid;
}
.entry + .entry { margin-top: var(--gap-entry); }

.entry-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12pt;
}

.entry-title {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--ink);
}

.role { font-weight: 700; color: var(--ink); }
.where { color: var(--muted); }

.dates {
  flex: 0 0 auto;
  color: var(--muted);
  font-size: var(--small);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.entry-summary { margin-top: 1pt; }

.bullets {
  margin-top: 2pt;
  padding-left: 11pt;
  list-style: none;
}

.bullets > li + li { margin-top: var(--gap-bullet); }

/* The bullet is a real glyph, so extracted text still shows where one bullet
   ends and the next begins, and it stays in normal flow: anything positioned
   is painted after the rest of the page, which puts every bullet at the end
   of the PDF's text layer — the order an ATS reads it in. */
.bullets > li::before {
  content: "\\2022";
  display: inline-block;
  width: 9pt;
  margin-left: -9pt;
  color: var(--accent);
}

.skills-row + .skills-row { margin-top: var(--gap-bullet); }
.skills-label { color: var(--ink); font-weight: 700; }

@media screen {
  html { background: #e8ece9; }
  body { background: transparent; }
  .page {
    width: 8.5in;
    min-height: 11in;
    margin: 24px auto;
    padding: 0.5in 0.6in;
    background: #ffffff;
    box-shadow: 0 1px 2px rgba(20, 30, 25, 0.08), 0 8px 28px rgba(20, 30, 25, 0.10);
  }
}

@media screen and (max-width: 8.75in) {
  html { background: #ffffff; }
  .page {
    width: auto;
    min-height: 0;
    margin: 0;
    padding: 28px 24px;
    box-shadow: none;
  }
}

@media print {
  html, body { background: #ffffff; }
  .page { width: auto; margin: 0; padding: 0; box-shadow: none; }
}
`;
