# Area map: next-frontend

Scope: every page/layout under `src/app/(app)/**`, `src/app/(auth)/**`, `src/app/layout.tsx`,
`src/app/page.tsx`, the edge `src/proxy.ts`, and every file under `src/components/**`, plus
`src/components/live/*` (the polling contract) and `src/lib/theme.ts`.
All paths are relative to the repo root
`/Users/haydenbarolette/Projects/JobSearchAgent/.claude/worktrees/middleware-django-migration-389b1a`.

Goal of this map: say exactly which data each page needs from a future Django JSON API, which
mutations each client component performs (server actions today → HTTP endpoints tomorrow), and
which `src/lib` modules the frontend keeps (pure helpers, types) versus loses (server-only).

---------------------------------------------------------------------------------------------

## 0. Summary of the rendering model today

* App Router, Next 16, React 19. Every page under `(app)` is an **async React Server Component**
  that calls `requireUser()` (`src/lib/auth/session.ts:21-25`) and then reads Postgres
  **directly** — through `src/lib/db/queries/*`, `src/lib/profile/onboarding.ts`, raw
  drizzle (`db.select()…`) in the page file itself, or `src/lib/llm/*` / `src/env.ts` for
  configuration. There is no JSON layer between pages and the database.
* Client components mutate through **Server Actions** in `src/app/actions/*.ts` (imported
  directly into client components) and through a handful of **Route Handlers** under
  `src/app/api/**` (fetched with `fetch()`).
* Everything "live" goes through one client poll: `LiveProvider`
  (`src/components/live/live-provider.tsx`) fetching `GET /api/live?since=<cursor>`.
  Server pages re-render on events with `<RefreshOn kinds=[…]/>` → `router.refresh()`.
* Chat uses the AI SDK v7 `useChat` hook with `DefaultChatTransport` → `POST /api/chat`
  (AI SDK UI-message SSE stream).
* Auth UI uses the Better Auth React client (`src/lib/auth/client.ts`) → `/api/auth/*`.
* Theme is 100% client-side (localStorage), no server state.

Dynamic rendering: every `(app)` page and `sign-in` read `headers()` via `getSession()`, so
they are all dynamic (no static generation, no `export const dynamic`).

---------------------------------------------------------------------------------------------

## 1. Edge / root

### 1.1 `src/proxy.ts` (Next 16 "proxy" = middleware) — runs on every matched request
* `proxy(request)` (`src/proxy.ts:12-18`): if `getSessionCookie(request)` (from
  `better-auth/cookies`) finds a session cookie → `NextResponse.next()`; else 307-redirect to
  `/sign-in?next=<pathname>`.
* Matcher (`src/proxy.ts:20-33`): `/home/:path*`, `/matches/:path*`, `/chat/:path*`,
  `/onboarding/:path*`, `/pipeline/:path*`, `/jobs/:path*`, `/activity/:path*`,
  `/profile/:path*`, `/companies/:path*`, `/settings/:path*`. (`/pipeline`, `/jobs` have no
  pages any more; they 404 after the cookie check.)
* Invariant (comment `src/proxy.ts:4-11`): this is an *optimistic* redirect on cookie
  presence only; it never validates the session. Real validation is `requireUser()`.
* Cookie name: Better Auth default `better-auth.session_token` (or
  `__Secure-better-auth.session_token` under https). CLAUDE.md's dev-session script mints
  exactly that cookie. **If Django owns auth, this check must read Django's session cookie
  name instead** (or the Django side must keep issuing a cookie with this name).

### 1.2 `src/app/layout.tsx` — Server Component, root layout
* Imports: `ThemeSync` (client), `THEME_COLORS`, `THEME_SCRIPT` from `src/lib/theme.ts`,
  `globals.css`, three `next/font/google` faces: Bricolage Grotesque (`--font-bricolage`, axes
  opsz,wdth), Instrument Sans (`--font-instrument`, axis wdth), Geist Mono
  (`--font-geist-mono`) (`layout.tsx:14-29`).
* `metadata`: title default "Foray", template "%s – Foray", description (`layout.tsx:31-35`).
* `viewport`: `themeColor: THEME_COLORS.dark` (`#0d1310`), `colorScheme: "dark light"`
  (`layout.tsx:38-41`).
* Renders `<html class="… dark h-full antialiased" lang="en" suppressHydrationWarning>`; inline
  `<script>` = `THEME_SCRIPT` in `<head>` (runs before paint); `<body class="min-h-full">`
  containing `<ThemeSync/>` and children (`layout.tsx:43-61`).
* No server data. No auth.

### 1.3 `src/app/page.tsx` — Server Component, `/`
* `redirect((await getSession()) ? "/home" : "/sign-in")` (`page.tsx:6-8`).
* Needs from backend: "is there a valid session?" (boolean).

### 1.4 `src/app/(auth)/sign-in/page.tsx` — Server Component, `/sign-in`
* `metadata.title = "Sign in"`.
* `safeNext(next)` (`sign-in/page.tsx:12-14`): honours `?next=` only if it starts with `/` and
  not `//`; else `/home`. **Open-redirect guard — must survive.**
* Reads `searchParams.next`; if `getSession()` returns a session → `redirect(next)`
  (`:21-22`).
* `googleEnabled = Boolean(env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET)` (`:24`) — reads
  server env. Future API must expose this flag (e.g. `GET /api/auth/providers` →
  `{google: bool}`).
* Renders `<Wordmark/>`, `<SignInForm googleEnabled next/>`, a static marketing `Vignette`
  with hard-coded illustrative rows rendered via `<ScoreChip/>` (`:49-87`). No data.

---------------------------------------------------------------------------------------------

## 2. Signed-in shell `(app)`

### 2.1 `src/app/(app)/layout.tsx` — Server Component
* `const user = await requireUser()` (`layout.tsx:12`) → redirect to `/sign-in` when no
  session.
* Renders `<LiveProvider><AppShell user={{name, email}}>{children}</AppShell><Toaster/></LiveProvider>`.
* Comment invariant (`:6-10`): pages still call `requireUser()` themselves; a layout does not
  re-run on every navigation, so it cannot be the only guard.
* Needs from backend: current user `{name, email}` (and auth check).

### 2.2 `src/app/(app)/loading.tsx` — Server Component, skeleton only. No data.
### 2.3 `src/app/(app)/error.tsx` — Client Component (`"use client"`), shows `error.digest`,
  `reset()` button, link to `/home`. No data.
### 2.4 `src/app/(app)/not-found.tsx` — Server Component, static. Rendered by `notFound()` in
  chat/[id] and matches/[id] pages ("That match, chat or page does not exist, or it belongs
  to another account.").
### 2.5 `src/app/(app)/onboarding/page.tsx` — Server Component, `redirect("/home")`. No data.

---------------------------------------------------------------------------------------------

## 3. Pages (data needs, exactly)

Notation: **Reads** = what the page loads server-side today (function → source); **Needs** =
the JSON a Django endpoint must return to replace it; **Renders** = what it shows.

### 3.1 `/home` — `src/app/(app)/home/page.tsx` (Server Component)
* `metadata.title = "Home"`.
* Reads (in parallel, `home/page.tsx:32-38`):
  1. `getOnboardingState(user.id)` — `src/lib/profile/onboarding.ts:31-54`.
  2. `countByStage(user.id)` — `src/lib/db/queries/matches.ts:253-268` →
     `Record<Stage, number>` (all 7 stages, zero-filled).
  3. `listMatches(user.id, { stages: ["found","shortlisted"], limit: 6 })` —
     `matches.ts:104-142` → `MatchRow[]` ordered `coalesce(deep_score, fast_score) desc nulls
     last, posted_at desc nulls last`.
  4. `spendSince(user.id, monthStart())` — `src/lib/db/queries/spend.ts:14-25`; `monthStart`
     = first of current month UTC (`spend.ts:10-12`).
  5. `liveCounts(user.id)` — **raw drizzle in the page** (`home/page.tsx:138-156`):
     * `select count(*)::int from approvals where user_id=$1 and status='pending' and
       (expires_at is null or expires_at > now())`
     * `select count(*)::int from runs where user_id=$1 and status in ('queued','running')`
     * returns `{approvals, running}`.
* Derived on the page (pure, can move to the client or the API):
  * `firstName = (user.name||"").split(" ")[0]` (`:40`).
  * `describeState()` (`:159-213`) — headline/detail priority: no resume → "Let's set up your
    search."; not ready → "A few answers…" + list of `progress.blocking[].label` lowercased;
    `waiting.approvals>0` → "{hi}one decision is waiting on you." / "{n} decisions…";
    `waiting.running>0` → "Foray is searching right now."; else `strong = best.filter(score>=80)`,
    `queue = counts.found + counts.shortlisted`; queue>0 → strong>0 ? "{n} strong match(es)
    waiting for a look." : "{n} role(s) in your queue." with detail "{shortlisted} shortlisted,
    {applied} applied so far." when shortlisted>0; else "Ready when you are."
  * Suggestions (`:44-57`): if `onboarding.ready`: `[target_roles[0] ? "Find new
    {role.toLowerCase()} roles" : "Find new roles for me", "Which of my matches fit me best,
    and why?", untailored ? "Tailor my resume for the {company} role" : "What should I
    improve on my resume?"]` where `untailored = best.find(!tailoredResumeId)`; else if
    `hasResume`: two setup prompts; else `[]`.
* Renders: `<RefreshOn kinds={["run.completed","approval.decided","approval.requested","tailor.done"]}/>`
  (`:61`); headline; `<AskBox suggestions/>` when `hasResume`; if not ready → numbered
  `Onboarding` steps (`:253-307`: step 1 `<ResumeDropzone hasResume={false}/>` or counts of
  `resume.work/education/skills`; step 2 progress `answered/total`, `<OnboardingChat/>`,
  link `/profile#preferences`; step 3 `<StartSearchButton label="Start searching"/>`);
  else `<AttentionPanel/>` (client, live), "Best matches" `<MatchLine match={fromMatchRow(row)}
  showHeadline/>` or EmptyState with `<StartSearchButton label="Run a search"/>`, "Your board"
  `PipelineSummary` (bars per OPEN_STAGES linking `/matches?stage=<stage>`, plus
  "{archived+rejected} archived or rejected"), `<RecentActivity/>` (client, live), "This
  month" model spend `formatUsd(spend.usd)` and `spend.calls.toLocaleString()`, link
  `/settings#usage`.
* **Needs** (`GET /api/home` or composed):
  ```
  user: {name}
  onboarding: {hasProfile, hasResume, ready,
               preferences: {target_roles: string[]|null, …},
               progress: {answered, total, blocking: [{id,label}]},
               resumeCounts: {work, education, skills}}       // only counts are rendered
  counts: {found, shortlisted, applied, interviewing, offer, rejected, archived}
  best: MatchRow[]  (≤6, stages found+shortlisted)
  spend: {usd, calls}
  waiting: {approvals, running}
  ```

### 3.2 `/matches` — `src/app/(app)/matches/page.tsx` (Server Component)
* `searchParams: {view?, stage?, closed?}` (`:20`).
* `stageParam` = `params.stage` if in `ALL_STAGES` else null; `view = (params.view === "list"
  || stageParam) ? "list" : "board"`; `showClosed = params.closed === "1"` (`:30-33`).
* Reads (`:35-42`): `countByStage`, `listMatches(user.id, {stages: view==="board" &&
  !showClosed ? OPEN_STAGES : ALL_STAGES, limit: 1000})`, `getOnboardingState` (only `.ready`
  used).
* Derived: `open = sum(counts[OPEN_STAGES])`, `closed = counts.archived + counts.rejected`.
* Renders: `<RefreshOn kinds={["run.completed","approval.decided"]}/>` (`:49`); view toggle
  links `/matches` and `/matches?view=list`; `<StartSearchButton label="Search again"
  variant="outline"/>` when ready; description; empty states (not ready → link `/home`;
  no matches → StartSearchButton); board → `<Board matches showClosed/>` + toggle link
  `/matches?closed=1`; list → `<MatchTable initialStage={stageParam} matches/>`.
* **Needs**: `GET /api/matches?stages=…&limit=1000` → `MatchRow[]`; `counts`; `ready`.

### 3.3 `/matches/[id]` — `src/app/(app)/matches/[id]/page.tsx` (Server Component)
* `generateMetadata` (`:36-40`) calls `requireUser` + `getMatchDetail` again → title
  "{job.title} at {job.companyName}" or "Match". (Duplicate fetch.)
* Reads: `getMatchDetail(user.id, id)` (`matches.ts:164-178`; returns null for non-UUID or
  other user's) → `notFound()` (`:46-47`). Then in parallel (`:50-60`):
  * raw drizzle: `select * from resumes where id = match.tailored_resume_id and user_id = $1
    limit 1` (only if `match.tailoredResumeId`);
  * `getBaseResume(user.id)` (`src/lib/db/queries/profile.ts:20-28`; latest `kind='base'` by
    version desc) — only `Boolean(base)` is used.
* Shapes `TailoredView` (`:62-70`): `{id, documentId: renderedDocumentId, createdAt ISO,
  changes ?? [], verification ?? null}`. `deep = match.deepEval as DeepEvalView|null`,
  `fast = match.fastEval as FastEvalView|null`.
* Fields used from `job`: `title, companyName, locations, remote, salaryMin, salaryMax
  (numeric → string), salaryCurrency, salaryPeriod, postedAt, isActive, url, applyUrl,
  descriptionText, source`. From `match`: `id, stage, deepEval, fastEval, tailoredResumeId,
  notes, createdAt, stageChangedAt, fastScore, deepScore`.
* Renders: `<RefreshOn kinds={["run.completed","tailor.done","render.done"]}/>` (`:79`); back
  link; `<CompanyMark size="lg"/>`; title/company; location via
  `formatLocation(job.locations, null)` + `remoteLabel(job.remote)`; `formatSalary(job)`;
  `postedLabel(job.postedAt)`; "This posting has closed" when `!isActive`;
  `<StageSelect matchId stage/>`; "View posting" (`job.url`), "Apply" (`job.applyUrl` when
  ≠ url); "Why it fits": `<DeepEvaluation/>` + `<EvaluateButton label="Review again"/>`, or
  `<FastEvaluation/>` + `<EvaluateButton/>`, or "not been scored" + `<EvaluateButton/>`;
  `<Posting text={job.descriptionText}/>`; aside `<TailorPanel hasResume matchId tailored/>`
  (anchor `#resume`), `<MatchNotes initial={match.notes}/>`, History rows: Found
  `timeAgo(match.createdAt)`, `STAGE_LABEL[stage]` "since {timeAgo(stageChangedAt)}",
  Triage score, Deep review, Source (`jsearch` → "Job aggregator", `web` → "Careers page",
  else "{Capitalized} board") (`:177`).
* **Needs** `GET /api/matches/<id>` → `{match:{…above}, job:{…above}, tailored:
  TailoredView|null, hasBaseResume: bool}`; 404 → `notFound()`.

### 3.4 `/activity` — `src/app/(app)/activity/page.tsx` (Server Component)
* Reads `listRuns(user.id, 30)` (`src/lib/runs/runs.ts:56-63`: `select * from runs where
  user_id=$1 order by created_at desc limit 30`), then `describeRuns()` (`:59-118`):
  * `UUID` regex `/^[0-9a-f]{8}-…-[0-9a-f]{12}$/i` (`:18`) guards every id before SQL.
  * `resumeIds` = each `run.input.resumeId` that is a UUID string; batched read
    `select id, kind, job_match_id from resumes where user_id=$1 and id = any($2)`.
  * Cost: `select run_id, coalesce(sum(cost_usd),0)::float8 usd from llm_calls where
    user_id=$1 and run_id = any($runIds) group by run_id`.
  * `matchIds` = `input.matchId` (string) ∪ `input.matchIds[0]` (string) ∪ every resume's
    `jobMatchId`; then `getMatchRows(user.id, uuidOnly)` → `roleById = "{title} at {company}"`.
  * Each summary: `{run: toLiveRun(run), target, costUsd: costById ?? 0, conversationId:
    run.conversationId}` (`:109-117`).
  * `target(kind, input, roleById, resumeById)` (`:120-157`):
    * `search` → `describeBrief(parseBrief(input.brief ?? input.hints)) ?? (input.prompt if
      string) ?? null`
    * `rank_deep` → ids=`input.matchIds`; 0 → null; 1 → role or "1 role"; n → "{first} and
      {n-1} more" or "{n} roles"
    * `tailor` → `roleById[input.matchId]` or null
    * `resume_render` → resume by `input.resumeId`; none → null; `kind==base` → "Your base
      resume"; else "Tailored for {role}" or "A tailored resume"
    * `company_discovery` → `input.names` (string items) joined ", "
    * `company_refresh` → `input.all ? "Every active board" : "Boards not fetched lately"`
    * default null.
* Renders `<RefreshOn kinds={["run.completed","run.failed","run.cancelled","tailor.done","render.done"]}/>`
  (`:31`), `<PageHeader/>`, `<ActivityView initialRuns={summaries}/>`.
* **Needs** `GET /api/activity/runs?limit=30` → `RunSummary[]` where
  `RunSummary = {run: LiveRun, target: string|null, costUsd: number|null, conversationId:
  string|null}` (`src/components/activity/run-summary.ts:7-14`). The `describeBrief` /
  `parseBrief` logic (`src/lib/ranking/brief.ts:56-63,151-169`) and `toLiveRun`
  (`src/lib/runs/live.ts:71-94`) move to Django.

### 3.5 `/chat` and `/chat/[id]` — `src/app/(app)/chat/[[...id]]/page.tsx` (Server Component)
* `params.id?.[0]` = conversation id or null (`:26`).
* New thread: `draftId = crypto.randomUUID()` minted **on the server per visit** (`:29`),
  rendered `<Chat chatId={draftId} initialMessages={[]} isNew key={draftId}
  suggestions={await suggestionsFor(user.id)} title={null}/>`. Keyed so "New chat" always
  remounts (`:13-19`).
* Existing: `getConversation(user.id, id)` (`src/lib/db/queries/conversations.ts:24-33`, UUID
  guard, user-scoped) → `notFound()` if null; `loadMessages(user.id, conversation.id)`
  (`conversations.ts:86-106`, ordered by `created_at asc`, maps to `{id, role, parts,
  metadata ?? undefined}`) cast to `ForayUIMessage[]`. Renders `<Chat chatId isNew={false}
  initialMessages={history} title={conversation.title} suggestions={[]}/>`.
* `suggestionsFor(userId)` (`:62-115`): `getOnboardingState`; no resume → 1 suggestion ("How
  does this work?"); not ready → 2 ("Set up my search", "What's on my resume?"); ready →
  `listMatches(stages found+shortlisted, limit 1)` top match; `role =
  preferences.target_roles?.[0]?.toLowerCase() ?? "roles"`; `where = locations?.[0]`; 4
  suggestions incl. `{title:"Add a company", prompt:"Start tracking this company for me: ",
  prefill:true}`.
* **Needs**: `GET /api/chat/suggestions` → `ChatSuggestion[]` (`{title, prompt,
  prefill?}`), `GET /api/conversations/<id>` → `{conversation:{id,title}, messages:
  UIMessage[]}` (today the route returns only `{messages}` — the page reads the title
  directly; the API would need the title too). The draft UUID can be minted in the browser.

### 3.6 `/companies` — `src/app/(app)/companies/page.tsx` (Server Component)
* `await requireUser()` but **not user-scoped** — the registry is shared (`:16-22`).
* Reads (raw drizzle in the page, `:33-40`):
  * `select * from companies order by name asc limit 2000`
  * `select company_id, count(id) from jobs where is_active = true group by company_id`
  (Comment `:26-32`: two queries instead of a correlated subquery because drizzle rendered
  the subquery column unqualified and it compared `jobs.company_id` to `jobs.id`.)
* `data = rows.map(row => toCompanyRow(row, jobsByCompany[row.id] ?? 0))`
  (`src/components/companies/company-row.ts:27-44`).
* Derived: `active = rows.filter(status==="active").length`, `onBoards = sum(activeJobs)`;
  description "{n} active board(s) with {m} open posting(s) between them. Foray refreshes
  them every six hours and keeps postings that disappear, marked closed."
* Renders `<RefreshOn kinds={["company.refresh.done","company.discovered"]}/>` (`:50`),
  `<PageHeader actions={<RefreshStaleButton/>}/>`, `<CompanyTable companies={data}/>`.
* **Needs** `GET /api/companies` → `CompanyRow[]` (shape in §5.6).

### 3.7 `/profile` — `src/app/(app)/profile/page.tsx` (Server Component)
* Reads (`:26-30`): `getOnboardingState`, `getBaseResume` (only `.version`, `.createdAt`,
  existence), `latestBaseRender(user.id)` (`src/lib/db/queries/resumes.ts:110-134`: newest
  base resume with a non-null `rendered_document_id` joined to `documents` of the same user).
* `pdf` prop to `ProfileHeader` (`:38-46`): `{documentId: render.document.id, version:
  render.resume.version, current: render.resume.version === base?.version}` or null;
  `canRender = Boolean(base && resume)`; title = `resume.basics.name.trim()` or "Your profile".
* If `!onboarding.hasProfile` → header + "Start with your resume" + `<ResumeDropzone
  hasResume={false}/>` (`:51-65`).
* Else: `blocking = progress.blocking.map(label.toLowerCase())`; `preferencesFirst =
  blocking.length > 0` reorders sections and PageNav items (`:67-157`).
  * Preferences section `#preferences`: `<PreferencesForm canSave={hasProfile}
    initial={preferences} key={JSON.stringify(preferences)}/>` — keyed so a refresh with new
    saved data remounts the form.
  * Resume section `#resume`: description "Version {base.version}, saved
    {timeAgo(base.createdAt)}…"; `<ResumeDropzone compact hasResume/>`; `<ProfileEditor
    initial={resume} key={base?.version ?? 0}/>` (keyed by version → remount after save), or
    an error if stored resume does not parse (`resume === null` while profile exists).
  * `<PageNav>` items with child counts of work/education/projects/skills.
* Renders `<RefreshOn kinds={["render.done"]}/>` (`:138`).
* **Needs** `GET /api/profile` → `{hasProfile, hasResume, ready, resume: Resume|null,
  preferences: Preferences, progress:{blocking:[{id,label}], answered,total},
  base:{version, createdAt}|null, render:{documentId, version}|null}`.

### 3.8 `/settings` — `src/app/(app)/settings/page.tsx` (Server Component)
* Reads (`:52-59`): `getProfile` (existence + `settings` JSON), `spendSince`,
  `spendByPurpose`, `spendByModel`, `spendByDay` (all since `monthStart()`,
  `src/lib/db/queries/spend.ts:14-67`), `jsearchBudget()` (`src/lib/sources/jsearch.ts:328-336`
  → `{used, budget, remaining, enabled}`).
* Server-only config reads:
  * `parseSettings(profile?.settings)` (`src/lib/profile/settings.ts:34-36`).
  * `estimateEvalCost(1)` (`src/lib/ranking/deep-eval.ts:453-479`) → `{usd, perItemUsd,
    model}`; `reviewModel = perItemUsd != null ? model.split(":").pop() : null` (`:63,100`).
  * `configuredPurposes()` (`src/lib/llm/models.ts:182-198`) → `[{purpose, ref, error}]`.
  * `configuredProviders()` (`src/lib/llm/registry.ts:77-81`) → present provider ids.
  * `reasoningFor(purpose)` (`models.ts:142-146`; env `LLM_REASONING_<PURPOSE>`).
  * `rateFor(ref)` (`src/lib/llm/pricing.ts:109-119`) → `{input, output}` USD/M tokens.
  * `inUse` = providers of configured refs (`:66-68`).
  * Month name / reset date computed in UTC (`:70-76`).
* Renders sections `#behaviour` (`<BehaviourForm canSave={Boolean(profile)}
  initial={settings} reviewCost reviewModel/>`), `#usage` (Stats: `formatUsd(total.usd,
  {precise:true})`, calls, tokens via `compact()` (`:311-315`); `<SpendChart
  days={monthDays(since, byDay)}/>`, `<PurposeTable/>`, `<ModelTable/>`,
  `<AggregatorMeter budget enabled resetsOn used/>`), `#models` (table: purpose label
  `PURPOSE_LABEL`, model, provider `PROVIDER_LABEL`, `EFFORT_LABEL[reasoningFor]`
  (`:37-44`), `priceLabel(rate)` (`:305-308`) or "Not priced", or `row.error` in red;
  provider-key chips for `["openai","anthropic","google","deepseek","compat"]` with "in
  use" / "configured" (compat) / "key set" / "not set"), `#worker` (`<WorkerPanel/>`),
  `#appearance` (`<AppearanceForm/>`), `#account` (initials, name, email,
  `<SignOutButton/>`), `<PageNav/>`.
* No `RefreshOn`.
* **Needs** `GET /api/settings` →
  ```
  user:{name,email}
  hasProfile: bool, settings: {defaultMode, scoreThreshold, deepEvalTopN}
  review: {perItemUsd: number|null, model: string}
  spend: {usd, calls, inputTokens, outputTokens}
  byPurpose: [{purpose, usd, calls}]           // order: sum(cost) desc
  byModel: [{provider, model, usd, calls, failures, avgLatencyMs}]
  byDay: [{day:"YYYY-MM-DD", usd}]
  monthStart: ISO date
  aggregator: {used, budget, enabled}
  purposes: [{purpose, ref|null, error|null, reasoning, rate:{input,output}|null}]
  providers: {present: ProviderId[]}
  ```
  Keys are never sent to the client (comment `:241-245`).

---------------------------------------------------------------------------------------------

## 4. The live channel (`src/components/live/*`) — polling contract

### 4.1 `LiveProvider` (`src/components/live/live-provider.tsx`, client)
* State (`:35-41`): `{ready, events: LiveEvent[], runs: Record<id, LiveRun>, approvals:
  LiveApproval[], worker: WorkerStatus|null}`.
* Constants (`:55-61`): `FAST_MS = 1500`, `SLOW_MS = 6000`, `HIDDEN_MS = 30_000`,
  `MAX_EVENTS = 300`, `ACTIVE = {"queued","running","awaiting_approval"}`.
* `poll()` (`:86-143`):
  * single-flight (`inFlight` ref).
  * `fetch("/api/live?since=" + cursor, {cache:"no-store"})`.
  * `401` **or** `response.redirected` → `router.refresh()` and stop (the server page's
    `requireUser` then redirects to `/sign-in`). Today an unauthenticated
    `/api/live` produces a redirect (requireUser → `redirect()`), hence `redirected`.
  * non-OK → ignored (retry next tick).
  * `cursor = snapshot.cursor`; runs are **merged** into the map by id (never removed —
    a run that leaves the snapshot keeps its last state); events appended and trimmed to the
    last 300; `approvals` and `worker` **replaced** wholesale.
  * `busy = snapshot.runs.some(ACTIVE) || snapshot.approvals.length > 0`.
  * Only after the first snapshot (`!initial`) each new event is dispatched to listeners
    and to `announce()` — the first snapshot is history and must not toast (`:124-131`).
  * Next delay: busy ? (hidden ? 6000 : 1500) : (hidden ? 30000 : 6000) (`:136-141`).
* Triggers: on mount; `visibilitychange`/`focus` when visible → `schedule(0)`; window event
  `foray:live-refresh` → `refresh()` (sets `busy=true`, `schedule(0)`).
* `rememberRun(run)` adds a run fetched elsewhere if not already known (`:177-181`).
* `watchRuns(ids)` (`:195-208`) — module-level ref-counted set of run ids an open chat is
  showing; their events are not toasted.
* `announce(event, navigate)` (`:211-247`), skipped if `event.runId` is watched:
  * `chat.followup` (FOLLOW_UP_EVENT): needs `data.conversationId` and `data.messageId`;
    calls `announceConversationsChanged()`; if not already on `/chat/<id>` →
    `toast("Foray followed up in chat: {label.toLowerCase()}.", {id:"event-<id>", action
    "Open chat" → router.push})`.
  * `approval.requested` → `toast(event.message, {id: "approval-<approvalId ?? id>",
    description: "Review it on Home or in the chat that started it.", classNames:{toast:
    "foray-toast-signal"}})` (class is not defined in `globals.css`).
  * `run.completed` | `tailor.done` | `render.done` → `toast.success(message)`.
  * `run.failed` → `toast.error(message)`.
  * `company.discovered` → `toast(message)`.
* Hooks:
  * `useLive()` — context or throws.
  * `useLiveEvent(kinds | predicate, handler)` (`:256-281`).
  * `useRun(runId)` (`:287-309`): returns `runs[runId]`; if unknown, **one** `fetch("/api/runs/"
    + runId, {cache:"no-store"})` → `LiveRun` JSON (non-OK → null), then `rememberRun`.
  * `requestLiveRefresh()` (`:312-316`) → dispatches `foray:live-refresh`.
  * `isActiveRun(run)` (`:318-320`).

### 4.2 `GET /api/live?since=<int>` contract (`src/app/api/live/route.ts`, `src/lib/runs/live.ts`)
* `since` parsed with `Number(…)`; non-finite or ≤0 → 0.
* `since === 0` (first poll of a page load) → `expireStaleApprovals(user.id)` first
  (resumes lapsed approvals as "no"; once per load) (`route.ts:18-21`).
* Response headers `Cache-Control: no-store`. Body `LiveSnapshot`
  (`live.ts:60-66`):
  ```
  cursor: number            // id of last event returned, else `since`
  events: LiveEvent[]       // since>0: id > since, asc, limit 200
                            // since=0: latest 60 by id desc, then reversed to asc
  runs: LiveRun[]           // status in (queued,running,awaiting_approval) OR
                            // updated_at > now()-10min; order created_at desc; limit 20
  approvals: LiveApproval[] // status=pending and (expires_at null or > now()),
                            // created_at desc, limit 20
  worker: {online, lastSeenAt: ISO|null, pid: number|null}
  ```
  `LiveEvent = {id:number, runId|null, level: debug|info|warn|error, kind, message, data,
  approvalId|null, createdAt ISO}` (`live.ts:18-27`).
  `LiveRun = {id, kind, status, mode: approve_each|autopilot, prompt: input.prompt ?? null,
  summary: kind==="search" ? describeBrief(parseBrief(input.brief ?? input.hints)) : null,
  stats: object, output: object|null, error|null, createdAt, startedAt|null,
  finishedAt|null}` (`live.ts:29-49,71-94`).
  `LiveApproval = {id, runId, kind: deep_eval_batch|shortlist_proposal|tailor|company_add,
  title, payload: object, createdAt}` (`live.ts:51-58`).
  Worker `online = now - system_state['worker'].updated_at < 45_000ms`
  (`src/lib/system/heartbeat.ts:18,42-57`).
* `RECENT_MS = 10 min` keeps finished runs in the snapshot so cards see final state
  (`live.ts:68-69`).

### 4.3 `RefreshOn` (`src/components/live/refresh-on.tsx`, client)
* `useLiveEvent(kinds, () => router.refresh())`; renders null. Used by home, matches,
  matches/[id], activity, companies, profile (kinds listed per page in §3).

### 4.4 Event kinds the frontend depends on (contract with the worker's `emit()`)
| kind | consumer |
|---|---|
| `run.completed` | toast success; RefreshOn home/matches/match/activity; chat catch-up (FOLLOW_UP_KINDS) |
| `run.failed` | toast error; RefreshOn activity; chat catch-up |
| `run.cancelled` | RefreshOn activity |
| `tailor.done` | toast success; RefreshOn home/match/activity; chat catch-up |
| `tailor.failed` | chat catch-up |
| `render.done` | toast success; RefreshOn match/activity/profile |
| `approval.requested` | toast (signal); RefreshOn home; chat catch-up; tone "signal" in feeds |
| `approval.decided` | RefreshOn home/matches |
| `company.refresh.done` | RefreshOn companies; `RefreshStaleButton` stops waiting + toasts message |
| `company.discovered` | toast; RefreshOn companies |
| `worker.ping` | `WorkerPanel` probe answer (uses `event.createdAt`) |
| `chat.followup` | toast + recent-chats reload; open chat reloads messages (`data.{conversationId, messageId, label}`) |

`FOLLOW_UP_KINDS` and `FOLLOW_UP_EVENT` come from `src/lib/chat/follow-up-kinds.ts:10-22`
(pure constants; also used server-side).

### 4.5 Run `stats` / `output` keys the UI reads (contract with worker)
* `stats.phase` — search: `starting, refreshing, aggregating, filtering, scoring,
  awaiting_evaluation, evaluating, awaiting_shortlist, shortlisting`; tailor: `starting,
  tailoring, verifying, revising, rendering` (`run-card.tsx:27-39,90-120`).
* `stats`: `boardsRefreshed, scored, candidates, evaluated, toEvaluate`.
* search `output`: `candidates, scored, evaluated, shortlisted, strong, topMatchIds |
  matchIds` (`run-card.tsx:69-77,381-385`; `run-summary.ts:57-66`).
* tailor / resume_render `output`: `documentId, matchId, unsupported, pages`.
* rank_deep: `evaluated`, `matchIds`. company_refresh: `ok`, `boards`.
* Approval `payload`: `items[{matchId, title, company, fastScore?, deepScore?,
  recommendation?, summary?}]`, `estimate {usd, perItemUsd, model}` (`approval-card.tsx:27-37`).

---------------------------------------------------------------------------------------------

## 5. Components (every non-`ui/` file)

Legend for lib imports: **[S]** = server-only module (db / queries / auth server / llm /
queue / env), even if only a type is imported; **[P]** = pure / shared, safe in the browser;
**(type)** = `import type`, erased at build.

### 5.1 `components/shell/*`
| file | client? | lib imports | network / actions | live |
|---|---|---|---|---|
| `app-shell.tsx` | client | `utils` [P] | — | `useLive()` → `working = runs.some(isActiveRun)`, `waiting = approvals.length` (badge on Home), WorkerLine (offline banner "Start it with `pnpm dev:worker`"; "N agents working"; "N runs waiting on you"; "Agents ready") |
| `recent-chats.tsx` | client | `utils` [P] | `GET /api/conversations` → `{conversations:[{id,title,updatedAt}]}` (route limit 30; UI shows first 20); `DELETE /api/conversations/<id>` (optimistic remove; if on that chat → `router.push("/chat")`) | reloads on window event `foray:conversations-changed` (`CONVERSATIONS_CHANGED`, `announceConversationsChanged()` `:19-23`); initial load deferred with `setTimeout(0)` |
| `theme-sync.tsx` | client | `theme` [P] (`applyEffectiveTheme, readThemeChoice, THEME_STORAGE_KEY`) | — | — |
| `toaster.tsx` | client | `theme` [P] (`isDarkNow, THEME_EVENT`) | — | sonner `duration 6000`, `bottom-right`, `closeButton` |
| `user-menu.tsx` | client | `auth/client` [P*] (`signOut`), `format` [P] (`initials`), `theme` [P] | `signOut()` → `/api/auth/sign-out`, then `router.push("/sign-in")`, `router.refresh()` | — |

AppShell nav (`app-shell.tsx:40-75`): Home `/home`, Matches `/matches`, Activity
`/activity`; Profile, Companies, Settings; "Ask Foray" → `/chat`.

### 5.2 `components/live/*` — see §4.

### 5.3 `components/foray/primitives.tsx` — server-safe (no `"use client"`)
* Imports `utils` [P], `format` [P] (`fitLevel, fitWord, initials, nameHue,
  RECOMMENDATION_LABEL, RecommendationId`).
* Exports `Blaze`, `Wordmark`, `fitClass`, `ScoreChip` (sizes sm/md/lg/xl, `evaluated` dot,
  title "{fitWord} ({score})"), `RecommendationPill`, `CompanyMark` (initials on
  `--hue = nameHue(name)`; **no logos fetched — privacy invariant** `:150-154`),
  `PageHeader`, `PageBody` (narrow/wide/full), `Section`, `EmptyState`, `Stat`, `SignalDot`.
* No server deps. Keep as is.

### 5.4 `components/home/*`
| file | client? | lib imports | actions / fetch | live |
|---|---|---|---|---|
| `live-panels.tsx` | client | `format` [P] (`timeAgo`), `utils` | `startSearchAction()` (StartSearchButton) | `AttentionPanel`: `useLive()` approvals+runs; active runs sorted by createdAt desc; approvals whose run is not active render standalone `ApprovalCard`; each active run → `RunCard`; hidden when empty. `RecentActivity`: last 6 non-debug events reversed. `StartSearchButton`: on ok → `toast.success`, `requestLiveRefresh()`, `router.refresh()`; on fail → `toast.error(message)` |
| `ask-box.tsx` | client | `utils` | — | `PENDING_PROMPT_KEY = "foray:pending-prompt"`; `startChatWith(router, prompt)` writes sessionStorage then `router.push("/chat")` — prompt never in URL/history (`:9-23`) |
| `onboarding-chat.tsx` | client | — | `startChatWith(router, "Let's set up my job search. Ask me what you need to know.")` | — |

### 5.5 `components/chat/*`
**`chat.tsx`** (client). Imports: `@ai-sdk/react` `useChat`; `ai` (`DefaultChatTransport,
getStaticToolName, isStaticToolUIPart, isTextUIPart`); `agents/chat-agent` **[S] (type
ForayUIMessage)**; `chat/follow-up-kinds` [P]; `utils`.
* `useChat<ForayUIMessage>({id: chatId, messages: initialMessages, transport: new
  DefaultChatTransport({api: "/api/chat", prepareSendMessagesRequest: ({id, messages}) =>
  ({body: {id, message: messages.at(-1)}})}), onFinish})` (`:67-89`) — **only the newest
  message is sent**; Postgres history is the authority.
* `onFinish`: `requestLiveRefresh()`; if new thread: `window.history.replaceState(null, "",
  "/chat/" + chatId)` (address bar only — a navigation would remount and drop the reply),
  `setPersisted(true)`, `announceConversationsChanged()`.
* Follow-up loop (`:93-141`): `runIdsIn(messages)` = every assistant static tool part with
  `state === "output-available"` and string `output.runId`; `watchRuns(those)`; on a live
  event in `FOLLOW_UP_KINDS` for one of those runs → show "Foray is looking at the results…"
  for up to 90 s; on `chat.followup` with `data.conversationId === chatId` → clear
  catching-up; if `data.messageId` → `reload()` (`GET /api/conversations/<chatId>` →
  `{messages}`, merged by id keeping local-only messages after server ones `:293-296`); if
  mid-stream, defer reload until idle.
* Pending prompt hand-off (`:151-174`): new thread reads+removes sessionStorage
  `foray:pending-prompt` after `setTimeout(0)` and `sendMessage({text})`, cancelled on
  cleanup (StrictMode double mount).
* Rendering: header title = `title ?? firstUserText(messages)` (first 80 chars of first
  non-run-update user message); `runUpdateLabel(message)` — user message with
  `metadata.kind === "run_update"` renders as a divider with `metadata.label` (`:299-318`);
  assistant text via `MessageResponse` (Streamdown); tool parts via `ToolPart`; "Thinking…"
  when submitted or streaming with no text/tool part yet; error box with "Try again" →
  `regenerate()`; composer: Enter sends, Shift+Enter newline, button stops while busy.
* `ChatSuggestion = {title, prompt, prefill?}` (`:48`); prefill puts text in composer.

**`tool-part.tsx`** (client). Imports `format` [P] (`formatLocation, STAGE_LABEL, StageId`),
`utils`; components `MatchLine`, `ApprovalCard`, `RunCard`, `CompanyMark`, `useLive`.
* `ToolPartView = {name, state, input, output, errorText?}`; states
  `input-streaming|input-available|approval-requested|approval-responded|output-available|output-error|output-denied`.
* COPY table of running/done labels per tool name (`:40-57`): get_profile_summary,
  get_resume, update_profile_preferences, add_company, discover_companies, list_companies,
  start_search_run, get_run_status, cancel_run, list_jobs, get_job, move_job_stage,
  evaluate_job, tailor_resume, list_pending_approvals, answer_approval.
* Rendering rules (`:66-150`) — **contract with the chat tools' output JSON**:
  * input states → pulsing "{running}…"; `output-error` → "{running} failed" + errorText.
  * `output.ok === false` → line with `output.message`.
  * **any `output.runId` string → `<RunCard runId/>`** (start_search_run, get_run_status,
    evaluate_job, tailor_resume).
  * `list_jobs` → `output.jobs: JobCardOut[]` (`{matchId|null, title, company, locations?,
    remote?, score?, fastScore?, deepScore?, recommendation?, headline?, salary?, postedAt?,
    stage?}`) as MatchLines with stage; score fallback `score ?? deepScore ?? fastScore`.
  * `get_job` → `output.{matchId,title,company,locations,remote,salary,postedAt,
    match:{deepScore,fastScore,deepEval.recommendation,stage}}`.
  * `update_profile_preferences` → input keys with non-null values listed; `output.saved`
    expandable; `output.blockingSearch: string[]`; "Ready to search." when empty and
    `output.ok`.
  * `add_company` → `output.company` (name), `output.source` (`_`→space).
  * `discover_companies` → `output.queued: string[]`.
  * `list_pending_approvals` → `output.approvals[].id` intersected with live
    `approvals` → ApprovalCards; none listed → "Nothing is waiting on you."; all answered →
    render nothing.
  * `move_job_stage` → `output.message` or `output.stage`.
  * `answer_approval` → `output.message`.
  * default → collapsed line with JSON details.

### 5.6 `components/companies/*`
* **`company-row.ts`** (no directive; imported by the **server action**
  `src/app/actions/companies.ts:6` and by client components). Imports `db/schema` **[S]
  (type Company)**. Exports:
  * `CompanyRow = {slug, name, domain|null, sourceType: greenhouse|lever|ashby|careers_page|none,
    atsSlug|null, careersUrl|null (careersUrls[0]), status: active|paused|invalid,
    needsBrowser, tags: string[], lastFetchedAt ISO|null, lastFetchStatus|null,
    lastFetchJobCount|null, discoveredVia: seed|agent|jsearch|user, activeJobs}`
    (`:8-25`).
  * `toCompanyRow(company, activeJobs)` (`:27-44`) — moves to Django serializer.
  * `BOARD_LABEL` (`:46-52`), `FETCH_PROBLEM` (`:55-60`: `not-found` "Board not found",
    `error` "Last fetch failed", `unsupported` "Cannot be fetched", `needs-browser` "Needs
    a browser") — used both in UI and in server-action messages.
* **`company-table.tsx`** (client): `refreshCompanyAction(slug)`, `setStatusAction(slug,
  "paused"|"active")`; `format.timeAgo`; client-side filter (All/Active/Paused/Invalid/Needs
  a browser), search over name/slug/atsSlug/domain/tags, sort name|jobs|fetched (never
  fetched last). Keeps a `fresh` overlay of rows returned by actions **until the
  `companies` prop identity changes** (i.e. until revalidation delivers new page data)
  (`:95-102`). `boardUrl()` builds `https://job-boards.greenhouse.io/<slug>`,
  `https://jobs.lever.co/<slug>`, `https://jobs.ashbyhq.com/<slug>` or careersUrl
  (`:67-80`). Newly added row is highlighted 2.4 s and scrolled into view.
* **`add-company.tsx`** (client): `addCompanyAction(query)` → `{ok:true, message, company:
  CompanyRow, existed}` | `{ok:false, message, trail: DiscoveryStep[]}`; imports
  `companies/discovery` **[S] (type DiscoveryStep = {step, detail})**. Elapsed-seconds
  counter while pending. `STEP_LABEL` maps step ids (`:22-33`): ats-link, domain,
  careers-page, unsupported-ats, homepage, homepage-ats-link, slug-guess, search,
  search-ats-link, careers-page-fallback. **This action can take ~30 s (synchronous
  discovery + board refresh inside the request).**
* **`refresh-stale-button.tsx`** (client): `refreshAllAction()` → `{ok, message, queued}`;
  waits for live `company.refresh.done` (toast its message) or gives up after
  `PATIENCE_MS = 3 min`; `requestLiveRefresh()` after queueing.

### 5.7 `components/matches/*`
* **`match-line.tsx`** (no directive — server-safe; used by Home (server) and RunCard/ToolPart
  (client)). Imports `db/queries/matches` **[S] (type MatchRow)**, `format` [P]. Exports
  `MatchLineData`, `fromMatchRow(row)` (`:38-52`: location via `formatLocation(locations,
  remote)`, `salary = formatSalary(row)`, `evaluated = deepScore != null`), `MatchLine`
  (links to `/matches/<matchId>` when matchId), `CompanyLine`.
* **`board.tsx`** (client). `moveMatchStageAction(matchId, target)`; type `MatchRow` [S];
  `format` [P]. dnd-kit board, `OPEN`/`CLOSED` columns, `COLUMN_PAGE = 40`, optimistic
  `overrides`, revert + `toast.error` on failure, success toast with **Undo** (moves back,
  then `router.refresh()`), `router.refresh()` after success. `BoardMatch` = Pick of
  MatchRow (`:45-62`): matchId, stage, title, company, locations, remote, score, deepScore,
  recommendation, salaryMin/Max/Currency/Period, postedAt, tailoredResumeId. `DndContext
  id="matches-board"` fixed for hydration (`:127-130`).
* **`match-table.tsx`** (client). `moveMatchStageAction`; filters: text, stage (open/all/one),
  min fit 0/55/70/80, deep-reviewed only, sort fit|newest; optimistic overrides;
  `router.refresh()` on success.
* **`match-controls.tsx`** (client). `StageSelect` (`moveMatchStageAction`, toast,
  `router.refresh()`), `EvaluateButton` (`evaluateMatchAction(matchId)` → `{ok, runId,
  message}`; shows `<RunCard compact runId/>`; `requestLiveRefresh()`), `MatchNotes`
  (debounced 800 ms autosave via `updateMatchAction(matchId, {notes: value.trim() ? value :
  null})`).
* **`evaluation.tsx`** (no directive — server-rendered). `format.fitWord` [P]. Exports
  `DeepEvalView` (`:20-31`: `score, recommendation, summary, matched_requirements[{requirement,
  evidence, fact_ids?, strength?: strong|partial}], gaps[{gap, severity:
  learnable|disqualifying, note?}], seniority_fit (match|under|over|unclear), location_fit
  (match|partial|mismatch|unclear), salary_fit (above|within|below|unstated), risk_flags[],
  talking_points[]`) and `FastEvalView` (`:33-39`: `score, fit_reasons[], red_flags[],
  seniority, hard_requirement_misses[]`). `clean()` strips bracketed fact ids
  `/\s*\[[0-9a-f]{8}-[0-9a-f-]{27,}\]/gi` from model prose (`:66-68`).
* **`posting.tsx`** (client). Pure text → paragraphs/bullets; `COLLAPSED_CHARS = 1600`.
* **`tailor-panel.tsx`** (client). `tailorResumeAction(matchId)` → `{ok, runId, message}`;
  shows `<RunCard runId/>` until the page refreshes with a different `tailored.id`
  (`delivered` logic `:63-69`, relies on RefreshOn `tailor.done`/`run.completed`);
  `<iframe sandbox="" src="/api/resumes/<tailored.id>/preview">`; links
  `/api/documents/<documentId>` and `?download=1`; `TailoredView` shape (`:34-44`) with
  `changes[{section, item_id?, before?, after?, reason}]`, `verification {passed?,
  unsupported[{section, text, reason, source?: rule|model}], revised?}`.

### 5.8 `components/runs/*`
* **`run-card.tsx`** (client). Actions: `cancelRunAction(runId)` (Stop), `startSearchAction({againFrom:
  runId})` (Run again, for failed/cancelled searches). Types: `MatchRow` [S] (type),
  `LiveRun` [S] (type, from `runs/live`). `useRun(runId)`, `useLive()` (approval for this
  run, `worker.online`). `describe(run, workerOnline)` (`:56-121`) produces the status
  sentence (queued + worker offline → "Waiting for the worker to start"). `StepTrack` from
  `stats.phase`. `RunResults` → `/api/documents/<id>` links or `ResultList` that fetches
  **`GET /api/matches?ids=a,b,c`** → `{matches: MatchRow[]}` (route: ids filtered by
  `/^[0-9a-f-]{36}$/i`, max 20; `src/app/api/matches/route.ts:10-20`). `useElapsed` ticks
  1 s while active. Local `KIND_TITLE` (`:41-48`) duplicates `run-summary.ts` (missing
  `profile_import`).
* **`approval-card.tsx`** (client). `decideApprovalAction(approvalId, {approved, itemIds:
  approved ? [...selected] : undefined})` → `{ok, message}`; `requestLiveRefresh()`.
  Items all pre-selected; `PREVIEW = 6`; cost = `estimate.perItemUsd * count ??
  estimate.usd`; COPY per kind (`:39-50`).

### 5.9 `components/activity/*`
* **`activity-view.tsx`** (client). `useLive()`, `useRun(extraRunId)`; merges server
  `initialRuns` with live runs (live overrides), adds runs that appeared since load (target
  `run.summary ?? run.prompt`, `costUsd: null`), shows active as `RunCard`s, finished as
  `RunList`; "Debug events" switch; `openRun()` fetches unknown runs via `useRun` and scrolls
  to `#run-<id>`.
* **`run-summary.ts`** (no directive). Types `LiveEvent, LiveRun` [S] (type). Exports
  `RunSummary`, `KIND_TITLE` (`:16-24`), `STATUS` (`:26-36`), `eventTone()` (`:39-44`),
  `runOutcome(run)` (`:51-90`).
* **`run-list.tsx`** (client). `RunEvents` fetches **`GET /api/runs/<id>/events`** →
  `{events: LiveEvent[]}` (route: user-scoped run lookup else 404; `select * from
  agent_events where user_id=$1 and run_id=$2 order by id asc limit 1000`;
  `src/app/api/runs/[id]/events/route.ts:11-47`), merges with live events of that run by
  id; links `/chat/<conversationId>`.
* **`activity-feed.tsx`** (client). Live events only (newest first, grouped by local day,
  `PAGE = 100`, debug hidden by default). No fetch — limited to what the live window
  holds (60 on first load + everything since).

### 5.10 `components/profile/*`
| file | client? | lib imports | actions / fetch |
|---|---|---|---|
| `resume-dropzone.tsx` | client | `utils` | **`POST /api/uploads`** multipart `file` → `{documentId, resumeId, usedVision, garble, counts:{work,education,skills,projects}}` or `{error}` (400/413/415/422). Client pre-checks `/\.(pdf|docx)$/i`. Success toast "Read N roles, N schools and N skills." then `router.refresh()`. Request is synchronous: one LLM extraction, "up to half a minute". |
| `profile-header.tsx` | client | — | `renderBaseResumeAction()` → `{ok, runId, message}`; `useRun(runId)`, `isActiveRun`, `requestLiveRefresh()`; link `/api/documents/<pdf.documentId>?download=1` |
| `page-nav.tsx` | client | `utils` | — (scroll-spy, `OFFSET = 140`) |
| `preferences-form.tsx` | client | `profile/interview` [P] (`SLOTS`, types `Preferences`, `SlotId`), `utils` | `savePreferencesAction(next: Preferences)` → `{ok, message}` |
| `profile-editor.tsx` | client | `resume/schema` [P*] (type `Resume`) | `saveProfile(cleaned: Resume)` → `{ok:true} \| {ok:false, error}`; `prune()` drops empty items before save (`:588-612`); new items get `crypto.randomUUID()` ids and `source:"user"` |
| `skills-editor.tsx` | client | `resume/schema` (type `SkillItem`), `utils` | — |
| `bullet-list.tsx`, `field.tsx` | client | `utils` | — |
| `form-controls.tsx` | client | `utils` | — (`FormRow, TagInput, Segmented, SaveBar, useUnsavedChanges` (beforeunload + Cmd/Ctrl+S), `sameContent` (null/undefined/key-order-insensitive deep compare)) |

PreferencesForm (`preferences-form.tsx`): option lists SENIORITY (intern, new_grad, entry,
mid, senior, staff_plus), REMOTE (remote, hybrid, onsite, any), PERIOD (year, hour);
`toPreferences` stores empty answers as null; `parseAmount` strips non `[0-9.]`;
`REQUIRED` from `SLOTS.filter(required)` shows "Needed to search".

### 5.11 `components/settings/*`
| file | client? | lib imports | actions / fetch | live |
|---|---|---|---|---|
| `appearance-form.tsx` | client | `theme` [P], `utils` | — | — |
| `behaviour-form.tsx` | client | `format` [P], `profile/settings` **[S] (types only: AutonomyMode, Settings)**, `utils` | `saveSettingsAction({defaultMode, scoreThreshold, deepEvalTopN})` → `{ok, message, settings}` | — sliders clamp: threshold 30–80, topN 0–40 (`:46-52`) though schema allows 0–100 / 0–60 |
| `sign-out-button.tsx` | client | `auth/client` (`signOut`) | `signOut()`; on `result.error` toast | — |
| `spend.tsx` | server-safe | `format` [P], `utils` | — exports `PURPOSE_LABEL`, `PROVIDER_LABEL`, `DaySpend`, `monthDays(start, rows, now)` (UTC days, future days null), `SpendChart`, `PurposeTable`, `ModelTable`, `AggregatorMeter` (quota default 200) | — |
| `worker-panel.tsx` | client | `format` [P] | `pingWorker()` → `{jobId}` | `useLive()` worker/ready; `useLiveEvent(["worker.ping"])`; while waiting `setInterval(requestLiveRefresh, 1500)`, give up at `PATIENCE_MS = 20_000` |

### 5.12 `components/auth/sign-in-form.tsx` (client)
* Imports `auth/client` (`signIn`, `signUp`).
* Sign up: `signUp.email({name: name || email.split("@")[0], email, password})`; sign in:
  `signIn.email({email, password})`; `result.error.message` shown, else defaults. Success →
  `router.push(mode === "sign-up" ? "/home" : next)` + `router.refresh()`.
* Google: `signIn.social({provider: "google", callbackURL: next})` when `googleEnabled`.
* Password `minLength={8}` (client hint only).

### 5.13 `components/ai-elements/*` (vendored, client)
* `conversation.tsx` (use-stick-to-bottom), `message.tsx` (`MessageResponse` = Streamdown
  with cjk/code/math/mermaid plugins), `code-block.tsx` (shiki; **not imported anywhere**).
  Imports only `utils` and `ai` types. No server deps.

### 5.14 `components/ui/*` (shadcn, Base UI flavour)
* 28 files; all import `cn` from the `cn` package (not `@/lib/utils`), Base UI primitives,
  cva, cmdk. **No `src/lib` imports, no network.** Keep unchanged.

---------------------------------------------------------------------------------------------

## 6. Mutations the frontend performs (server actions → future endpoints)

Server actions are called as plain async functions from client components. Each one does
`requireUser()` and most call `revalidatePath()` — **which is load-bearing** (see §8).

| action (file:line) | callers | input | output | revalidates |
|---|---|---|---|---|
| `decideApprovalAction(approvalId, {approved, itemIds?})` (`actions/approvals.ts:26-91`) | ApprovalCard | zod `{approved: bool, itemIds: string[] \| null \| undefined}` | `{ok, message}`; messages per kind/decision (`:70-90`); failure reasons already_decided / expired / missing | `/home`, `/activity`, `/matches` (layout) |
| `addCompanyAction(nameOrUrl)` (`actions/companies.ts:32-97`) | AddCompany | string ≤300 | `{ok:true, message, company: CompanyRow, existed}` \| `{ok:false, message, trail}` | `/companies` |
| `setStatusAction(slug, "active"\|"paused")` (`:99-117`) | CompanyTable | | `{ok, message, company}` | `/companies` |
| `refreshCompanyAction(slug)` (`:120-151`) | CompanyTable | | `{ok, message, company?}` | `/companies` |
| `refreshAllAction()` (`:154-178`) | RefreshStaleButton | | `{ok, message, queued}` | — |
| `moveMatchStageAction(matchId, stage)` (`actions/matches.ts:46-68`) | Board, MatchTable, StageSelect | uuid + stage | `{ok, message}` | `/matches` layout, `/home` |
| `updateMatchAction(matchId, {notes?, userRating?})` (`:79-98`) | MatchNotes | notes ≤20000 nullable, userRating int −10..10 nullable | `{ok, message}` | same |
| `evaluateMatchAction(matchId)` (`:104-145`) | EvaluateButton | uuid | `{ok, runId, message}` | same |
| `pingWorker()` (`actions/ping.ts:8-15`) | WorkerPanel | | `{jobId}` | — |
| `saveProfile(next: Resume)` (`actions/profile.ts:29-55`) | ProfileEditor | ResumeSchema | `{ok:true}` \| `{ok:false, error}` | `/profile` |
| `savePreferencesAction(input: Preferences)` (`:65-110`) | PreferencesForm | PreferencesSchema | `{ok, message}` | `/profile`, `/home` |
| `tailorResumeAction(matchId)` (`actions/resumes.ts:28-40`) | TailorPanel | | `{ok, runId, message}` | `/profile`, `/matches` layout, `/home` |
| `renderBaseResumeAction()` (`:43-57`) | ProfileHeader | | `{ok, runId, message}` | same (if ok) |
| `startSearchAction({prompt?, mode?, againFrom?})` (`actions/runs.ts:22-87`) | StartSearchButton, RunAgain | | `{ok, runId, message}` | `/home` |
| `cancelRunAction(runId)` (`:89-99`) | RunCard | | `{ok, message}` | `/home`, `/activity` |
| `saveSettingsAction(patch)` (`actions/settings.ts:12-38`) | BehaviourForm | `SettingsSchema.partial().strict()` | `{ok, message, settings}` | `/settings` |

Route handlers fetched directly by the frontend:

| endpoint | caller | response |
|---|---|---|
| `GET /api/live?since=` | LiveProvider | `LiveSnapshot` (§4.2) |
| `GET /api/runs/<id>` | `useRun` | `LiveRun` or 404 `{error}` |
| `GET /api/runs/<id>/events` | RunEvents | `{events: LiveEvent[]}` or 404 |
| `GET /api/matches?ids=` | RunCard ResultList | `{matches: MatchRow[]}` |
| `GET /api/conversations` | RecentChats | `{conversations:[{id, title ?? "Untitled chat", updatedAt}]}` |
| `GET /api/conversations/<id>` | Chat reload | `{messages: UIMessage[]}` or 404 |
| `DELETE /api/conversations/<id>` | RecentChats | `{ok:true}` or 404 |
| `POST /api/chat` | useChat transport | AI SDK UI-message SSE stream; header `x-conversation-id`; 400 "No message provided." |
| `POST /api/uploads` | ResumeDropzone | JSON (above) |
| `GET /api/documents/<id>[?download=1]` | `<a href>` in RunCard, TailorPanel, ProfileHeader | file bytes, `Content-Disposition` inline/attachment (RFC 6266), `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff` |
| `GET /api/resumes/<id>/preview` | `<iframe sandbox="">` in TailorPanel | HTML with CSP `default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'` |
| `/api/auth/*` | Better Auth client (sign-in/up/social/sign-out) | Better Auth protocol |

---------------------------------------------------------------------------------------------

## 7. `src/lib` modules: what the frontend keeps vs. loses

### Keep (pure; browser-safe; no server deps)
* `src/lib/format.ts` — `StageId, STAGE_LABEL, STAGE_HINT, RecommendationId,
  RECOMMENDATION_LABEL, fitLevel, fitWord, formatSalary, formatLocation, remoteLabel,
  timeAgo, postedLabel, formatUsd, formatDuration, plural, initials, nameHue`. Used by 16
  component/page files. Unit-tested in `src/test/unit/format.test.ts`. (Server side does not
  import it; the chat tool `jobCard` has its own `formatSalary` copy in
  `src/lib/agents/tools/search.ts`.)
* `src/lib/utils.ts` — `cn` re-export.
* `src/lib/theme.ts` — all of it (§9).
* `src/lib/chat/follow-up-kinds.ts` — `FOLLOW_UP_KINDS`, `FOLLOW_UP_EVENT` (Python needs a
  copy too).
* `src/lib/profile/interview.ts` — runtime `SLOTS` (labels, `required`) and types
  `Preferences`, `SlotId` for PreferencesForm (imports only zod).
* `src/lib/resume/schema.ts` — types `Resume`, `SkillItem` (only zod; could be kept or
  replaced by generated types).
* `src/lib/auth/client.ts` — only if the Django auth API stays Better-Auth-compatible;
  otherwise replace with plain fetches.

### Type-only imports from server modules (must be re-homed as frontend types)
* `ForayUIMessage` ← `src/lib/agents/chat-agent.ts:114-116` (inferred from the live agent:
  `InferAgentUIMessage<…>`). Needs a hand-written `UIMessage<Metadata, never, Tools>` type.
* `LiveEvent, LiveRun, LiveApproval, LiveSnapshot` ← `src/lib/runs/live.ts:18-66`.
* `WorkerStatus` ← `src/lib/system/heartbeat.ts:36-40`.
* `MatchRow`, `Stage` ← `src/lib/db/queries/matches.ts:15,31-58` (note: `stageChangedAt`,
  `postedAt`, `createdAt` are `Date` in RSC but will be ISO strings over JSON; all consumers
  already accept strings — `timeAgo`, `new Date(…)`).
* `Company` ← `src/lib/db/schema/jobs.ts:201` (only `sourceType`, `status`, `discoveredVia`
  enums are used by `CompanyRow`).
* `DiscoveryStep` ← `src/lib/companies/discovery.ts:135`.
* `AutonomyMode, Settings` ← `src/lib/profile/settings.ts:15-30` (module imports `db`).
* `ChatSuggestion` (`chat.tsx:48`), `RunSummary` (`run-summary.ts:7-14`), `TailoredView`,
  `DeepEvalView`, `FastEvalView`, `CompanyRow`, `BoardMatch`, `MatchLineData` are already
  frontend-defined.

### Lose (server-only; used today by pages, actions, routes)
`src/env.ts`; `src/lib/auth/index.ts`, `src/lib/auth/session.ts` (`getSession`,
`requireUser`); `src/lib/db/**` (db, schema, queries: matches, profile, resumes, spend,
conversations, companies); `src/lib/profile/onboarding.ts`; `src/lib/profile/settings.ts`
(runtime); `src/lib/ranking/brief.ts` (`describeBrief`, `parseBrief`,
`SAVED_PREFERENCES_PROMPT`); `src/lib/ranking/deep-eval.ts` (`estimateEvalCost`,
`costPhrase`); `src/lib/runs/*` (`toLiveRun`, `liveSnapshot`, `listRuns`, `getRun`,
`expireStaleApprovals`, `decideApproval`, starts); `src/lib/llm/*` (`configuredPurposes`,
`reasoningFor`, `rateFor`, `configuredProviders`); `src/lib/sources/jsearch.ts`
(`jsearchBudget`); `src/lib/companies/*`; `src/lib/queue/*`; `src/lib/storage`;
`src/lib/resume/extract`, `src/lib/resume/render/template`; `src/lib/system/heartbeat.ts`
(runtime).

Server-only logic currently living **inside the frontend tree** that Django must absorb:
* `home/page.tsx:138-156` `liveCounts` SQL; `:159-213` `describeState` (or keep on client).
* `activity/page.tsx:59-157` `describeRuns` + `target`.
* `companies/page.tsx:33-46` two-query registry read.
* `matches/[id]/page.tsx:50-70` tailored-resume read + `TailoredView` shaping.
* `chat/[[...id]]/page.tsx:62-115` `suggestionsFor`.
* `settings/page.tsx:52-76` config + spend aggregation.
* `components/companies/company-row.ts` `toCompanyRow`, `BOARD_LABEL`, `FETCH_PROBLEM`
  (shared with the server action's messages).

---------------------------------------------------------------------------------------------

## 8. Invariants and behaviours that must survive

1. Open-redirect guard on `?next=` (`sign-in/page.tsx:12-14`).
2. Every signed-in page validates the session itself, not just the layout or proxy
   (`(app)/layout.tsx:6-10`, `proxy.ts:4-11`).
3. Someone else's match / conversation / run / document is indistinguishable from a missing
   one: 404 → `notFound()` (`chat/[[...id]]/page.tsx:42-45`, `matches/[id]/page.tsx:46-47`,
   API routes).
4. Companies registry is shared, not user-scoped, but still requires sign-in
   (`companies/page.tsx:16-22`).
5. Chat sends only the newest message; history in Postgres is authoritative
   (`chat.tsx:39-45,71-76`; `api/chat/route.ts:13-20`). Client picks the conversation id up
   front; server claims it (`conversations.ts:53-81`).
6. New chat: address bar rewritten with `history.replaceState`, not a navigation
   (`chat.tsx:81-87`).
7. A prompt typed on Home travels via sessionStorage `foray:pending-prompt`, never the URL
   (`ask-box.tsx:9-23`); hand-off waits a tick and is cancelled on unmount
   (`chat.tsx:151-174`).
8. One shared poll; never per-component polls (`live.ts:8-16`; CLAUDE.md). Cadence
   1.5 s / 6 s / 30 s; first snapshot never toasts (`live-provider.tsx:124-131`).
9. Events of runs an open chat shows are not toasted (`watchRuns`).
10. Approval cards: all items selected by default; unticking narrows; double-answers decide
    once server-side (`approval-card.tsx:19-25`; `actions/approvals.ts:9-15`).
11. `signal` colour = agents working / waiting on user only (CLAUDE.md UI conventions).
12. Company monograms only; no third-party logo fetch (`primitives.tsx:150-154`).
13. Model prose has bracketed fact ids stripped before display (`evaluation.tsx:62-68`).
14. Resume preview is sandboxed and CSP-locked (`api/resumes/[id]/preview/route.ts:8-24`;
    `tailor-panel.tsx:133-138` `sandbox=""`).
15. Documents never cached (`private, no-store`) (`api/documents/[id]/route.ts:10-16,46`).
16. Dates on the settings page and spend chart are UTC (`settings/page.tsx:70-76`,
    `spend.tsx:30-45`).
17. Board drag: optimistic, reverted with a toast on refusal, Undo action
    (`board.tsx:91-122`).
18. Profile editor: whole resume saved at once, empty items pruned client-side, server
    stamps only changed items as `user` (`profile-editor.tsx:17-27,583-612`;
    `actions/profile.ts:21-28`).
19. Preferences form writes whole (empty = null), clears matching `skipped` slots
    (`actions/profile.ts:57-110`).
20. Theme: dark default, applied before first paint by inline script (§9).

---------------------------------------------------------------------------------------------

## 9. Theme (`src/lib/theme.ts`) usage

* Exports (`theme.ts:11-81`): `ThemeChoice = "light"|"dark"|"system"`,
  `THEME_STORAGE_KEY = "foray-theme"`, `DEFAULT_THEME = "dark"`, `THEME_EVENT =
  "foray:theme"`, `THEME_COLORS = {light:"#ecefea", dark:"#0d1310"}`, `readThemeChoice()`
  (localStorage, try/catch), `prefersDark(choice)`, `applyEffectiveTheme(choice?)` (toggles
  `dark` class on `<html>`, sets `style.colorScheme`, updates `meta[name=theme-color]`,
  dispatches `foray:theme`), `chooseTheme(choice)` (store + apply), `isDarkNow()`,
  `THEME_SCRIPT` (IIFE string inlined in `<head>`; built from the constants so it cannot
  drift).
* Consumers: `app/layout.tsx` (`THEME_COLORS`, `THEME_SCRIPT`), `shell/theme-sync.tsx`
  (apply on mount, follow OS when "system", cross-tab `storage` event),
  `shell/toaster.tsx` (`useSyncExternalStore(subscribe THEME_EVENT, isDarkNow, () =>
  true)`), `shell/user-menu.tsx` and `settings/appearance-form.tsx` (radio groups →
  `chooseTheme`).
* No server involvement at all; moves to the frontend-only app unchanged.

---------------------------------------------------------------------------------------------

## 10. Hazards for the Django move (frontend side)

1. **`revalidatePath` is load-bearing.** Several clients rely on the server action
   response carrying a refreshed RSC tree instead of calling `router.refresh()`:
   * `BehaviourForm` computes its `baseline` from the `initial` prop; after
     `saveSettingsAction` succeeds, without a refresh the form stays "dirty" and the
     SaveBar never hides (`behaviour-form.tsx:48-59`).
   * `PreferencesForm` (keyed by `JSON.stringify(preferences)`) and `ProfileEditor` (keyed
     by `base.version`) remount only when the page re-renders with new data
     (`profile/page.tsx:80-84,107`). After save, `dirty` stays true for PreferencesForm
     until refresh (baseline from `initial`).
   * `CompanyTable`'s `fresh` overlay is dropped only when the `companies` prop changes
     identity (`company-table.tsx:95-102`).
   * `ApprovalCard`, `RunAgain`, `EvaluateButton`, `TailorPanel`, `ProfileHeader` rely on
     revalidation plus live events.
   With a JSON API, every mutation's caller must explicitly `router.refresh()` (if pages
   stay RSC that fetch from Django) or invalidate its client cache.
2. **AI SDK UI-message stream protocol.** `useChat` + `DefaultChatTransport` expect the AI
   SDK v7 SSE protocol (header `x-vercel-ai-ui-message-stream: v1`, verified in
   `node_modules/ai/dist/index.js`; `data:` JSON chunks of type `start`, `start-step`,
   `text-start`/`text-delta`/`text-end`, `tool-input-start`/`tool-input-delta`/
   `tool-input-available`, `tool-output-available`, `tool-output-error`,
   `tool-output-denied`, `tool-approval-request`, `message-metadata`, `finish-step`,
   `finish`, `error`, `abort`, then `data: [DONE]`). Django
   must emit this byte-exactly, with message ids (the route sets `generateMessageId:
   generateId` because an empty id collides on the `messages` PK — `api/chat/route.ts:72-78`),
   and tool parts named `tool-<toolName>` with `state`, `input`, `output`, `errorText`
   because `tool-part.tsx` and `runIdsIn()` read exactly those. `regenerate()` re-sends the
   last user message with the same id (the server appends it to a history that already
   contains it).
3. **Persisted `messages.parts` JSON is the AI SDK UIMessage part format**, returned verbatim
   by `GET /api/conversations/<id>` and used as `initialMessages`; Django must store and
   return it unchanged.
4. **`ForayUIMessage` type is inferred from the server agent.** A frontend-only app needs a
   hand-written tool-type map or loses type safety on tool parts.
5. **Auth client coupling.** `signIn.email`, `signUp.email`, `signIn.social`, `signOut` from
   `better-auth/react` speak Better Auth's REST protocol at `/api/auth/*` (same-origin,
   `createAuthClient()` with no baseURL, `auth/client.ts:5-9`). Either Django reproduces
   that protocol (and Better Auth's password hash format) or the four call sites are
   rewritten. `proxy.ts` checks Better Auth's cookie name.
6. **Same-origin assumptions everywhere.** All fetches are relative (`/api/...`), document
   links are plain `<a href>` relying on the session cookie, and the resume preview CSP is
   `frame-ancestors 'self'`. If Django serves on another origin, Next must reverse-proxy
   `/api/*` (Next rewrites) or the CSP/cookies/CORS must change.
7. **401 detection in LiveProvider** treats `401` or a followed redirect as "signed out" and
   calls `router.refresh()`. A Django 403 (CSRF/permission) would be silently ignored as
   "non-OK".
8. **Long synchronous requests**: `POST /api/uploads` (LLM extraction, up to ~30 s) and
   `addCompanyAction` (discovery + board refresh, ~30 s) block the request; Django needs
   timeouts ≥ that (or a job + live event).
9. **Server-only config surfaced in UI** (settings page): models per purpose, reasoning,
   prices, provider-key presence, aggregator budget, google-enabled flag — must become API
   fields; keys never leave the server.
10. **RSC Date serialization**: `MatchRow` dates are `Date` objects today; over JSON they
    become ISO strings. All current consumers tolerate strings, but `Board`/`MatchTable`
    sorting by `new Date(postedAt ?? 0)` must keep working with `null`.
11. **Server-minted draft chat id** (`chat/[[...id]]/page.tsx:29`) — keyed remount on each
    visit to `/chat`; if made client-side, keep a fresh id per navigation.
12. Small inconsistencies to keep or fix deliberately: duplicate `KIND_TITLE`
    (`run-card.tsx:41-48` vs `run-summary.ts:16-24`); `foray-toast-signal` class not defined
    in CSS; `ai-elements/code-block.tsx` unused; proxy matcher lists `/pipeline` and `/jobs`
    which have no pages; `generateMetadata` on `/matches/[id]` re-queries the match.
