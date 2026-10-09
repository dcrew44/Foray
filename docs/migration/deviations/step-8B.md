# Step 8B: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 8B owns `src/app/(app)/{home,matches,activity,onboarding}/**` and
`src/components/{home,matches,runs,activity}/**`. It changed only frontend files. The four pages
are each one `django()` read plus JSX, and every action call site calls Django through `call(api.…)`
from `lib/api/client`. No owned file imports `@/app/actions`, `@/lib/{db,auth,profile,runs,ranking,
resume}` or any other module step 11 deletes. `onboarding/page.tsx` (a redirect) and
`home/{ask-box,onboarding-chat}.tsx`, `matches/{evaluation,posting}.tsx` needed no change.

## 1. Components that start a run watch it and toast it themselves (`use-run-toasts.ts`)

- **What.** The new hook `useRunToasts(runId)` (`src/components/runs/use-run-toasts.ts`) is used by
  the four components that start a run and stay on screen while it works: `StartSearchButton`
  (Home, Matches), `RunAgain` (inside a failed or stopped search's `RunCard`), `EvaluateButton` and
  `TailorPanel` (match page). While the component is mounted, the hook:
  - registers the run with `watchRuns([runId])`, which holds back the shell's `announce()` for it;
  - toasts `run.completed` / `run.failed` itself, with `{ id: runToastId(runId) }`;
  - toasts `approval.requested` for that run exactly as `announce()` does (same id
    `approval-<id>`, description and `foray-toast-signal` class).

  If the component unmounts first, `announce()` toasts the outcome instead, exactly once (§6.3).
  `StartSearchButton`'s own "Search started…" toast also takes the run's toast id, so the outcome
  replaces it rather than stacking under it.
- **Why.** The lane's instruction applies §6.3's rule to every component that starts a run. The
  approval case is the addition. `announce()` returns early for any watched run, before its
  `switch`, so watching a search would also swallow "Search paused for your go-ahead". On Home the
  approval card shows it, but on Matches ("Search again") nothing else would. The other kinds
  these components start (`rank_deep`, `tailor`) never ask for approval.
- **Evidence.** The headless check (§8): "Search again" while a search runs toasted the 409; no
  outcome toast was provoked (no worker runs in this group, §9).

## 2. Stop toasts the cancel message

- **What.** `RunCard`'s Stop calls `POST /api/runs/{id}/cancel`, then `requestLiveRefresh()` and
  `router.refresh()`. The answer is toasted:
  - `200` "Stopped. Everything found so far was kept." with the run's toast id (a stop is the run's
    outcome, and nothing else toasts `run.cancelled`);
  - `409` "That run had already finished." as a plain error toast, so it never replaces the
    outcome toast of the finish it lost to.
- **Why.** Today's button ignored the answer, and the card's own sentence for a cancelled run said
  the same thing. But Home's "In progress" panel lists only active runs, so the card vanishes on the
  next poll once the run stops. Without the toast, the user would never see the message there, and
  the Done check is "Stop on a run card shows the cancel message".
- **Evidence.** Headless check: Stop on the activity page's card → the toast "Stopped. Everything
  found so far was kept.", then the run moved to the finished list as "Stopped". Through the
  rewrite: the first cancel was `200` with that body, and a second was `409 {"ok":false,"message":
  "That run had already finished."}`.

## 3. Three small shared modules inside the lane's folders

- **`src/components/matches/stages.ts`** holds `OPEN_STAGES`, `CLOSED_STAGES` and `ALL_STAGES`.
  - They used to come from `lib/db/queries/matches`, which step 11 deletes.
  - The board and the list each had a private copy.
  - The module has no directive, because Home (a server component) reads `OPEN_STAGES`. A constant
    imported into a server component from a `"use client"` module arrives as a client reference,
    not as the array.
- **`src/components/matches/match-api.ts`** holds `moveMatch` and `saveMatchNotes`
  (`PATCH /api/matches/{id}` through `call()`). The board (including its Undo), the list, the stage
  picker and the notes share them.
- **`src/components/runs/use-run-toasts.ts`** is §1.

## 4. Page types come from the generated schema directly

- **What.** The pages import `components["schemas"]["HomePage" | "MatchesPage" | "MatchDetailPage" |
  "ActivityPage"]` from `@/lib/api/schema`.
- **Why.** `lib/api/types.ts` (8A's) re-exports the 15 friendly names of §6.7, and those do not
  include the page shapes.
- **Also.** These now come from the generated types:
  - **components:** `MatchRow`, `LiveRun`, `LiveEvent`, `LiveApproval`, `RunSummary` and
    `TailoredView`;
  - **8A's handoff:** `activity-feed`, `run-list`, `run-summary`, `approval-card` and `run-card`
    no longer import `lib/runs/live`. `activity-view` only ever imported through `run-summary`.

  `run-summary.ts` re-exports `RunSummary` from `lib/api/types` so its importers did not change.
  `TailorPanel` no longer exports its own `TailoredView`.
  - It takes the generated type.
  - It casts `changes` and `verification` to the fields it reads. They stay open objects in the
    schema (step-7.md §4).
- **Optional (8A's file).** `types.ts` could add the four page names. Nothing depends on it.

## 5. Small behaviour notes

- **Match page title.**
  - `generateMetadata` makes the same `django()` GET as the page; Next memoises it, so the two make
    one call.
  - A 404 now becomes `notFound()` in the metadata too. Today's title fell back to "Match" before the
    page itself called `notFound()`.
- **`RefreshOn` kinds** are §6.7's:
  - home: `run.completed`, `approval.requested`, `approval.decided`;
  - matches: `run.completed`, `approval.decided`;
  - match: `run.completed`;
  - activity: `run.completed`, `run.failed`, `run.cancelled`.

  Tailoring and rendering end with `run.completed`, so `tailor.done` / `render.done` are gone.
  `TailorPanel`'s "delivered" switch still works: the page refreshes on the tailor's
  `run.completed` with the new `tailored.id`.
- **`router.refresh()`** follows every former action call site (§6.2). That covers:
  - approval decision (always, so a 409 "That was already answered." refreshes too);
  - Stop, Run again, Start a search, evaluate and tailor;
  - every stage move (board, Undo, list, stage picker);
  - notes saves.

  The typed notes survive a refresh, because they are the component's state.
- **`RunCard.describe()`** gains the three kinds that are now runs (§9.4):
  - `profile_import`: running "Reading your resume" / "Picking out your roles, schools and skills"
    / "Saving your profile"; done "Read 3 roles, 2 schools and 14 skills." from `output.counts`,
    with real plurals.
  - `company_discovery`: "Looking for job boards", then "Fetching the new board's postings" while
    `refreshing`; done = `output.message`, the add sentence its `run.completed` carried.
  - `company_refresh`: "Waiting for the scheduled board refresh to finish" in `waiting`; done =
    `output.message`.

  `KIND_TITLE` lives only in `activity/run-summary.ts`, typed `Record<RunKind, string>`, so a new
  kind fails `tsc` until it has a title. As 5C's deviation notes, only the search card shows a
  prompt.
- **`RecentActivity`'s dot** uses `run-summary`'s `eventTone`. It is the same mapping, written once.

## 6. API gaps for 8E

None. Every endpoint and shape the four pages and their components need is in step 7's generated
schema, and no backend file was touched.

One observation for 8E's pane checks, not a gap. §5.2 answers expected refusals with 4xx statuses,
and Chrome logs every non-2xx `fetch` as a console error ("Failed to load resource: … 409
(Conflict)"). Some examples:
- "Search again" while a search runs;
- a decision someone else already answered;
- Stop on a run that just finished.

The server actions answered `200 {ok:false}`. The 8B Done flow (render, drag, approve, stop)
provokes none of these, but a pane check that does will see a console error by design.

## 7. Deferred to 8E (pane checks of this step)

Every Done item of §15.2 step 8B runs in the browser pane, which this group may not use. They
are:
- Home, Matches (board and list), match detail and Activity render with a throwaway user's data;
- a drag-and-drop stage move survives a reload;
- Approve on an `ApprovalCard` advances the run (it also needs a worker);
- Stop on a run card shows the cancel message;
- there are no console errors.

§8 ran the same flows headlessly on spare ports. It stubbed the approval answer, so nothing was
queued.

## 8. Checks run in this step

- **Static.**
  - `pnpm exec next typegen`, then `tsc --noEmit`: exit 0.
  - `eslint` over the whole repo: exit 0.
  - `pnpm build`: exit 0, with all 13 routes; the four converted pages are dynamic (`ƒ`).
  - The legacy-import grep over the owned files found nothing.
- **pytest** (`FORAY_TEST_DB_NAME=test_foray_app_lane8b`, `--create-db`). The run covered
  `tests/pages`, `tests/core/test_wire_format.py`, `tests/runs/test_api.py` and `tests/matching`:
  210 passed. These are the endpoints the pages call; no backend file changed.
- **SSR smoke, spare ports, no pane, no worker.**
  - **The stack.**
    - `DJANGO_ORIGIN=http://127.0.0.1:8108 pnpm build`;
    - `next start -p 3108`;
    - `uvicorn foray.asgi:application --port 8108`, later restarted with
      `PUBLIC_ORIGIN=http://localhost:3108` in its own process environment only, so the browser's
      Origin passes §4.3 (`.env` was not touched);
    - one `create_test_user --with-resume --with-match` user with a `dev_session` cookie.
  - **Seeded rows.** A few `runs` rows (and one pending `deep_eval_batch` approval) were written
    straight into `foray_app` for that user, with no procrastinate job.
  - **GET results, each checked for its expected text, for no `NEXT_REDIRECT`, and for no error
    boundary:**
    - `/home`, `/matches`, `/matches?view=list`, `/matches?stage=found`, `/matches?closed=1`,
      `/matches/<id>` and `/activity` answered 200.
    - Activity showed each run's Django-built target: "Smoke search for backend roles",
      "smoke-resume.pdf", "Boards not fetched lately". It also showed "5 scored, 1 strong" and the
      failed refresh's error.
    - `/matches/not-a-uuid` and an unknown UUID rendered "Nothing here.", with status 200. The
      page streams under `(app)/loading.tsx`, so `notFound()` comes after the headers. The legacy
      pages behaved the same way.
  - **Mutations through the rewrite.**
    - `PATCH /api/matches/<id> {"stage":"shortlisted"}` answered 200 "Moved Machine Learning
      Engineer, Trust & Safety at Vercel to Shortlisted.". `/matches` then said "1 open match, 1
      shortlisted and 0 applied.".
    - Cancel answered 200, then 409 (§2).
    - `POST /api/searches` while a seeded search ran answered 409 with its `runId`. No job was
      deferred.
- **Headless Chrome check** (scratch `ui.mjs`: `playwright-core` with the system Chrome, dark
  scheme, against :3108). It passed 9/9 twice. The earlier runs failed only on the check's own
  setup:
  - the browser's Origin, before the API trusted :3108 (403);
  - locators that matched two elements;
  - a repeated notes text, which needed no save.

  The checks:
  1. Home renders with "Waiting on you" from the live feed.
  2. Approve sends exactly `{"approved":true,"itemIds":["<match id>"]}` and shows "Approved. Foray
     is on it.". The answer was stubbed with `page.route`, so no continuation was queued.
  3. "Search again" toasts the 409 sentence.
  4. Stop shows the cancel toast, and the run moves to the finished list as "Stopped".
  5. A pointer drag of the card to the Applied column toasts "Moved to Applied", and the card is
     in Applied after a reload.
  6. A list-view stage change answers PATCH 200 and survives a reload.
  7. The match page renders, and a notes edit shows "Saved" and survives a reload.
  8. An activity row opens to its events.
  9. There were no console errors apart from the one provoked 409.
- **Afterwards.**
  - Both servers were stopped, and :3108 and :8108 are free.
  - The one throwaway user was deleted by email, with its 1 session. It was not
    `create_test_user --purge`, which would also delete other lanes' `@example.test` users.
  - No orphaned runs are left. `procrastinate_jobs` has no `todo`/`doing` rows, and there are 0
    worker rows.
  - The worktree's `.next` was rebuilt with the default `DJANGO_ORIGIN`.
  - The legacy `foray` write counters stayed at 41158/1014040/5351.
  - No model was called, so $0 was spent.
