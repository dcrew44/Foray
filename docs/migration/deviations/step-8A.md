# Step 8A: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 8A owns `src/app/{page,layout}.tsx`, `src/app/(app)/layout.tsx`, `src/app/(auth)/**`,
`src/app/globals.css`, `src/components/{auth,shell,live}/**`,
`src/components/settings/sign-out-button.tsx` and `src/lib/api/{types,auth}.ts`.
`src/app/layout.tsx`, `src/components/live/refresh-on.tsx`, `shell/theme-sync.tsx` and
`shell/toaster.tsx` needed no change.

## 1. Every `(app)` page is still legacy, so a signed-in visit to one loops (hazard for 8B–8D)

- **What.** Each page under `src/app/(app)/` (home, matches, match, activity, chat, companies,
  profile, settings) still calls the legacy `requireUser()` (Better Auth) until 8B–8D convert it.
  With only Django's `sessionid` cookie, Better Auth finds no session and the page redirects to
  `/sign-in` (no `next`). The sign-in page, as §5.3 says, sends a signed-in visitor on to
  `safeNext(next)` = `/home`, whose legacy page redirects to `/sign-in` again. The page redirect
  happens inside the `(app)/loading.tsx` Suspense boundary, after the layout has streamed, so it
  is a client-side redirect and the browser never stops: the pane ping-pongs `/home` ↔ `/sign-in`
  with full page loads several times a second.
- **Why it is not fixed here.** The pages belong to 8B–8D, and the sign-in redirect is §5.3's
  rule. The loop disappears page by page as each lane converts its page to a `django()` read.
- **For the lanes.** Never navigate the pane, signed in, to a page that is still legacy (the nav
  links, `/`, and `/sign-in` while signed in all lead to `/home`). If the pane gets caught, delete
  its session, which ends the loop at `/sign-in`:
  `uv run python manage.py shell -c "from django.contrib.sessions.models import Session;
  Session.objects.filter(session_key='<key>').delete()"`. Clearing the cookie from JavaScript does
  not work: the first browser request through the rewrite slides the dev session and replaces the
  cookie with an `HttpOnly` one (step-3A.md §2).
- **Evidence.** `curl -b <cookie> :3000/home` → 200 whose stream holds
  `NEXT_REDIRECT;replace;/sign-in;307;`. Once in the pane, the web log alternated
  `GET /sign-in 307` / `GET /home 200` until the session row was deleted; the pane then settled
  on `/sign-in`.

## 2. The shell checks ran on a scratch page inside `(app)` (deleted afterwards)

- **What.** "The shell shows Agents ready / Worker offline" and "sign-out works" ran on
  `src/app/(app)/shell-check/page.tsx`, which rendered only a header inside the real `(app)`
  layout (so `getMe()`, `LiveProvider`, `AppShell` and the `Toaster` all ran for real). It was
  deleted after the checks, and `.next/dev/types` was cleared so `tsc` does not look for it. The
  verifier can re-create it exactly:

  ```tsx
  import { PageBody, PageHeader } from "@/components/foray/primitives";
  import { SignOutButton } from "@/components/settings/sign-out-button";

  export default function ShellCheck() {
    return (
      <PageBody>
        <PageHeader actions={<SignOutButton />} description="Scratch page for the shell check." title="Shell check" />
      </PageBody>
    );
  }
  ```
- **Why.** Every real `(app)` page loops (§1), and step 8A may not edit them. The precedent is the
  G1 integration's scratch page (integration-G1.md §5). `SignOutButton` is on the scratch page
  because its only real host, the settings page, is 8C's and still legacy.
- **Signed-in redirects without navigating into the loop.** `/` → `/home` and
  `/sign-in?next=//evil.example` → `/home` were checked in the pane with the page's own `fetch()`
  (it follows the 307 and reports `redirected: true, url: …/home`), and with `curl -b` (`307`,
  `Location` `/home`). Navigating there would have landed on the legacy `/home` (§1).

## 3. `DiscoveryStep` is written by hand in `types.ts`

- **What.** `types.ts` re-exports the 14 other friendly names from the generated schema, and
  defines `DiscoveryStep = { step: string; detail: string }` itself.
- **Why.** It is not an OpenAPI component: the discovery trail lives in a `company_discovery`
  run's `output`, which the schema types as an open object (`LiveRun.output`). The shape is
  `apps.runs.catalogue.DiscoveryStep`'s. Registering it (`register_schema`) would be a backend
  change for 8E, and `add-company.tsx` (8C) reads it from run output either way.

## 4. Auth helpers: a discriminated result, and sign-out treats 401 as done

- **What.** `lib/api/auth.ts` returns `AuthResult` = `{ok: true, user}` (sign-in, sign-up) or
  `{ok: true}` (sign-out), else `{ok: false, message}` with Django's sentence (or `call()`'s
  "Could not reach Foray…" / "That request did not go through…"). `signOut()` treats a 401 ("Not
  signed in.": the session had already expired) as signed out, since `POST /api/auth/sign-out` is
  authenticated and the user is out either way.
- **Why.** The generated operations type only the 200 body (`AuthOut` with `ok: boolean`), so a
  plain `call()` result cannot be narrowed on `ok`.
- **Also.** The user menu's Sign out now toasts "Signing out did not work. Reload the page and try
  again." and stays put on failure, as `SignOutButton` already did; today's menu navigated to
  `/sign-in` regardless, which with Django unreachable would land on an error page.

## 5. One toast per outcome: `runToastId()` for the components that toast their own run

- **What.** `live-provider.tsx` exports `runToastId(runId)` → `"run-<runId>"`. `announce()` uses it
  for `run.completed` / `run.failed` (falling back to `event-<id>` for an event with no run), drops
  the `tailor.done`, `render.done` and `company.discovered` toasts, and keeps the
  `approval.requested` and `chat.followup` toasts as they were.
- **For 8B/8C.** `ResumeDropzone`, `AddCompany`, `CompanyTable`'s refresh and `RefreshStaleButton`
  register their run with `watchRuns([runId])` while mounted and toast with
  `{ id: runToastId(runId) }` (§6.3).

## 6. `.foray-toast-signal`

- **What.** In `globals.css` (`@layer utilities`): `[data-sonner-toast].foray-toast-signal` sets a
  marigold-tinted border (`color-mix(in oklab, var(--signal) 60%, var(--border)) !important`) and
  `--tw-inset-shadow: inset 3px 0 0 0 var(--signal)`.
- **Why that selector.** The toaster's own classes are `!important` utilities in the same layer
  (`!border-border`, `!shadow-lg`), so the class needs the extra attribute to outrank them. The
  left bar rides on `shadow-lg`'s composition through `--tw-inset-shadow`, so the lg shadow stays.
- **Evidence.** In the pane (dark), an element with the toaster's classes computed
  `border-left-color rgb(37, 48, 42)`; with `foray-toast-signal` added,
  `oklab(0.607 0.006 0.084)` and `box-shadow: rgb(240, 184, 76) 3px 0px 0px 0px inset, …, rgba(0,
  0, 0, 0.1) 0px 10px 15px -3px, …` (`--signal` is `#f0b84c` in dark).

## 7. `recent-chats.tsx` catches a failed load

- **What.** `RecentChats.load()` is wrapped in `try/catch`; a failed load keeps the last list.
- **Why.** §6.7 lists the file as unchanged, but a fetch cut off by a full page load (seen during
  §1's loop) or a restarting dev server logged `Uncaught (in promise) TypeError: Failed to fetch
  at RecentChats.useCallback[load]`, a console error the step's Done check forbids. The file is
  this step's (`src/components/shell/**`).

## 8. Smaller choices

- `ShellUser` is `Pick<components["schemas"]["MeOut"], "name" | "email">` (app-shell's "types
  only" change). It is taken from the schema because a `"use client"` file may not import
  `lib/api/server`'s `Me`, even as a type (step-3G.md §1).
- The sign-in page reads `GET /api/auth/config` through `django()` after `getMeOrNull()` (only when
  the visitor is signed out), and passes `signupOpen` to the form, which hides the "New to Foray?
  Create an account" line when it is false.
- `(app)/layout.tsx`'s comment now says each page's own Django read is the second guard, not
  `requireUser()`.

## 9. Handoffs (files other steps own)

- **8B: `src/components/activity/activity-feed.tsx`.** §6.7 lists its `LiveEvent` import switch
  (`lib/runs/live` → `lib/api/types`) under step 8A, but §15.2 gives the file to 8B. The same
  switch is due in `activity/{run-list,activity-view}.tsx`, `activity/run-summary.ts` and
  `runs/{approval-card,run-card}.tsx`. They compile today only because the legacy types are
  structurally compatible with the generated ones; step 11 deletes `lib/runs/live`.
- **`src/proxy.ts`.** §6.7 also lists it under 8A; step 3G already made all three of §6.5's
  changes (`sessionid`, `x-foray-path`, no `/pipeline` or `/jobs`). Nothing left to do.
- **`src/lib/api/server.ts` (3G's).** Its hand-written `Me` (step-3G.md §3) is identical to the
  generated `MeOut` and could become `components["schemas"]["MeOut"]`; optional, for the next owner.
- **8B–8D:** §1's loop; `runToastId` (§5).

## 10. Checks run in this step

All in the browser pane (`preview_start foray-web`, i.e. `pnpm dev:all`), with a
`create_test_user` user and `pnpm dev:session` cookies. No model was called ($0); no search ran.

- **Signed out.** `/` → `/sign-in`; `/matches` → `/sign-in?next=%2Fmatches`; `/sign-in` rendered
  "Welcome back" with `performance` navigation `redirectCount: 0`. curl agreed: `/` 307 →
  `/sign-in`, `/matches` 307 → `/sign-in?next=%2Fmatches`, `/sign-in` and
  `/sign-in?next=//evil.example` 200.
- **Signed in.** In-pane `fetch()`: `/` and `/sign-in?next=//evil.example` both ended at
  `http://localhost:3000/home` (`redirected: true`). curl: `/`, `/sign-in` and
  `/sign-in?next=//evil.example` 307 → `/home`; `/sign-in?next=/matches` 307 → `/matches`.
- **Error path.** `curl -X POST :3000/api/auth/sign-in` with the test user's email (unusable
  hash) → `HTTP 400 {"ok":false,"message":"Invalid email or password"}`. In the pane, on
  `/sign-in`, `fetch('/api/auth/sign-in', {method: 'POST', …})` with that email → status 400, the
  same body, and `location.pathname` still `/sign-in` 1.5 s later.
- **No Google button.** The sign-in page's text and buttons ("Sign in", "Create an account") have
  no "Google", in dark and in light (screenshots of both).
- **Shell.** On the scratch page (§2), dark: the sidebar read "Agents ready" with the test user's
  name and email; light (`foray-theme=light`, reload): the same, background `rgb(236, 239, 234)`.
  The theme key was removed afterwards (it was unset before).
- **Worker offline.** The worker's three Workers last heartbeat at 06:15:42.767Z; `kill -9` of
  `manage.py worker` at 06:15:47.926Z. "Worker offline … Start it with `pnpm dev:worker`." appeared
  in the sidebar at 06:16:27.916Z: **45.1 s after the last heartbeat** (40.0 s after the kill).
  The pane was hidden, where `LiveProvider` polls only every 30 s, so the check dispatched
  `foray:live-refresh` every 2 s to stand in for a visible tab's cadence.
- **Sign-out.** The user menu's "Sign out" (opened and clicked from JavaScript, the pane being
  hidden): `POST /api/auth/sign-out` 200, the pane on `/sign-in` ("Welcome back"), `/api/me` 401,
  and the session row gone. `SignOutButton` on the scratch page, with a fresh session: the same.
- **Console.** A fresh tab on the scratch page, through the live poll, the offline banner and both
  sign-outs, logged no error except the 401 of the check's own `fetch('/api/me')` after signing
  out. (The error-path tab logged the deliberate 400 as "Failed to load resource".)
- **Static.** `pnpm exec next typegen`, then `tsc --noEmit` exit 0; `eslint` exit 0;
  `pnpm build` exit 0 (its `BetterAuthError: You are using the default secret` lines come from the
  legacy `(app)` pages, §1).
- **Afterwards.** `create_test_user --purge` → "Deleted 1 test user."; `foray_app` has 0 users,
  0 sessions, 0 active runs and no todo/doing jobs. The kill left 3 crashed `procrastinate_workers`
  rows, deleted by SQL (heartbeat older than 60 s). :3000, :8000 and :8001 are free and no dev
  process remains. Legacy `foray` write counters stayed at 41158/1014040/5351.

## 11. Observations, no change

- While a preview is open, the desktop app probes `HEAD /` about three times a second
  (`lsof` shows the `Claude` process as the client). `/` always redirects, so each probe costs one
  `GET /api/me` 401 in the api log. The legacy `/` redirected as well, so the probe is not new
  with this step.
- The worker's start ran the six-hourly cron's catch-up (`companies.refresh_scheduled`, job 244),
  as integration-G3.md §3 describes. It refreshes boards only; no aggregator search.
