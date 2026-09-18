// Regenerates plain.docx (a real OOXML .docx, for the mammoth extraction path).
// Run from the repo root:  pnpm exec node src/test/fixtures/resumes/plain.docx.gen.mjs
import { writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import {
  AlignmentType,
  Document,
  ExternalHyperlink,
  HeadingLevel,
  Packer,
  Paragraph,
  TabStopPosition,
  TabStopType,
  TextRun,
} from "docx";

const FONT = "Calibri";

const para = (children, opts = {}) =>
  new Paragraph({ children, spacing: { after: 80, line: 276 }, ...opts });

const text = (t, opts = {}) => new TextRun({ text: t, font: FONT, size: 22, ...opts });

const sectionHeading = (t) =>
  new Paragraph({
    children: [new TextRun({ text: t.toUpperCase(), font: FONT, size: 24, bold: true })],
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 280, after: 120 },
    border: { bottom: { style: "single", size: 6, color: "888888", space: 2 } },
  });

// role on the left, dates right-aligned on a tab stop
const jobHeading = (role, dates) =>
  new Paragraph({
    children: [
      new TextRun({ text: role, font: FONT, size: 22, bold: true }),
      new TextRun({ text: "\t", font: FONT, size: 22 }),
      new TextRun({ text: dates, font: FONT, size: 20 }),
    ],
    tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
    spacing: { before: 120, after: 0 },
  });

const orgLine = (org) =>
  new Paragraph({
    children: [new TextRun({ text: org, font: FONT, size: 21, italics: true })],
    spacing: { after: 60 },
  });

const bullet = (t) =>
  new Paragraph({
    children: [text(t)],
    bullet: { level: 0 },
    spacing: { after: 60, line: 264 },
  });

const doc = new Document({
  creator: "Foray test fixtures",
  title: "Wren Calloway-Sato - Resume",
  description: "Fictional resume fixture for DOCX extraction tests",
  sections: [
    {
      properties: {
        page: { margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 } },
      },
      children: [
        // ---- contact block ----
        new Paragraph({
          children: [new TextRun({ text: "Wren Calloway-Sato", font: FONT, size: 40, bold: true })],
          alignment: AlignmentType.CENTER,
          spacing: { after: 60 },
        }),
        new Paragraph({
          children: [
            text("Portland, OR | (555) 0142-9863 | wren.callowaysato@example.com", { size: 20 }),
          ],
          alignment: AlignmentType.CENTER,
          spacing: { after: 20 },
        }),
        new Paragraph({
          children: [
            new ExternalHyperlink({
              children: [text("github.com/wrencalloway", { size: 20, style: "Hyperlink" })],
              link: "https://github.com/wrencalloway",
            }),
            text("  |  ", { size: 20 }),
            new ExternalHyperlink({
              children: [text("linkedin.com/in/wrencallowaysato", { size: 20, style: "Hyperlink" })],
              link: "https://www.linkedin.com/in/wrencallowaysato",
            }),
            text("  |  ", { size: 20 }),
            new ExternalHyperlink({
              children: [text("wrencalloway.example.dev", { size: 20, style: "Hyperlink" })],
              link: "https://wrencalloway.example.dev",
            }),
          ],
          alignment: AlignmentType.CENTER,
          spacing: { after: 160 },
        }),

        // ---- summary ----
        sectionHeading("Summary"),
        para([
          text(
            "Software engineer with three years of experience building web applications and the " +
              "services behind them, mostly in TypeScript and Python. Comfortable across the stack " +
              "but happiest working on APIs, data models, and test infrastructure. Looking for a " +
              "mid-level product engineering role on a team that ships to real users regularly.",
          ),
        ]),

        // ---- experience ----
        sectionHeading("Experience"),

        jobHeading("Software Engineer", "April 2023 - Present"),
        orgLine("Marrow & Finch Software - Portland, OR"),
        bullet(
          "Own the appointments API (TypeScript, Node.js, PostgreSQL) serving roughly 85,000 " +
            "bookings per week across 340 clinic accounts.",
        ),
        bullet(
          "Cut p95 availability-search latency from 1.9 seconds to 240 milliseconds by replacing " +
            "per-slot queries with a precomputed availability index refreshed on write.",
        ),
        bullet(
          "Reduced failed bookings caused by double-allocation to near zero (from about 120 per " +
            "week) by moving slot reservation into a single serializable transaction.",
        ),
        bullet(
          "Set up the seed-data and factory layer that the whole backend test suite now uses; " +
            "integration test runtime dropped from 14 minutes to 5.",
        ),

        jobHeading("Junior Software Engineer", "September 2022 - March 2023"),
        orgLine("Pemberton Grove Health Tech - Portland, OR"),
        bullet(
          "Built the patient intake form flow in React, including offline draft saving and " +
            "resumable uploads for insurance documents.",
        ),
        bullet(
          "Migrated 22 legacy jQuery pages to the component library, which removed 11,000 lines of " +
            "duplicated code and shrank the main bundle by 38%.",
        ),
        bullet(
          "Wrote the team's first accessibility checklist after an audit found 40+ issues; all " +
            "were closed within one quarter.",
        ),

        jobHeading("Software Engineering Intern", "June 2021 - August 2021"),
        orgLine("Alder Point Labs - Eugene, OR"),
        bullet(
          "Implemented a batch export tool that saved the support team an estimated six hours " +
            "per week of manual CSV assembly.",
        ),
        bullet("Added continuous integration to three internal repositories that had none."),

        // ---- education ----
        sectionHeading("Education"),
        jobHeading("B.S. Computer Science", "2018 - 2022"),
        orgLine("Cascadia Vale University - Eugene, OR"),
        bullet(
          "Minor in Linguistics. GPA 3.58. Undergraduate teaching assistant for Intro to " +
            "Programming for four terms.",
        ),
        bullet(
          "Senior project: a speech-to-text alignment tool for oral history archives, built with " +
            "a community partner.",
        ),

        // ---- skills ----
        sectionHeading("Skills"),
        para([
          text("Languages: ", { bold: true }),
          text("TypeScript, JavaScript, Python, SQL, Ruby (familiar), Bash"),
        ]),
        para([
          text("Frameworks: ", { bold: true }),
          text("React, Next.js, Node.js, Express, Django, Vitest, Jest, Cypress"),
        ]),
        para([
          text("Data: ", { bold: true }),
          text("PostgreSQL, SQLite, Redis, Prisma, Drizzle ORM, Elasticsearch"),
        ]),
        para([
          text("Infrastructure: ", { bold: true }),
          text("Docker, AWS (EC2, RDS, S3), Fly.io, GitHub Actions, Sentry, Terraform (basic)"),
        ]),

        // ---- projects ----
        sectionHeading("Projects"),
        new Paragraph({
          children: [
            new TextRun({ text: "Fieldnote", font: FONT, size: 22, bold: true }),
            text(" - "),
            new ExternalHyperlink({
              children: [text("github.com/wrencalloway/fieldnote", { size: 22, style: "Hyperlink" })],
              link: "https://github.com/wrencalloway/fieldnote",
            }),
          ],
          spacing: { before: 100, after: 40 },
        }),
        para([
          text(
            "An offline-first note-taking app for field researchers, built with local-first sync " +
              "over CRDTs. About 600 stars; used by two university field programs.",
          ),
        ]),
        new Paragraph({
          children: [
            new TextRun({ text: "seedpacket", font: FONT, size: 22, bold: true }),
            text(" - "),
            new ExternalHyperlink({
              children: [text("github.com/wrencalloway/seedpacket", { size: 22, style: "Hyperlink" })],
              link: "https://github.com/wrencalloway/seedpacket",
            }),
          ],
          spacing: { before: 100, after: 40 },
        }),
        para([
          text(
            "A small library for generating realistic, referentially-consistent test data from a " +
              "database schema. Write-up at wrencalloway.example.dev/posts/seedpacket.",
          ),
        ]),
      ],
    },
  ],
});

const out = fileURLToPath(new URL("./plain.docx", import.meta.url));
await writeFile(out, await Packer.toBuffer(doc));
console.log(`wrote ${out}`);
