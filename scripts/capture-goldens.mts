/**
 * Captures the goldens the Django port is tested against (docs/migration/design.md
 * §12.1, §8.10, §15.2 step 0): runs the TypeScript functions over every fixture and
 * fixed inputs, and writes `backend/tests/goldens/**`.
 *
 *   pnpm tsx scripts/capture-goldens.mts
 *
 * Temporary: it goes with the TS server code in step 11.
 *
 * It touches no database, no network and no model:
 * - `DATABASE_URL` points at a closed port, and the pool's `query` is replaced by an
 *   in-memory fake that answers the few SELECTs the captured code paths make;
 * - `fetch` serves only the fixture files, and refuses everything else;
 * - `registry.languageModel` returns a capture model that records the call and throws,
 *   so nothing is ever sent to a provider (the UI-stream scenarios use scripted mock
 *   models of their own).
 *
 * Re-running it rewrites the same bytes: the clock is frozen, `Math.random` is
 * seeded, and the two Better Auth hashes (random salts) are kept when they still
 * verify with the installed library.
 */
/* eslint-disable @typescript-eslint/no-explicit-any */

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// --- Isolation, before any app module is imported ------------------------------------

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const FIXTURES = join(ROOT, "backend/tests/fixtures");
const GOLDENS = join(ROOT, "backend/tests/goldens");
/** Written here first, and swapped in only when every capture succeeded. */
const OUT = `${GOLDENS}.partial`;

/** The instant every capture sees as "now" (Sunday evening in Los Angeles, Monday in UTC). */
const NOW_ISO = "2026-10-05T03:30:00.000Z";
const TIME_ZONE = "America/Los_Angeles";

for (const key of Object.keys(process.env)) {
  if (
    /^(LLM_|OPENAI_|ANTHROPIC_|DEEPSEEK_|GOOGLE_|RAPIDAPI_|SEARCH_API_|JSEARCH_|BETTER_AUTH_|DATABASE_URL|STORAGE_DIR|SKIP_ENV_VALIDATION)/.test(
      key,
    )
  ) {
    delete process.env[key];
  }
}
Object.assign(process.env, {
  TZ: TIME_ZONE,
  // A closed port: nothing in this script may reach a real database.
  DATABASE_URL: "postgres://capture:capture@127.0.0.1:9/capture_goldens_no_database",
  LLM_DEFAULT: "openai:gpt-5.6-luna",
  LLM_EVALUATE: "openai:gpt-5.6-terra",
  LLM_TAILOR: "openai:gpt-5.6-terra",
  OPENAI_API_KEY: "capture-goldens-not-a-key",
  JSEARCH_ENABLED: "false",
  // Nothing here stores files; a write would fail loudly.
  STORAGE_DIR: "/dev/null/capture-goldens-storage",
});

// A seeded Math.random (mulberry32), so AI SDK ids are the same on every run.
let seed = 0x5eed;
function reseed(value: number) {
  seed = value >>> 0;
}
Math.random = () => {
  seed = (seed + 0x6d2b79f5) >>> 0;
  let t = seed;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};

// A frozen clock: `new Date()` and `Date.now()` are NOW_ISO; dated construction is untouched.
const RealDate = Date;
const FIXED_NOW = RealDate.parse(NOW_ISO);
function FrozenDate(this: unknown, ...args: any[]) {
  if (!new.target) return new RealDate(FIXED_NOW).toString();
  return args.length === 0 ? new RealDate(FIXED_NOW) : new (RealDate as any)(...args);
}
FrozenDate.prototype = RealDate.prototype;
Object.assign(FrozenDate, { now: () => FIXED_NOW, parse: RealDate.parse, UTC: RealDate.UTC });
globalThis.Date = FrozenDate as unknown as DateConstructor;

// The network: fixture files only.
const routes = new Map<string, { status: number; body: string; type: string }>();
function route(url: string, file: string, status = 200, type = "application/json") {
  routes.set(url, { status, body: readFileSync(join(FIXTURES, file), "utf8"), type });
}
function notFoundBody(file: string): { body: string; type: string } {
  // The `_not-found.txt` captures hold the raw exchange as blank-line-separated blocks:
  // the request, the status line and headers, the body, then notes.
  const [, head = "", body = ""] = readFileSync(join(FIXTURES, file), "utf8").split(/\n[ \t]*\n/);
  const typeLine = head.split("\n").find((line) => /^content-type:/i.test(line)) ?? "content-type: text/plain";
  return { body: body.trim(), type: typeLine.slice(typeLine.indexOf(":") + 1).trim() };
}
globalThis.fetch = (async (input: any) => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
  const hit = routes.get(url);
  if (!hit) throw new Error(`capture-goldens: the network is disabled (${url})`);
  return new Response(hit.body, { status: hit.status, headers: { "content-type": hit.type } });
}) as typeof fetch;

// --- App modules (imported only now, so they see the environment above) -------------

(globalThis as any).AI_SDK_LOG_WARNINGS = false;

const { pool } = await import("@/lib/db");
const { registry } = await import("@/lib/llm/registry");
const aiTest: any = await import("ai/test");
const ai: any = await import("ai");
const { z } = await import("zod");

const { normalizeGreenhouseJob, greenhouse } = await import("@/lib/sources/greenhouse");
const { normalizeLeverJob, lever } = await import("@/lib/sources/lever");
const { normalizeAshbyJob, ashby } = await import("@/lib/sources/ashby");
const { normalizeJSearchJob, atsFromApplyOptions, employerSlug } = await import("@/lib/sources/jsearch");
const clean = await import("@/lib/sources/clean");
const normalize = await import("@/lib/sources/normalize");
const dedupe = await import("@/lib/sources/dedupe");
const discovery = await import("@/lib/companies/discovery");
const { slugCandidates } = await import("@/lib/companies/verify");
const { fetchCareersPage } = await import("@/lib/sources/careers-page");
const { parsePdf } = await import("@/lib/resume/parse/pdf");
const { parseDocx } = await import("@/lib/resume/parse/docx");
const { detectGarble } = await import("@/lib/resume/parse/garble");
const { extractResume } = await import("@/lib/resume/extract");
const { ResumeSchema, factIds } = await import("@/lib/resume/schema");
const { resumeToText } = await import("@/lib/resume/text");
const template = await import("@/lib/resume/render/template");
const { tailorResume } = await import("@/lib/resume/tailor");
const { verifyTailored } = await import("@/lib/resume/verify");
const { deriveSearch } = await import("@/lib/ranking/prefilter");
const { scoreMatches } = await import("@/lib/ranking/fast-score");
const { runDeepEval } = await import("@/lib/ranking/deep-eval");
const { applyBrief, describeBrief, EMPTY_BRIEF } = await import("@/lib/ranking/brief");
const { describeUpdate } = await import("@/lib/chat/follow-up");
const { summarizeSearchRun, describeEvaluations } = await import("@/lib/runs/evaluate-run");
const { systemPrompt } = await import("@/lib/agents/system-prompt");
const { discoverCompanies } = await import("@/lib/agents/tools/companies");
const { jobCard, getJob } = await import("@/lib/agents/tools/search");
const interview = await import("@/lib/profile/interview");
const { DEFAULT_SETTINGS } = await import("@/lib/profile/settings");

// --- The fake database ---------------------------------------------------------------

/** Rows by table, keyed by snake_case column name, for the SELECTs a capture makes. */
let tables: Record<string, Array<Record<string, unknown>>> = {};

function fakeQuery(config: any, values?: unknown) {
  void values;
  const text: string = (typeof config === "string" ? config : config.text).trim();
  if (/^(insert|update|delete)\b/i.test(text)) {
    return Promise.resolve({ rows: [], rowCount: 0, fields: [] });
  }
  const match = /^select\s+([\s\S]*?)\s+from\s+"([a-z_]+)"/i.exec(text);
  if (!match) throw new Error(`capture-goldens: unexpected SQL: ${text.slice(0, 160)}`);
  const columns = match[1].split(/,\s*/).map((item) => {
    const ids = [...item.matchAll(/"([a-z0-9_]+)"/g)].map((m) => m[1]);
    return ids.at(-1) ?? item;
  });
  const objects = tables[match[2]] ?? [];
  const rows =
    typeof config === "object" && config.rowMode === "array"
      ? objects.map((row) => columns.map((column) => (column in row ? row[column] : null)))
      : objects;
  return Promise.resolve({ rows, rowCount: rows.length, fields: [] });
}
(pool as any).query = fakeQuery;
(pool as any).connect = () => {
  throw new Error("capture-goldens: no database connections");
};

// --- The capture model ---------------------------------------------------------------

class CaptureStop extends Error {
  constructor() {
    super("capture-goldens: prompt captured; no model is called");
    this.name = "CaptureStop";
  }
}

let captured: any[] = [];
const captureModel = new aiTest.MockLanguageModelV4({
  provider: "capture",
  modelId: "capture",
  doGenerate: async (options: any) => {
    captured.push(options);
    throw new CaptureStop();
  },
  doStream: async (options: any) => {
    captured.push(options);
    throw new CaptureStop();
  },
});
(registry as any).languageModel = () => captureModel;

// The ledger middleware logs a failed row write; with the fake pool there is none, but
// keep its noise out of the output either way.
const realConsoleError = console.error;
console.error = (...args: unknown[]) => {
  if (typeof args[0] === "string" && args[0].startsWith("[llm] failed to write ledger row")) return;
  // The stream_error scenario's scripted provider error, which the SDK logs as it streams.
  if (args[0] instanceof Error && args[0].message === "Provider overloaded") return;
  realConsoleError(...args);
};
const realConsoleWarn = console.warn;
console.warn = (...args: unknown[]) => {
  if (typeof args[0] === "string" && /^\[(llm|resume)\]/.test(args[0])) return;
  realConsoleWarn(...args);
};

/** A file part's bytes are not part of the golden: only that it is there and its type. */
function describePart(part: any) {
  if (part.type === "text") return { type: "text", text: part.text };
  if (part.type === "file") return { type: "file", mediaType: part.mediaType };
  return { type: part.type };
}

/** `{instructions, prompt}` as the model saw them: the system text and the user text. */
function promptOf(options: any) {
  const system = options.prompt.filter((m: any) => m.role === "system");
  const user = options.prompt.filter((m: any) => m.role === "user");
  const other = options.prompt.filter((m: any) => m.role !== "system" && m.role !== "user");
  if (system.length !== 1 || user.length !== 1 || other.length) {
    throw new Error(`capture-goldens: unexpected prompt shape (${options.prompt.map((m: any) => m.role)})`);
  }
  const parts = user[0].content;
  const textOnly = parts.every((part: any) => part.type === "text");
  return textOnly
    ? { instructions: system[0].content, prompt: parts.map((part: any) => part.text).join("") }
    : {
        instructions: system[0].content,
        prompt: parts.filter((p: any) => p.type === "text").map((p: any) => p.text).join(""),
        parts: parts.map(describePart),
      };
}

/** Runs `fn`, which must reach the model at least once, and returns the first call's prompt. */
async function capture(label: string, fn: () => Promise<unknown>) {
  captured = [];
  try {
    await fn();
  } catch (error) {
    if (!(error instanceof CaptureStop)) throw error;
  }
  if (captured.length === 0) throw new Error(`capture-goldens: ${label} never reached the model`);
  return promptOf(captured[0]);
}

// --- Output ----------------------------------------------------------------------------

const written: string[] = [];
function write(path: string, content: string) {
  const file = join(OUT, path);
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, content);
  written.push(path);
}
const writeJson = (path: string, value: unknown) =>
  write(path, `${JSON.stringify(value, jsonReplacer, 2)}\n`);

/** Dates as ISO strings; `undefined` kept visible where it matters (as the JSON key's absence). */
function jsonReplacer(this: any, key: string, value: unknown) {
  const raw = this[key];
  if (raw instanceof RealDate) return Number.isNaN(raw.getTime()) ? "Invalid Date" : raw.toISOString();
  return value;
}

const fixture = (path: string) => readFileSync(join(FIXTURES, path), "utf8");
const fixtureBytes = (path: string) => readFileSync(join(FIXTURES, path));
const sha256 = (value: string | Buffer) => createHash("sha256").update(value).digest("hex");

// Keep the Better Auth hashes across runs (random salts), then start from a clean tree.
const HASHES = "auth/better_auth_hashes.json";
const previousHashes = existsSync(join(GOLDENS, HASHES))
  ? (JSON.parse(readFileSync(join(GOLDENS, HASHES), "utf8")) as Array<{ label: string; password: string; hash: string }>)
  : [];
rmSync(OUT, { recursive: true, force: true });

// =====================================================================================
// Inputs shared by several goldens
// =====================================================================================

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const IDS = {
  link1: id(1),
  link2: id(2),
  work1: id(11),
  work2: id(12),
  edu1: id(21),
  project1: id(31),
  skill1: id(41),
  skill2: id(42),
  skill3: id(43),
  skill4: id(44),
  skill5: id(45),
  skill6: id(46),
};

const BASE_RESUME = ResumeSchema.parse({
  basics: {
    name: "Dana Reyes",
    headline: "Backend engineer",
    email: "dana@example.com",
    phone: "(555) 0134-2087",
    location: "Sacramento, CA",
    links: [
      { id: IDS.link1, label: "GitHub", url: "github.com/dana" },
      { id: IDS.link2, label: "LinkedIn", url: "https://www.linkedin.com/in/dana/" },
    ],
  },
  summary: "Backend-leaning engineer who likes boring, reliable systems.",
  work: [
    {
      id: IDS.work1,
      company: "Kestrel Logistics Group",
      position: "Software Engineer II",
      location: "Sacramento, CA",
      startDate: "March 2023",
      endDate: "Present",
      summary: "Owns the shipment-tracking API.",
      highlights: [
        "Cut p95 latency from 840 ms to 190 ms by moving hot reads to a Redis cache.",
        "Rebuilt the ingestion service in Go; it now handles 2.3M events a day.",
        "Mentored two interns through their first production launches.",
      ],
    },
    {
      id: IDS.work2,
      company: "Café Numérique",
      position: "Software Engineer",
      location: "Montréal, QC",
      startDate: "June 2021",
      endDate: "February 2023",
      highlights: [
        "Built the PostgreSQL schema for the loyalty programme.",
        "Wrote the Python ETL that feeds the finance dashboards.",
      ],
    },
  ],
  education: [
    {
      id: IDS.edu1,
      institution: "Bellwater State University",
      studyType: "B.S.",
      area: "Computer Science",
      startDate: "2017",
      endDate: "2021",
      score: "3.7 GPA",
      highlights: ["Dean’s list, 2019–2021"],
    },
  ],
  projects: [
    {
      id: IDS.project1,
      name: "Tideline",
      url: "github.com/dana/tideline",
      description: "A CLI for tide forecasts.",
      startDate: "2022",
      highlights: ["Used by 1,200 sailors."],
    },
  ],
  skills: [
    { id: IDS.skill1, name: "TypeScript", category: "Languages" },
    { id: IDS.skill2, name: "Go", category: "Languages" },
    { id: IDS.skill3, name: "PostgreSQL", category: "Data" },
    { id: IDS.skill4, name: "Redis", category: "Data" },
    { id: IDS.skill5, name: "Python", category: "Languages" },
    { id: IDS.skill6, name: "Docker" },
  ],
});

/** Empty sections: a name and one role, nothing else. */
const SPARSE_RESUME = ResumeSchema.parse({
  basics: { name: "Zoë Ñúñez" },
  work: [
    {
      id: id(51),
      company: "Ålesund Fiskeri AS",
      position: "Data analyst",
      startDate: null,
      endDate: null,
      highlights: [],
    },
  ],
});

const EMPTY_RESUME = ResumeSchema.parse({});

const PREFS_FULL = {
  target_roles: ["Backend engineer", "Platform engineer"],
  seniority: "mid" as const,
  locations: ["San Francisco Bay Area", "Remote (US)"],
  remote_pref: "hybrid" as const,
  salary_floor: { amount: 150000, currency: "USD", period: "year" as const },
  work_auth: "US citizen, no sponsorship needed",
  start_date: "January 2027",
  must_haves: ["Go or Python"],
  dealbreakers: ["On-call more than one week in four"],
};
const PREFS_FRACTIONAL = {
  ...PREFS_FULL,
  salary_floor: { amount: 124.128, currency: "USD", period: "hour" as const },
  must_haves: [],
  dealbreakers: null,
};
const PREFS_REQUIRED_ONLY = {
  ...interview.EMPTY_PREFERENCES,
  target_roles: ["Software engineer"],
  seniority: "entry" as const,
  locations: ["Zürich", "München"],
  remote_pref: "any" as const,
  work_auth: "EU citizen",
};
const PREFS_PARTIAL = {
  ...interview.EMPTY_PREFERENCES,
  target_roles: ["Software engineer"],
  locations: ["New York, NY"],
};

/** Just over the deep evaluation's 12,000-character cut (and the fast score's 4,000). */
const LONG_DESCRIPTION = Array.from(
  { length: 140 },
  (_, n) => `Paragraph ${n + 1}: you will build and run services in Go and PostgreSQL — café-grade reliability.`,
).join("\n\n");
if (LONG_DESCRIPTION.length <= 12_000) throw new Error("capture-goldens: LONG_DESCRIPTION is too short");

// =====================================================================================
// Adapters
// =====================================================================================

/**
 * A NormalizedJob as a golden. `raw` is the source payload itself (the fixture), so it is
 * left out; `descriptionRaw` is the posting's HTML exactly as fetched (or, for Lever,
 * stitched from the fixture's fields), so it is pinned by hash to keep the goldens small.
 */
function normalizedJob(job: any) {
  const { raw, descriptionRaw, ...rest } = job;
  void raw;
  return {
    ...rest,
    descriptionRaw:
      descriptionRaw == null
        ? null
        : { sha256: sha256(descriptionRaw), utf8Bytes: Buffer.byteLength(descriptionRaw, "utf8") },
  };
}

const BOARDS = [
  { source: "greenhouse", slug: "anthropic", file: "greenhouse/anthropic.json", adapter: greenhouse,
    url: "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true" },
  { source: "greenhouse", slug: "stripe", file: "greenhouse/stripe.json", adapter: greenhouse,
    url: "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true" },
  { source: "lever", slug: "matchgroup", file: "lever/matchgroup.json", adapter: lever,
    url: "https://api.lever.co/v0/postings/matchgroup?mode=json" },
  { source: "lever", slug: "spotify", file: "lever/spotify.json", adapter: lever,
    url: "https://api.lever.co/v0/postings/spotify?mode=json" },
  { source: "ashby", slug: "linear", file: "ashby/linear.json", adapter: ashby,
    url: "https://api.ashbyhq.com/posting-api/job-board/linear?includeCompensation=true" },
  { source: "ashby", slug: "vanta", file: "ashby/vanta.json", adapter: ashby,
    url: "https://api.ashbyhq.com/posting-api/job-board/vanta?includeCompensation=true" },
] as const;

const NOT_FOUND = [
  { source: "greenhouse", adapter: greenhouse, file: "greenhouse/_not-found.txt",
    url: "https://boards-api.greenhouse.io/v1/boards/foray-no-such-company-xyz123/jobs?content=true" },
  { source: "lever", adapter: lever, file: "lever/_not-found.txt",
    url: "https://api.lever.co/v0/postings/foray-no-such-company-xyz123?mode=json" },
  { source: "ashby", adapter: ashby, file: "ashby/_not-found.txt",
    url: "https://api.ashbyhq.com/posting-api/job-board/foray-no-such-company-xyz123?includeCompensation=true" },
] as const;

const removedByJob: Record<string, string[]> = {};
const allLocations = new Set<string>();

for (const board of BOARDS) {
  route(board.url, board.file);
  const result = await board.adapter.fetchBoard!(board.slug);
  writeJson(`adapters/${board.source}/${board.slug}.json`, {
    fixture: board.file,
    request: board.url,
    companySlug: result.companySlug,
    jobs: result.jobs.map(normalizedJob),
  });
  for (const job of result.jobs) {
    removedByJob[job.id] = clean.cleanDescription(job.descriptionRaw).removed;
    for (const location of job.locations) allLocations.add(location);
  }
}

// The per-posting normalisers, called directly, agree with fetchBoard; pinned anyway.
{
  const anthropic = JSON.parse(fixture("greenhouse/anthropic.json"));
  const direct = anthropic.jobs.map((job: any) => normalizedJob(normalizeGreenhouseJob("anthropic", job)));
  const viaBoard = JSON.parse(readFileSync(join(OUT, "adapters/greenhouse/anthropic.json"), "utf8")).jobs;
  if (JSON.stringify(JSON.parse(JSON.stringify(direct, jsonReplacer))) !== JSON.stringify(viaBoard)) {
    throw new Error("capture-goldens: normalizeGreenhouseJob disagrees with fetchBoard");
  }
  void normalizeLeverJob;
  void normalizeAshbyJob;
}

for (const miss of NOT_FOUND) {
  const { body, type } = notFoundBody(miss.file);
  routes.set(miss.url, { status: 404, body, type });
  let outcome: unknown;
  try {
    outcome = { jobs: (await miss.adapter.fetchBoard!("foray-no-such-company-xyz123")).jobs.length };
  } catch (error: any) {
    outcome = { error: { name: error.name, message: error.message } };
  }
  writeJson(`adapters/${miss.source}/_not-found.json`, {
    fixture: miss.file,
    request: miss.url,
    response: { status: 404, contentType: type, body },
    outcome,
  });
}

{
  const response = JSON.parse(fixture("jsearch/entry-level-sf.json"));
  const jobs = response.data.jobs as any[];
  writeJson("adapters/jsearch/entry-level-sf.json", {
    fixture: "jsearch/entry-level-sf.json",
    jobs: jobs.map((job) => {
      const normalized = normalizeJSearchJob(job);
      for (const location of normalized.locations) allLocations.add(location);
      removedByJob[normalized.id] = clean.cleanDescription(normalized.descriptionRaw).removed;
      return {
        job: normalizedJob(normalized),
        board: atsFromApplyOptions(job),
        employerSlug: employerSlug(job.employer_name ?? ""),
      };
    }),
  });
}

// =====================================================================================
// Clean, links, normalize, dedupe, discovery
// =====================================================================================

const tail = (body: string, ending: string) => `${body}${ending}`;
const CLEAN_INPUTS: Array<string | null> = [
  null,
  "",
  "Just plain text.",
  "&lt;p&gt;We are hiring an engineer.&lt;/p&gt;",
  "<p>Ben &amp; Jerry&rsquo;s</p>",
  "<ul><li>First thing</li><li>Second thing</li></ul>",
  "<div><script>alert(1)</script><style>p{}</style><p>Kept</p></div>",
  "<p>One</p>\r\n<p>Two&nbsp;&nbsp;spaces</p><br/><br/><br/><p>Three &#8212; &#x2014; &hellip;</p>",
  "Unknown &entity; and &#xZZ; stay; &AMP; decodes",
  tail("We are hiring.\n\n".repeat(30), "Acme Corp is an equal opportunity employer and does not discriminate."),
  tail(
    "Responsibilities include shipping code.\n\n".repeat(20),
    "E-Verify is used by this employer.\n\nIf you need an accommodation, contact us.",
  ),
  `We are an equal opportunity employer and you will own that programme.\n\n${"You will ship features.\n\n".repeat(40)}`,
  tail(
    "<p>Build things.</p>".repeat(25),
    "<p>• We do not accept unsolicited resumes from agencies.</p><p>California applicants: see our privacy notice.</p><p>Pay transparency: the range is $150,000–$200,000.</p><p>Pursuant to the San Francisco Fair Chance Ordinance, we consider all applicants.</p>",
  ),
  "Café — naïve façade 🚀\n\n\n\nsecond paragraph\t\twith  tabs",
];

writeJson("clean/cases.json", {
  cleanDescription: CLEAN_INPUTS.map((input) => ({ input, output: clean.cleanDescription(input) })),
  htmlToText: CLEAN_INPUTS.filter((input): input is string => typeof input === "string").map((input) => ({
    input,
    output: clean.htmlToText(input),
  })),
  decodeEntities: [
    "&amp;&lt;&gt;&quot;&apos;&nbsp;&ndash;&mdash;&rsquo;&lsquo;&ldquo;&rdquo;&hellip;&bull;&middot;",
    "&#65;&#x42;&#X43;&#128512;&#x1F600;",
    "&unknown; &#; &#x; &AMP; &Lt;",
  ].map((input) => ({ input, output: clean.decodeEntities(input) })),
});
writeJson("clean/fixture_removed.json", removedByJob);

const CAREERS_PAGES = [
  { slug: "retool", name: "Retool", file: "careers-pages/retool.html", url: "https://retool.com/careers" },
  { slug: "crusoe", name: "Crusoe", file: "careers-pages/crusoe.html", url: "https://www.crusoe.ai/about/careers" },
];
for (const page of CAREERS_PAGES) {
  const html = fixture(page.file);
  write(`links/${page.slug}.with-links.txt`, clean.htmlToTextWithLinks(html, page.url));
  write(`links/${page.slug}.text.txt`, clean.htmlToText(html));
}
writeJson("links/cases.json", [
  ['<a href="/careers/swe">Software Engineer</a>', undefined],
  ['<a href="/careers/swe">SWE</a>', "https://acme.com/careers"],
  ['<a href="#top">Back to top</a><a href="mailto:jobs@acme.com">Email us</a>', "https://acme.com"],
  ['<a href="javascript:void(0)">Open</a>', "https://acme.com"],
  ['<a href="/x"><span>Staff</span> <b>Engineer</b></a>', undefined],
  ['<a href="/x"><img src="y.png"></a>', undefined],
  ["<a href='jobs/1?ref=a&amp;b=c'>Job &amp; more</a>", "https://acme.com/careers/"],
  ['<a class="x" href="http://[bad">Broken</a>', "https://acme.com"],
  ['<p>Before <a href="https://other.example/x">Other</a> after</p><ul><li><a href="/a">A</a></li></ul>', "https://acme.com"],
].map(([html, base]) => ({ html, baseUrl: base ?? null, output: clean.htmlToTextWithLinks(html as string, base) })));

const LOCATION_INPUTS = [
  "San Francisco, CA",
  "Austin, Texas",
  "SF Bay Area",
  "Mountain View, CA",
  "Oakland, CA",
  "Palo Alto, CA",
  "New York, NY (Remote)",
  "Seattle, WA - Hybrid",
  "  San Francisco, CA (Remote)  ",
  "Remote",
  "London, United Kingdom",
  "London, UK",
  "Dublin, Ireland",
  "Berlin",
  "San Jose, CA",
  "Toronto, ON, Canada",
  "Montréal, QC",
  "Zürich, Switzerland",
  "Boston, MA, USA",
  "New York City, NY / Remote",
  "Austin | TX",
  "Remote - US",
  "",
  "   ",
  ...[...allLocations].sort(),
];
writeJson("normalize/locations.json", [...new Set(LOCATION_INPUTS)].map((input) => ({
  input,
  output: normalize.parseLocation(input),
})));
writeJson("normalize/primary_location.json", [
  ["Dublin, Ireland", "San Jose, CA"],
  ["Dublin, Ireland", "Berlin"],
  [],
  ["Remote", "SF Bay Area"],
  ["New York City, NY", "San Francisco, CA", "Oakland, CA"],
].map((input) => ({ input, output: normalize.primaryLocation(input) })));
writeJson("normalize/remote.json", [
  { location: "San Francisco", isRemoteFlag: true },
  { location: "Hybrid remote" },
  { location: "Hybrid Remote - Austin, TX" },
  { location: "Remote - US" },
  { location: "", title: "Software Engineer (Remote)" },
  { location: "San Francisco, CA" },
  { location: "San Francisco, CA", description: "You will work with remote teams across the company." },
  { location: "", description: "This is a fully remote position." },
  { location: "", description: "Hybrid: three days a week in our office." },
  { location: null, title: null, description: null },
  { location: "On-site, Denver" },
  { location: "In-office" },
  { location: "Earth", title: "Engineer", description: `${"x ".repeat(400)}remote ok` },
  { location: "", title: "WFH engineer" },
  { location: "Remote-first", isRemoteFlag: false },
].map((input) => ({ input, output: normalize.detectRemote(input) })));

const SALARY_INPUTS = [
  "The salary range is $150,000 - $200,000 per year",
  "$180-220k",
  "$180-220k annually",
  "$55 - $70 per hour",
  "£70,000 – £95,000 per year",
  "Salary: 90,000 - 120,000 EUR annually",
  "Founded 2015 - 2020, we have grown fast",
  "a team of 5 - 10 engineers",
  "$200,000 - $150,000",
  "Competitive salary and equity",
  "C$95k — C$120k per year",
  "US$1.2m to US$1.5m OTE",
  "Pay: 124.128 – 130.5 per hour",
  "Base pay 7,982 - 9,100 a month",
  "The base salary is 150000 to 150000",
  "$4,000 - $5,000 monthly",
  "Compensation: $1-2m",
  null,
  "",
];
writeJson("normalize/salaries.json", SALARY_INPUTS.map((input) => ({ input, output: normalize.parseSalary(input) })));
writeJson("normalize/plausible_salary.json", [
  { min: 150, max: 200, currency: "USD", period: "year" },
  { min: 150000, max: 200000, currency: "USD", period: "year" },
  { min: 25, max: 40, currency: "CAD", period: "hour" },
  { min: 25, max: 40, currency: "CAD", period: "year" },
  { min: 200000, max: 150000, currency: "USD", period: "year" },
  { min: null, max: null, currency: null, period: null },
  { min: 600, max: 9000, currency: null, period: "month" },
  { min: 20000, max: 30000, currency: null, period: null },
].map((input) => ({ input, output: normalize.isPlausibleSalary(input as any) })));

writeJson("dedupe/cases.json", {
  normalizeCompany: ["Stripe, Inc.", "Ben & Jerry's LLC", "Café Numérique S.A.", "ACME CORP.", "Foo GmbH & Co. KG"].map(
    (input) => ({ input, output: dedupe.normalizeCompany(input) }),
  ),
  normalizeTitle: [
    "Senior Software Engineer (Remote)",
    "Software Engineer II [Payments] REQ-12345",
    "C++ Engineer #4567",
    "C# Developer - Remote - US",
    "Staff Engineer – Full-time",
    "Ingénieur logiciel",
  ].map((input) => ({ input, output: dedupe.normalizeTitle(input) })),
  normalizeLocationForKey: ["San Francisco, CA", null, "São Paulo"].map((input) => ({
    input,
    output: dedupe.normalizeLocationForKey(input),
  })),
  dedupeKey: [
    { companyName: "Stripe", title: "Backend Engineer", primaryLocation: "San Francisco" },
    { companyName: "Stripe, Inc.", title: "Backend Engineer (Remote)", primaryLocation: "San Francisco" },
    { companyName: "Stripe", title: "Backend Engineer", primaryLocation: null },
  ].map((input) => ({ input, output: dedupe.dedupeKey(input) })),
  contentHash: [
    { title: "Engineer", locations: ["B", "A"], descriptionRaw: "<p>x</p>", salaryMin: 150000, salaryMax: 200000 },
    { title: "Engineer", locations: ["A", "B"], descriptionRaw: "<p>x</p>", salaryMin: 150000, salaryMax: 200000 },
    { title: "Engineer", locations: [], descriptionRaw: null, salaryMin: 124.128, salaryMax: null },
    { title: "Ingénieur", locations: ["Montréal"] },
  ].map((input) => ({ input, output: dedupe.contentHash(input) })),
  jobId: [{ input: ["greenhouse", "anthropic", "4567"], output: dedupe.jobId("greenhouse", "anthropic", "4567") }],
  webJobId: ["https://retool.com/careers/jobs/123", "https://www.crusoe.ai/about/careers?gh_jid=9"].map((url) => ({
    input: { companySlug: "retool", url },
    output: dedupe.webJobId("retool", url),
  })),
});

writeJson("discovery/cases.json", {
  careersPages: CAREERS_PAGES.map((page) => {
    const html = fixture(page.file);
    return {
      fixture: page.file,
      baseUrl: page.url,
      findAtsLinks: discovery.findAtsLinks(html),
      hasUnsupportedAts: discovery.hasUnsupportedAts(html),
      findCareersLinks: discovery.findCareersLinks(html, page.url),
    };
  }),
  normalizeDomain: [
    "Stripe.com",
    "https://www.stripe.com/jobs",
    "http://linear.app/",
    "linear",
    "",
    "  ANTHROPIC.COM ",
    "boards.greenhouse.io/anthropic",
  ].map((input) => ({ input, output: discovery.normalizeDomain(input) })),
  slugCandidates: ["Stripe", "Ben & Jerry's", "Scale AI, Inc.", "Café Numérique", "A", "Notion Labs Corp."].map(
    (input) => ({ input, output: slugCandidates(input) }),
  ),
});

// =====================================================================================
// Resumes: PDF text, DOCX text, garble decisions, resume text, template HTML
// =====================================================================================

const RESUME_FIXTURES = ["single-column.pdf", "two-column.pdf", "serif-dense.pdf"];
for (const name of RESUME_FIXTURES) {
  const parsed = await parsePdf(fixtureBytes(`resumes/${name}`));
  writeJson(`resumes/pdf/${name.replace(/\.pdf$/, "")}.json`, {
    fixture: `resumes/${name}`,
    sha256: sha256(fixtureBytes(`resumes/${name}`)),
    ...parsed,
  });
}
{
  const parsed = await parseDocx(fixtureBytes("resumes/plain.docx"));
  writeJson("resumes/docx/plain.json", {
    fixture: "resumes/plain.docx",
    sha256: sha256(fixtureBytes("resumes/plain.docx")),
    ...parsed,
  });
}
writeJson("resumes/garble_cases.json", [
  { text: "", pageCount: 1 },
  { text: "Short.", pageCount: 2 },
  { text: "S U M M A R Y  I L O N A  B E R G Q U I S T  ".repeat(20), pageCount: 1 },
  { text: "Experiencedsoftwareengineerwithtenyearsofbuildingsystems".repeat(10), pageCount: 1 },
  { text: `${"Normal words in a sentence. ".repeat(20)}�����`, pageCount: 1 },
  { text: "1234 5678 9012 3456 7890 ".repeat(40), pageCount: 1 },
  { text: "A perfectly ordinary resume line with words. ".repeat(30), pageCount: 1 },
  { text: "Ingénieur logiciel à Montréal, spécialisé en systèmes répartis. ".repeat(20), pageCount: 0 },
].map((input) => ({ input, output: detectGarble(input.text, input.pageCount) })));

writeJson("strings/resume_text.json", [
  { name: "base", resume: BASE_RESUME, sections: null },
  { name: "base_work_and_skills", resume: BASE_RESUME, sections: ["work", "skills"] },
  { name: "sparse", resume: SPARSE_RESUME, sections: null },
  { name: "empty", resume: EMPTY_RESUME, sections: null },
].map((item) => ({
  ...item,
  output: item.sections ? resumeToText(item.resume, item.sections) : resumeToText(item.resume),
})));

const HOSTILE_RESUME = ResumeSchema.parse({
  ...BASE_RESUME,
  basics: {
    ...BASE_RESUME.basics,
    name: 'Dana "DR" <Reyes>',
    links: [
      ...BASE_RESUME.basics.links,
      { id: id(3), label: "Bad", url: "javascript:alert(1)" },
      { id: id(4), label: "Email", url: "dana@example.com" },
      { id: id(5), label: "Port", url: "localhost:8080/x" },
    ],
  },
  work: [
    {
      ...BASE_RESUME.work[0],
      company: "Kestrel & Sons <script>alert(1)</script>",
      highlights: ["Shipped `x < y` checks & 'quotes' \"too\"."],
    },
    { ...BASE_RESUME.work[1], startDate: "2021", endDate: "2021" },
  ],
  skills: [...BASE_RESUME.skills, { id: id(47), name: "Kubernetes", category: "" }],
});
const TEMPLATE_CASES = [
  { name: "base", resume: BASE_RESUME, options: {} },
  { name: "base-compact-titled", resume: BASE_RESUME, options: { density: "compact", title: "Dana Reyes — Stripe" } },
  { name: "sparse", resume: SPARSE_RESUME, options: {} },
  { name: "empty", resume: EMPTY_RESUME, options: {} },
  { name: "hostile", resume: HOSTILE_RESUME, options: {} },
];
for (const item of TEMPLATE_CASES) {
  write(`template/${item.name}.html`, template.renderResumeHtml(item.resume, item.options as any));
}
writeJson("template/cases.json", TEMPLATE_CASES.map(({ name, resume, options }) => ({ name, resume, options, html: `${name}.html` })));
writeJson("template/helpers.json", {
  escapeHtml: [`<a href="x" onclick='y'>&</a>`, null, undefined, 42, "Café ✓"].map((input) => ({
    input: input === undefined ? "__undefined__" : input,
    output: template.escapeHtml(input),
  })),
  safeHref: [
    "github.com/dana",
    "https://example.com/x",
    "http://example.com",
    "mailto:dana@example.com",
    "dana@example.com",
    "javascript:alert(1)",
    "data:text/html,x",
    "localhost:8080/x",
    "www.example.co.uk/path?q=1#frag",
    "has space.com",
    "",
    null,
  ].map((input) => ({ input, output: template.safeHref(input) })),
  displayUrl: ["https://www.github.com/x/", "mailto:a@b.co", "http://example.com///", " www.linkedin.com/in/dana/ "].map(
    (input) => ({ input, output: template.displayUrl(input) }),
  ),
  dateRange: [
    ["March 2023", "Present"],
    ["2021", "2021"],
    [null, "2021"],
    ["2019", null],
    [null, null],
    ["  June 2021 ", " "],
  ].map(([start, end]) => ({ input: [start, end], output: template.dateRange(start, end) })),
});

// =====================================================================================
// Strings: run updates, summaries, evaluations, briefs, preferences
// =====================================================================================

const SEARCH_RUN = {
  id: "run-1",
  kind: "search",
  input: { prompt: "entry-level roles at Anthropic", brief: { companies: ["Anthropic"], seniority: "entry" } },
  output: { candidates: 20 },
};
const PROPOSED = [
  {
    title: "Software Engineer, Research Data Platform",
    company: "Anthropic",
    score: 78,
    headline: "Strong data platform overlap",
    locations: "San Francisco, CA",
  },
  { title: "Fellow — Café Systems", company: "Anthropic", score: null, headline: null, locations: "" },
];
const UPDATE_CASES: Array<[string, any, any, any, any?]> = [
  ["deep_eval_gate", SEARCH_RUN, { kind: "approval.requested", message: "Waiting for your go-ahead." },
    { kind: "deep_eval_batch", title: "Deep-evaluate 8 roles (about $0.25)", payload: { items: [] } }, PROPOSED],
  ["deep_eval_gate_no_roles", SEARCH_RUN, { kind: "approval.requested", message: "Waiting." },
    { kind: "deep_eval_batch", title: "Deep-evaluate 1 role", payload: {} }, []],
  ["shortlist_from_payload", SEARCH_RUN, { kind: "approval.requested", message: "" },
    { kind: "shortlist_proposal", title: "Shortlist 2 roles",
      payload: { items: [{ title: "Software Engineer", company: "Anthropic" }, { title: "Fellow", company: "Anthropic" }] } }],
  ["shortlist_one_proposed", SEARCH_RUN, { kind: "approval.requested", message: "" },
    { kind: "shortlist_proposal", title: "Shortlist 1 role", payload: { items: [] } }, PROPOSED.slice(0, 1)],
  ["other_approval", { id: "run-9", kind: "tailor", input: { prompt: "Tailor your resume for Engineer at Stripe" }, output: null },
    { kind: "approval.requested", message: "Approve the tailored resume." }, { kind: "tailor", title: "Use this resume?", payload: {} }],
  ["other_approval_no_approval", { id: "run-9", kind: "company_discovery", input: {}, output: null },
    { kind: "approval.requested", message: "Needs a decision." }, null],
  ["search_completed", SEARCH_RUN, { kind: "run.completed", message: "Found 20 roles." }, null],
  ["search_completed_empty", { ...SEARCH_RUN, output: { candidates: 0 } },
    { kind: "run.completed", message: "No open roles matched." }, null],
  ["search_hints_only", { id: "run-4", kind: "search", input: { prompt: "find me remote jobs", hints: { remote: "remote", roles: ["Senior backend engineer"] } }, output: { candidates: 3 } },
    { kind: "run.completed", message: "Found 3 roles." }, null],
  ["search_prompt_only", { id: "run-5", kind: "search", input: { prompt: "Search with my saved preferences" }, output: null },
    { kind: "run.completed", message: "Found 2 roles." }, null],
  ["search_no_input", { id: "run-6", kind: "search", input: null, output: null }, { kind: "run.failed", message: "The board fetch failed." }, null],
  ["tailor_failed", { id: "run-2", kind: "tailor", input: { prompt: "Tailor your resume for Engineer at Stripe" }, output: null },
    { kind: "tailor.failed", message: "The model timed out." }, null],
  ["tailor_done", { id: "run-3", kind: "tailor", input: { prompt: "Tailor your resume for Engineer at Stripe" }, output: null },
    { kind: "tailor.done", message: "Your resume is ready." }, null],
  ["tailor_no_prompt", { id: "run-3b", kind: "tailor", input: {}, output: null }, { kind: "run.completed", message: "Done." }, null],
  ["rank_deep_done", { id: "run-7", kind: "rank_deep", input: {}, output: { evaluated: 2 } },
    { kind: "run.completed", message: "Evaluated 2 roles: Engineer at Stripe (82, worth applying)." }, null],
  ["rank_deep_failed", { id: "run-7", kind: "rank_deep", input: null, output: null }, { kind: "run.failed", message: "No resume." }, null],
  ["render_done", { id: "run-8", kind: "resume_render", input: null, output: null }, { kind: "run.completed", message: "Rendered 1 page." }, null],
  ["discovery_done", { id: "run-10", kind: "company_discovery", input: { prompt: "Find boards for Linear" }, output: null },
    { kind: "run.completed", message: "Registered Linear." }, null],
  ["discovery_no_prompt", { id: "run-11", kind: "company_discovery", input: {}, output: null },
    { kind: "run.completed", message: "Registered 3 of 5." }, null],
  ["unknown_kind", { id: "run-12", kind: "mystery", input: {}, output: null }, { kind: "run.completed", message: "ok" }, null],
  ["unknown_kind_failed", { id: "run-13", kind: "mystery", input: {}, output: null }, { kind: "run.failed", message: "boom" }, null],
];
writeJson("strings/describe_update.json", UPDATE_CASES.map(([name, run, event, approval, proposed]) => ({
  name,
  input: { run, event, approval, proposed: proposed ?? [] },
  output: describeUpdate(run, event, approval, proposed ?? []),
})));

const base = { candidates: 60, newMatches: 60, scored: 60, autoArchived: 0, evaluated: 0, shortlisted: 0, topMatchIds: [], recommended: 0, strong: 0, good: 0 };
writeJson("strings/summarize_search_run.json", [
  { ...base, candidates: 0, newMatches: 0, scored: 0 },
  { ...base, evaluated: 12, recommended: 5, shortlisted: 4 },
  { ...base, evaluated: 12, recommended: 1, shortlisted: 0 },
  { ...base, evaluated: 3, recommended: 0 },
  { ...base, newMatches: 0, scored: 0, strong: 1, good: 2, autoArchived: 1 },
  { ...base, newMatches: 7, strong: 3, good: 1, autoArchived: 5 },
  { ...base, candidates: 1, newMatches: 1, scored: 1 },
  { ...base, scored: 0 },
].map((input) => ({ input, output: summarizeSearchRun(input) })));

const evaluation = (title: string, company: string, score: number, recommendation: string) => ({
  matchId: `m-${title}`,
  jobId: `j-${title}`,
  title,
  company,
  evaluation: { score, recommendation },
});
const EVALS = [
  evaluation("Senior Backend Engineer", "Stripe", 82, "apply"),
  evaluation("Platform Engineer", "Café Numérique", 91, "strong"),
  evaluation("Data Engineer", "Linear", 55, "maybe"),
  evaluation("SRE", "Vanta", 30, "skip"),
];
writeJson("strings/describe_evaluations.json", [
  { evaluations: EVALS.slice(0, 1), failed: 0 },
  { evaluations: EVALS.slice(0, 2), failed: 0 },
  { evaluations: EVALS.slice(0, 3), failed: 1 },
  { evaluations: EVALS, failed: 2 },
  { evaluations: EVALS.slice(3), failed: 0 },
].map((input) => ({ input, output: describeEvaluations(input.evaluations as any, input.failed) })));

const BRIEFS = [
  EMPTY_BRIEF,
  { ...EMPTY_BRIEF, companies: ["Anthropic"], seniority: "entry" },
  { ...EMPTY_BRIEF, roles: ["software engineer"], seniority: "entry", companies: ["Anthropic"], locations: ["San Francisco Bay Area"] },
  { ...EMPTY_BRIEF, roles: ["Senior Backend Engineer"], seniority: "senior" },
  { ...EMPTY_BRIEF, roles: ["backend engineer", "platform engineer", "SRE"], remote: "remote" },
  { ...EMPTY_BRIEF, roles: ["data scientist"], companies: ["Stripe", "Linear"], remote: "any" },
  { ...EMPTY_BRIEF, seniority: "staff_plus", locations: ["Zürich", "München", "Wien"], remote: "onsite" },
  { ...EMPTY_BRIEF, seniority: "intern", remote: "hybrid", focus: "climate" },
  { ...EMPTY_BRIEF, seniority: "new_grad" },
  { ...EMPTY_BRIEF, seniority: "mid", roles: ["new grad engineer"] },
  { ...EMPTY_BRIEF, focus: "only the focus is set" },
];
writeJson("strings/describe_brief.json", BRIEFS.map((input) => ({ input, output: describeBrief(input as any) })));

writeJson("strings/describe_preferences.json", [
  interview.EMPTY_PREFERENCES,
  PREFS_FULL,
  PREFS_FRACTIONAL,
  PREFS_REQUIRED_ONLY,
  PREFS_PARTIAL,
].map((input) => ({ input, output: interview.describePreferences(input as any) })));

// =====================================================================================
// Prompt goldens (§8.10)
// =====================================================================================

type PromptCase = { name: string; input: unknown; run: () => Promise<unknown> };
async function prompts(builder: string, cases: PromptCase[]) {
  for (const item of cases) {
    const result = await capture(`${builder}/${item.name}`, item.run);
    writeJson(`prompts/${builder}/${item.name}.json`, { builder, case: item.name, input: item.input, ...result });
  }
}

// deriveSearch (purpose score)
await prompts("derive_search", [
  { name: "request_with_hints", input: { preferences: PREFS_FULL, request: "entry-level roles at Anthropic, remote is fine", hints: { companies: ["Anthropic"], seniority: "entry", roles: null, locations: null, remote: "any", focus: "" } } },
  { name: "no_request", input: { preferences: PREFS_REQUIRED_ONLY, request: null, hints: null } },
  { name: "request_no_hints_unicode", input: { preferences: PREFS_FRACTIONAL, request: "Ingénieur backend à Montréal — café-adjacent startups", hints: {} } },
].map((item) => ({ ...item, run: () => deriveSearch(item.input as any) })));

// Fast score (purpose score): scoreMatches reads two tables, served by the fake pool.
const FAST_POSTINGS = [
  { name: "integral_salary_long_description", resume: BASE_RESUME, preferences: PREFS_FULL, focus: "Prefer teams that own infrastructure",
    job: { id: "greenhouse:acme:1", title: "Backend Engineer — Café Platform", company_name: "Acme Café", locations: ["San Francisco, CA", "Remote (US)"], remote: "hybrid", salary_min: "150000", salary_max: "200000", description_text: LONG_DESCRIPTION } },
  { name: "fractional_salary_sparse_resume", resume: SPARSE_RESUME, preferences: PREFS_FRACTIONAL, focus: null,
    job: { id: "lever:acme:2", title: "Data Engineer", company_name: "Ålesund Fiskeri", locations: [], remote: "unknown", salary_min: "124.128", salary_max: "130.5", description_text: "Short description with ünïcödé." } },
  { name: "missing_max_no_resume", resume: null, preferences: PREFS_REQUIRED_ONLY, focus: null,
    job: { id: "ashby:acme:3", title: "Platform Engineer", company_name: "Acme", locations: ["Zürich"], remote: "onsite", salary_min: "150000", salary_max: null, description_text: null } },
  { name: "no_salary_empty_resume", resume: EMPTY_RESUME, preferences: interview.EMPTY_PREFERENCES, focus: "",
    job: { id: "web:acme:4", title: "SRE", company_name: "Acme", locations: ["Remote"], remote: "remote", salary_min: null, salary_max: null, description_text: "" } },
];
await prompts("fast_score", FAST_POSTINGS.map((item) => ({
  name: item.name,
  input: { resume: item.resume, preferences: item.preferences, focus: item.focus, posting: item.job,
    note: "posting salaries are the strings Postgres returns for numeric columns" },
  run: async () => {
    tables = { job_matches: [{ job_id: item.job.id }], jobs: [item.job] };
    const outcome = await scoreMatches({
      userId: "capture-user",
      preferences: item.preferences as any,
      focus: item.focus,
      resume: item.resume,
      jobIds: [item.job.id],
    });
    tables = {};
    return outcome;
  },
})));

// Deep evaluation (purpose evaluate)
const candidateFor = (resume: any, preferences: any, brief: any) => ({
  resumeId: "resume-1",
  resumeVersion: 1,
  resumeText: resumeToText(resume),
  preferences: applyBrief(preferences, brief ?? EMPTY_BRIEF),
  focus: brief?.focus ?? null,
  factIds: new Set(factIds(resume)),
});
const DEEP_CASES = [
  { name: "integral_range_with_brief", resume: BASE_RESUME, preferences: PREFS_FULL,
    brief: { ...EMPTY_BRIEF, seniority: "entry", companies: ["Anthropic"], focus: "Research infrastructure" },
    posting: { title: "Software Engineer, Research Data Platform", company: "Anthropic", locations: ["San Francisco, CA", "New York City, NY"], remote: "hybrid", employmentType: "Full-time", salaryMin: "150000", salaryMax: "200000", salaryCurrency: "USD", salaryPeriod: "year", postedAt: new RealDate("2026-10-01T18:00:00.000Z"), description: "Build the data platform that research runs on. Go, Python, PostgreSQL." } },
  { name: "fractional_min_only_one_day", resume: SPARSE_RESUME, preferences: PREFS_FRACTIONAL, brief: null,
    posting: { title: "Analyste de données", company: "Café Numérique", locations: [], remote: "unknown", employmentType: null, salaryMin: "124.128", salaryMax: null, salaryCurrency: null, salaryPeriod: null, postedAt: new RealDate("2026-10-04T01:00:00.000Z"), description: "  Données, SQL, tableaux de bord.  " } },
  { name: "equal_min_max_truncated", resume: BASE_RESUME, preferences: PREFS_REQUIRED_ONLY, brief: null,
    posting: { title: "Platform Engineer", company: "Acme", locations: ["Remote"], remote: "remote", employmentType: "Contract", salaryMin: 7.982, salaryMax: 7.982, salaryCurrency: "EUR", salaryPeriod: "hour", postedAt: new RealDate("2026-06-01T00:00:00.000Z"), description: LONG_DESCRIPTION } },
  { name: "no_pay_no_date_no_description", resume: EMPTY_RESUME, preferences: interview.EMPTY_PREFERENCES, brief: null,
    posting: { title: "SRE", company: "Acme", locations: ["Austin, TX"], remote: "onsite", employmentType: null, salaryMin: null, salaryMax: null, salaryCurrency: null, salaryPeriod: null, postedAt: null, description: null } },
];
await prompts("deep_eval", DEEP_CASES.map((item) => ({
  name: item.name,
  input: { resume: item.resume, preferences: item.preferences, brief: item.brief, posting: item.posting, now: NOW_ISO },
  run: () =>
    runDeepEval({
      candidate: candidateFor(item.resume, item.preferences, item.brief) as any,
      posting: item.posting as any,
      now: new RealDate(FIXED_NOW),
    }),
})));

// Resume extraction (purpose extract): the text path for every resume fixture, and the
// vision path's text parts (the page images are listed, not stored).
await prompts("extract_text", [
  ...RESUME_FIXTURES.map((name) => ({
    name: name.replace(/\.pdf$/, ""),
    input: { fixture: `resumes/${name}`, mime: "application/pdf" },
    run: () => extractResume({ data: fixtureBytes(`resumes/${name}`), mime: "application/pdf" }),
  })),
  {
    name: "plain-docx",
    input: { fixture: "resumes/plain.docx", mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" },
    run: () =>
      extractResume({
        data: fixtureBytes("resumes/plain.docx"),
        mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      }),
  },
]);
await prompts("extract_vision", [
  {
    name: "single-column-forced",
    input: { fixture: "resumes/single-column.pdf", mime: "application/pdf", forceVision: true },
    run: () => extractResume({ data: fixtureBytes("resumes/single-column.pdf"), mime: "application/pdf", forceVision: true }),
  },
]);

// Tailoring (purpose tailor)
const DEEP_EVAL_STORED = {
  score: 84,
  recommendation: "apply",
  summary: "You have the backend depth this role wants.",
  matched_requirements: [
    { requirement: "Go services in production", evidence: "Rebuilt the ingestion service in Go", fact_ids: [IDS.work1], strength: "strong" },
    { requirement: "PostgreSQL", evidence: "Built the PostgreSQL schema", fact_ids: [IDS.work2], strength: "partial" },
    "A plain-string requirement from an older shape",
  ],
  gaps: [{ gap: "Kubernetes", severity: "learnable", note: "Not on the resume." }, "On-call leadership"],
  talking_points: ["Lead with the 840 ms → 190 ms latency work.", `${"Very long point ".repeat(30)}end`],
  risk_flags: [],
};
const TAILOR_CASES = [
  { name: "with_notes_and_goals", base: BASE_RESUME, preferences: PREFS_FULL,
    match: { deepEval: DEEP_EVAL_STORED, fastEval: { score: 80, hard_requirement_misses: ["Kubernetes", "5+ years of Rust"] } },
    job: { title: "Backend Engineer", companyName: "Stripe", locations: ["San Francisco, CA", "Seattle, WA"], remote: "hybrid", descriptionText: "We need Go and PostgreSQL. Kubernetes a plus.", descriptionRaw: "<p>ignored</p>" } },
  { name: "html_only_no_notes", base: SPARSE_RESUME, preferences: null, match: null,
    job: { title: "Analyste — données", companyName: "Café Numérique", locations: [], remote: "unknown", descriptionText: "   ", descriptionRaw: "&lt;p&gt;Données &amp; SQL&lt;/p&gt;<ul><li>Tableaux</li></ul>" } },
  { name: "no_description_fast_only", base: BASE_RESUME, preferences: { ...interview.EMPTY_PREFERENCES, seniority: "senior" },
    match: { deepEval: null, fastEval: { hard_requirement_misses: ["Clearance"] } },
    job: { title: "SRE", companyName: "Acme", locations: ["Remote"], remote: "remote", descriptionText: null, descriptionRaw: null } },
];
await prompts("tailor", TAILOR_CASES.map((item) => ({
  name: item.name,
  input: { base: item.base, job: item.job, match: item.match, preferences: item.preferences },
  run: () => tailorResume({ base: item.base, job: item.job as any, match: item.match as any, preferences: item.preferences as any }),
})));

// The verify judge (purpose verify): only lines that pass the rules reach it.
const TAILORED = ResumeSchema.parse({
  ...BASE_RESUME,
  summary: "Backend engineer who builds reliable systems in Go and PostgreSQL.",
  work: [
    {
      ...BASE_RESUME.work[0],
      highlights: [
        "Moved hot reads to a Redis cache, cutting p95 latency from 840 ms to 190 ms.",
        "Rebuilt the ingestion service in Go; it now handles 2.3M events a day.",
        "Guided two interns through their first production launches.",
      ],
    },
    {
      ...BASE_RESUME.work[1],
      highlights: ["Designed the PostgreSQL schema behind the loyalty programme.", "Wrote the Python ETL that feeds the finance dashboards."],
    },
  ],
  projects: [{ ...BASE_RESUME.projects[0], description: "A command-line tool for tide forecasts." }],
});
const TAILORED_CHANGES = [
  { section: "work", item_id: IDS.work1, before: BASE_RESUME.work[0].highlights[0], after: TAILORED.work[0].highlights[0], reason: "Lead with the outcome.", evidence_fact_ids: [IDS.work1] },
  { section: "work", item_id: IDS.work1, before: BASE_RESUME.work[0].highlights[2], after: TAILORED.work[0].highlights[2], reason: "Plainer verb.", evidence_fact_ids: [IDS.work1] },
  { section: "summary", item_id: null, before: BASE_RESUME.summary, after: TAILORED.summary, reason: "Name the stack.", evidence_fact_ids: [] },
];
const TAILORED_EDU = ResumeSchema.parse({
  ...BASE_RESUME,
  education: [{ ...BASE_RESUME.education[0], highlights: ["On the Dean’s list from 2019 to 2021"] }],
});
await prompts("verify_judge", [
  { name: "work_summary_project_with_changes", input: { base: BASE_RESUME, tailored: TAILORED, changes: TAILORED_CHANGES },
    run: () => verifyTailored(BASE_RESUME, TAILORED, TAILORED_CHANGES as any) },
  { name: "education_without_changes", input: { base: BASE_RESUME, tailored: TAILORED_EDU, changes: [] },
    run: () => verifyTailored(BASE_RESUME, TAILORED_EDU, []) },
]);

// The careers-page listing extractor (purpose extract)
await prompts("careers_listing", CAREERS_PAGES.map((page) => ({
  name: page.slug,
  input: { company: { name: page.name, slug: page.slug, careersUrls: [page.url] }, fixture: page.file },
  run: () => {
    route(page.url, page.file, 200, "text/html; charset=utf-8");
    return fetchCareersPage({ name: page.name, slug: page.slug, careersUrls: [page.url] });
  },
})));

// discover_companies' name proposal (purpose chat)
await prompts("discover_companies", [
  { name: "five", input: { description: "AI infrastructure startups in SF hiring new grads", count: 5 } },
  { name: "one_unicode", input: { description: "Fintechs in Zürich — “Series B” or later", count: 1 } },
].map((item) => ({
  ...item,
  run: () => (discoverCompanies("capture-user") as any).execute(item.input, { toolCallId: "capture", messages: [] }),
})));

// The chat system prompt: not an LLM call, captured directly. The date line uses the
// process time zone (TIME_ZONE) at the frozen instant NOW_ISO.
const SYSTEM_CASES = [
  { name: "no_resume", input: { hasProfile: false, preferences: interview.EMPTY_PREFERENCES, interviewState: interview.EMPTY_INTERVIEW_STATE, settings: null, userName: null } },
  { name: "no_resume_named_autopilot", input: { hasProfile: false, preferences: PREFS_PARTIAL, interviewState: interview.EMPTY_INTERVIEW_STATE, settings: { defaultMode: "autopilot", scoreThreshold: 60, deepEvalTopN: 20 }, userName: "Zoë Ñúñez" } },
  { name: "interview_empty", input: { hasProfile: true, preferences: interview.EMPTY_PREFERENCES, interviewState: interview.EMPTY_INTERVIEW_STATE, settings: DEFAULT_SETTINGS, userName: "Dana Reyes" } },
  { name: "interview_partial_skipped", input: { hasProfile: true, preferences: PREFS_PARTIAL, interviewState: { version: 1, skipped: ["salary_floor", "seniority"] }, settings: null, userName: null } },
  { name: "complete_optional_outstanding", input: { hasProfile: true, preferences: PREFS_REQUIRED_ONLY, interviewState: interview.EMPTY_INTERVIEW_STATE, settings: DEFAULT_SETTINGS, userName: "Dana Reyes" } },
  { name: "complete_optional_skipped", input: { hasProfile: true, preferences: PREFS_REQUIRED_ONLY, interviewState: { version: 1, skipped: ["salary_floor", "start_date", "constraints"] }, settings: null, userName: null } },
  { name: "complete_all_autopilot", input: { hasProfile: true, preferences: PREFS_FULL, interviewState: interview.EMPTY_INTERVIEW_STATE, settings: { defaultMode: "autopilot", scoreThreshold: 55, deepEvalTopN: 0 }, userName: "Dana Reyes" } },
  { name: "complete_fractional_floor", input: { hasProfile: true, preferences: PREFS_FRACTIONAL, interviewState: { version: 1, skipped: ["constraints"] }, settings: DEFAULT_SETTINGS, userName: null } },
];
for (const item of SYSTEM_CASES) {
  writeJson(`prompts/chat_system_prompt/${item.name}.json`, {
    builder: "chat_system_prompt",
    case: item.name,
    input: { ...item.input, now: NOW_ISO, timeZone: TIME_ZONE },
    instructions: systemPrompt(item.input as any),
  });
}

// The salary strings the chat tools return: list_jobs (jobCard over float8 values) and
// get_job (the raw numeric strings Postgres returns), the latter through the real tool.
const CARD_SALARIES = [
  { salaryMin: 150000, salaryMax: 200000, salaryCurrency: "USD", salaryPeriod: "year" },
  { salaryMin: 150000, salaryMax: null, salaryCurrency: "USD", salaryPeriod: "year" },
  { salaryMin: null, salaryMax: 200000, salaryCurrency: null, salaryPeriod: null },
  { salaryMin: 150000, salaryMax: 150000, salaryCurrency: "USD", salaryPeriod: "year" },
  { salaryMin: 55, salaryMax: 70, salaryCurrency: "USD", salaryPeriod: "hour" },
  { salaryMin: 124.128, salaryMax: null, salaryCurrency: null, salaryPeriod: null },
  { salaryMin: 999.5, salaryMax: 1500.4, salaryCurrency: "GBP", salaryPeriod: "month" },
  { salaryMin: 90000, salaryMax: 120000, salaryCurrency: "EUR", salaryPeriod: "year" },
  { salaryMin: 0, salaryMax: 0, salaryCurrency: "USD", salaryPeriod: "year" },
  { salaryMin: null, salaryMax: null, salaryCurrency: null, salaryPeriod: null },
];
const matchRow = (salary: (typeof CARD_SALARIES)[number]) => ({
  matchId: "m-1", jobId: "greenhouse:acme:1", title: "Backend Engineer", company: "Acme", locations: ["San Francisco, CA"],
  remote: "hybrid", stage: "found", score: 82, fastScore: 70, deepScore: 82, recommendation: "apply",
  headline: "Strong backend overlap", postedAt: new RealDate("2026-10-01T18:00:00.000Z"), url: "https://example.com/jobs/1",
  ...salary,
});
writeJson("prompts/tool_salary/list_jobs.json", {
  builder: "tool_salary",
  case: "list_jobs",
  cases: CARD_SALARIES.map((salary) => ({ input: matchRow(salary), output: jobCard(matchRow(salary) as any) })),
});
const JOB_SALARIES = [
  { salary_min: "150000", salary_max: "200000", salary_currency: "USD", salary_period: "year" },
  { salary_min: "124.128", salary_max: "130.5", salary_currency: "USD", salary_period: "hour" },
  { salary_min: "150000", salary_max: null, salary_currency: "USD", salary_period: "year" },
  { salary_min: null, salary_max: "7.982", salary_currency: null, salary_period: null },
  { salary_min: "150000", salary_max: "200000", salary_currency: null, salary_period: null },
  { salary_min: null, salary_max: null, salary_currency: "USD", salary_period: "year" },
];
const getJobTool = getJob("capture-user") as any;
const getJobCases = [];
for (const salary of JOB_SALARIES) {
  tables = {
    job_matches: [],
    jobs: [{ id: "greenhouse:acme:1", title: "Backend Engineer", company_name: "Acme", locations: ["San Francisco, CA"],
      remote: "hybrid", posted_at: "2026-10-01T18:00:00.000Z", is_active: true, url: "https://example.com/jobs/1",
      apply_url: null, description_text: "Short.", ...salary }],
  };
  const output = await getJobTool.execute({ id: "greenhouse:acme:1" }, { toolCallId: "capture", messages: [] });
  tables = {};
  getJobCases.push({ input: salary, output: { salary: output.salary } });
}
writeJson("prompts/tool_salary/get_job.json", {
  builder: "tool_salary",
  case: "get_job",
  note: "input values are what Postgres returns for the numeric salary columns (strings)",
  cases: getJobCases,
});

// =====================================================================================
// UI message stream reference turns (the TS route's createAgentUIStreamResponse)
// =====================================================================================

const usage = {
  inputTokens: { total: 10, noCache: 10, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 5, text: 5, reasoning: 0 },
};
const finish = (unified: string, raw: string) => ({ type: "finish", finishReason: { unified, raw }, usage });
const textChunks = (id: string, ...deltas: string[]) => [
  { type: "text-start", id },
  ...deltas.map((delta) => ({ type: "text-delta", id, delta })),
  { type: "text-end", id },
];
const toolCall = (toolCallId: string, toolName: string, input: string, streamed = false) => [
  ...(streamed
    ? [
        { type: "tool-input-start", id: toolCallId, toolName },
        { type: "tool-input-delta", id: toolCallId, delta: input },
        { type: "tool-input-end", id: toolCallId },
      ]
    : []),
  { type: "tool-call", toolCallId, toolName, input },
];
const step = (...chunks: any[]) => [{ type: "stream-start", warnings: [] }, ...chunks];

const tools = {
  list_jobs: ai.tool({
    inputSchema: z.object({ limit: z.number() }),
    execute: async ({ limit }: { limit: number }) => ({
      count: Math.min(limit, 1),
      jobs: [{ matchId: "m-1", jobId: "greenhouse:acme:1", title: "Backend Engineer", company: "Café Numérique", salary: "$150k–200k" }].slice(0, limit),
    }),
  }),
  get_job: ai.tool({
    inputSchema: z.object({ id: z.string() }),
    execute: async ({ id }: { id: string }) => {
      if (id === "raise") throw new Error("That step failed on our side.");
      if (id === "undefined") return undefined;
      if (id === "null") return null;
      return { ok: true, jobId: id, title: "Backend Engineer" };
    },
  }),
  tailor_resume: ai.tool({
    inputSchema: z.object({ jobId: z.string() }),
    execute: async ({ jobId }: { jobId: string }) => ({ runId: `run_${jobId}` }),
  }),
};

type Scenario = {
  name: string;
  steps: any[][];
  turns?: Array<(previous: any) => any[]>;
  approval?: boolean;
  maxSteps?: number;
};
const USER = { id: "u1", role: "user", parts: [{ type: "text", text: "Find me something good — café hours welcome." }] };
const SCENARIOS: Scenario[] = [
  { name: "text", steps: [step(...textChunks("t1", "Here is ", "the answer: Zoë’s café ✓ \"quoted\"\n", "</script> & done."), finish("stop", "stop"))] },
  { name: "reasoning_text", steps: [step(
    { type: "reasoning-start", id: "r1" }, { type: "reasoning-delta", id: "r1", delta: "Thinking..." }, { type: "reasoning-end", id: "r1" },
    ...textChunks("t1", "Done thinking."), finish("stop", "stop"))] },
  { name: "tool_two_steps", steps: [
    step(...textChunks("t1", "Let me ", "look."), ...toolCall("call_1", "list_jobs", '{"limit":2}', true), finish("tool-calls", "tool_use")),
    step(...textChunks("t2", "One match: Backend Engineer at Café Numérique."), finish("stop", "end_turn")),
  ] },
  { name: "tool_parallel", steps: [
    step(...toolCall("call_1", "get_job", '{"id":"greenhouse:acme:1"}'), ...toolCall("call_2", "list_jobs", '{"limit":1}'), finish("tool-calls", "tool_use")),
    step(...textChunks("t2", "Both are in."), finish("stop", "end_turn")),
  ] },
  { name: "tool_returns_undefined", steps: [
    step(...toolCall("call_1", "get_job", '{"id":"undefined"}'), finish("tool-calls", "tool_use")),
    step(...textChunks("t2", "It returned nothing."), finish("stop", "end_turn")),
  ] },
  { name: "tool_returns_null", steps: [
    step(...toolCall("call_1", "get_job", '{"id":"null"}'), finish("tool-calls", "tool_use")),
    step(...textChunks("t2", "It returned null."), finish("stop", "end_turn")),
  ] },
  { name: "tool_raises", steps: [
    step(...toolCall("call_1", "get_job", '{"id":"raise"}'), finish("tool-calls", "tool_use")),
    step(...textChunks("t2", "That did not work."), finish("stop", "end_turn")),
  ] },
  { name: "invalid_tool_input", steps: [
    step(...toolCall("call_1", "list_jobs", '{"limit":"two"}', true), finish("tool-calls", "tool_use")),
    step(...textChunks("t2", "Retrying was not needed."), finish("stop", "end_turn")),
  ] },
  { name: "unknown_tool", steps: [
    step(...toolCall("call_1", "no_such_tool", "{}"), finish("tool-calls", "tool_use")),
    step(...textChunks("t2", "Never mind."), finish("stop", "end_turn")),
  ] },
  { name: "step_cap", maxSteps: 8, steps: Array.from({ length: 8 }, (_, n) =>
    step(...toolCall(`call_${n + 1}`, "list_jobs", '{"limit":1}'), finish("tool-calls", "tool_use"))) },
  { name: "stream_error", steps: [step(...textChunks("t1", "Partial "), { type: "error", error: new Error("Provider overloaded") })] },
  { name: "length_finish", steps: [step(...textChunks("t1", "Cut off mid"), finish("length", "max_tokens"))] },
];

async function runTurn(model: any, uiMessages: any[], replyId: string, approval: boolean, maxSteps: number) {
  const agent = new ai.ToolLoopAgent({
    model,
    instructions: "test",
    tools,
    ...(approval ? { toolApproval: { tailor_resume: "user-approval" } } : {}),
    stopWhen: ai.isStepCount(maxSteps),
  });
  let persisted: any = null;
  const response: Response = await ai.createAgentUIStreamResponse({
    agent,
    uiMessages,
    originalMessages: uiMessages,
    headers: { "x-conversation-id": "conv_1" },
    generateMessageId: () => replyId,
    onEnd: ({ responseMessage }: any) => {
      persisted = responseMessage ?? null;
    },
  });
  const headers = Object.fromEntries([...response.headers.entries()].sort(([a], [b]) => a.localeCompare(b)));
  const body = await response.text();
  return { status: response.status, headers, body, persisted };
}

function scriptedModel(steps: any[][]) {
  let index = 0;
  return new aiTest.MockLanguageModelV4({
    doStream: async () => {
      const chunks = steps[index++];
      if (!chunks) throw new Error("capture-goldens: the scripted model ran out of steps");
      return { stream: aiTest.convertArrayToReadableStream(chunks) };
    },
  });
}

const uiIndex = [];
for (const scenario of SCENARIOS) {
  reseed(0x1d5eed);
  const turn = await runTurn(scriptedModel(scenario.steps), [USER], `asst_${scenario.name}`, false, scenario.maxSteps ?? 8);
  write(`uistream/${scenario.name}.sse`, turn.body);
  writeJson(`uistream/${scenario.name}.persisted.json`, turn.persisted);
  uiIndex.push({ name: scenario.name, uiMessages: [USER], replyId: `asst_${scenario.name}`, stopWhen: `isStepCount(${scenario.maxSteps ?? 8})`,
    status: turn.status, headers: turn.headers, modelSteps: scenario.steps, sse: `${scenario.name}.sse`, persisted: `${scenario.name}.persisted.json` });
}

// The research reference (docs/migration/prototypes/ts-turn{1,2}): one auto-run tool and
// one approval-gated tool, then the turn after the browser approves. Foray does not use
// SDK tool approvals (§7.6); kept because the prototype accumulator was proven on it.
{
  reseed(0x1d5eed);
  const steps = [
    step({ type: "reasoning-start", id: "r1" }, { type: "reasoning-delta", id: "r1", delta: "Thinking..." }, { type: "reasoning-end", id: "r1" },
      ...textChunks("t1", "Let me ", "look."), ...toolCall("call_1", "list_jobs", '{"limit":2}', true),
      ...toolCall("call_2", "tailor_resume", '{"jobId":"j1"}'), finish("tool-calls", "tool_use")),
    step(...textChunks("t2", "Started your tailored resume."), finish("stop", "end_turn")),
  ];
  const model = scriptedModel(steps);
  const user = { id: "u1", role: "user", parts: [{ type: "text", text: "tailor my resume for j1" }] };
  const turn1 = await runTurn(model, [user], "asst_turn1", true, 8);
  const approved = {
    ...turn1.persisted,
    parts: turn1.persisted.parts.map((part: any) =>
      part.state === "approval-requested"
        ? { ...part, state: "approval-responded", approval: { ...part.approval, approved: true } }
        : part,
    ),
  };
  const turn2 = await runTurn(model, [user, approved], "asst_turn2", true, 8);
  write("uistream/approval_turn1.sse", turn1.body);
  writeJson("uistream/approval_turn1.persisted.json", turn1.persisted);
  write("uistream/approval_turn2.sse", turn2.body);
  writeJson("uistream/approval_turn2.persisted.json", turn2.persisted);
  uiIndex.push({ name: "approval_turn1", uiMessages: [user], replyId: "asst_turn1", toolApproval: { tailor_resume: "user-approval" },
    status: turn1.status, headers: turn1.headers, modelSteps: steps.slice(0, 1), sse: "approval_turn1.sse", persisted: "approval_turn1.persisted.json" });
  uiIndex.push({ name: "approval_turn2", uiMessages: [user, approved], replyId: "asst_turn2", toolApproval: { tailor_resume: "user-approval" },
    status: turn2.status, headers: turn2.headers, modelSteps: steps.slice(1), sse: "approval_turn2.sse", persisted: "approval_turn2.persisted.json" });
}
writeJson("uistream/scenarios.json", {
  note: "Each scenario ran through ToolLoopAgent + createAgentUIStreamResponse as src/app/api/chat/route.ts calls it, with a scripted MockLanguageModelV4. Tools: list_jobs, get_job (id 'raise' throws, 'undefined'/'null' return those), tailor_resume.",
  ai: readPackageVersion("ai"),
  scenarios: uiIndex,
});

// =====================================================================================
// Better Auth hashes (minted by the installed @better-auth/utils)
// =====================================================================================

/** The installed version of a direct dependency. */
function readPackageVersion(name: string): string {
  return JSON.parse(readFileSync(join(ROOT, "node_modules", name, "package.json"), "utf8")).version;
}

{
  const { realpathSync } = await import("node:fs");
  const betterAuthDir = realpathSync(join(ROOT, "node_modules/better-auth"));
  const fromBetterAuth = createRequire(join(betterAuthDir, "package.json"));
  const utilsEntry = fromBetterAuth.resolve("@better-auth/utils/password");
  const utils: any = await import(pathToFileURL(utilsEntry.replace(/\.cjs$/, ".mjs")).href);
  let utilsDir = dirname(utilsEntry);
  while (!existsSync(join(utilsDir, "package.json"))) utilsDir = dirname(utilsDir);
  const utilsVersion = JSON.parse(readFileSync(join(utilsDir, "package.json"), "utf8")).version;

  const PASSWORDS = [
    { label: "ascii", password: "correct horse battery staple 42!" },
    // NFKC changes this one (fullwidth letters, the ﬁ ligature, a superscript two), and it
    // has accents, an emoji and a non-Latin script, so it exercises the normalisation.
    { label: "non_ascii", password: "Ｐａｓｓ ﬁxé² Zoë — пароль 🔐" },
  ];
  const hashes = [];
  for (const { label, password } of PASSWORDS) {
    const kept = previousHashes.find((item) => item.label === label && item.password === password);
    const hash = kept && (await utils.verifyPassword(kept.hash, password)) ? kept.hash : await utils.hashPassword(password);
    if (!(await utils.verifyPassword(hash, password))) throw new Error(`capture-goldens: ${label} hash does not verify`);
    if (await utils.verifyPassword(hash, `${password}x`)) throw new Error(`capture-goldens: ${label} hash verifies a wrong password`);
    hashes.push({ label, password, nfkc: password.normalize("NFKC"), hash });
  }
  writeJson(HASHES, hashes);
  writeJson("auth/source.json", {
    package: "@better-auth/utils",
    version: utilsVersion,
    entry: relative(ROOT, utilsEntry),
    betterAuth: readPackageVersion("better-auth"),
    format: "<32 lowercase hex salt>:<128 lowercase hex key>; scrypt(NFKC(password), salt as the hex string's UTF-8 bytes, N=16384, r=16, p=1, dkLen=64)",
  });
}

// =====================================================================================
// Index
// =====================================================================================

write(
  "README.md",
  `# Goldens

Written by \`scripts/capture-goldens.mts\` (migration step 0) from the TypeScript code, before
it is deleted. Do not edit by hand; re-run the script instead:

    pnpm tsx scripts/capture-goldens.mts

Conventions:
- Dates are ISO 8601 UTC strings. The capture clock is frozen at ${NOW_ISO}, and the process
  time zone is ${TIME_ZONE} (the chat system prompt's date line depends on both).
- Salary inputs written as strings are what Postgres returns for the legacy \`numeric\` columns;
  numbers are what \`::float8\` reads return.
- \`adapters/**\` hold every \`NormalizedJob\` field except \`raw\` (the fixture payload itself);
  \`descriptionRaw\` is pinned as \`{sha256, utf8Bytes}\` of the exact string, rebuilt from the fixture.
- \`prompts/<builder>/<case>.json\` hold \`{instructions, prompt}\` exactly as the model received
  them (the system message and the user text). Vision prompts also list their parts; page
  images are recorded by media type only.
- \`uistream/\` holds the raw SSE and the persisted (\`onEnd\`) message per scenario;
  \`scenarios.json\` lists the inputs and the scripted model chunks.
- \`auth/better_auth_hashes.json\` are real Better Auth hashes of test passwords (not secrets).
- Library versions: ai ${readPackageVersion("ai")}, better-auth ${readPackageVersion("better-auth")},
  unpdf ${readPackageVersion("unpdf")}, mammoth ${readPackageVersion("mammoth")}.

Files:
${[...written, "README.md"].sort().map((path) => `- \`${path}\``).join("\n")}
`,
);

// Leave nothing behind that the script did not write this run.
const stray = listFiles(OUT).filter((path) => !written.includes(path));
if (stray.length) throw new Error(`capture-goldens: unexpected files: ${stray.join(", ")}`);

function listFiles(dir: string, prefix = ""): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? listFiles(join(dir, entry.name), `${prefix}${entry.name}/`)
      : [`${prefix}${entry.name}`],
  );
}

rmSync(GOLDENS, { recursive: true, force: true });
renameSync(OUT, GOLDENS);
console.log(`capture-goldens: wrote ${written.length} files under ${relative(ROOT, GOLDENS)}`);
await pool.end().catch(() => undefined);
