# Step 8C: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 8C owns `src/app/(app)/{profile,settings,companies}/**`, `src/components/{profile,companies}/**`
and `src/components/settings/**` except `sign-out-button.tsx`. It adds two files there:
`src/components/profile/resume.ts` (the editor's resume type) and
`src/components/profile/run-outcome.ts` (`useRunOutcome`, §2). No backend file changed.
`spend.tsx`, `appearance-form.tsx`, `page-nav.tsx`, `form-controls.tsx`, `field.tsx` and
`bullet-list.tsx` needed no change.

## 1. `toCompanyRow` is kept, as legacy, until step 11 (§6.7 says it is deleted here)

- **What.** `company-row.ts` now re-exports the generated `CompanyRow` (`lib/api/types`) and keeps
  `BOARD_LABEL` and `FETCH_PROBLEM`. It also keeps `toCompanyRow(company, activeJobs)`, typed
  against the legacy drizzle `Company` and returning the generated `CompanyRow` (two casts:
  `sourceType`, `lastFetchStatus`). `BOARD_LABEL` keeps a `none: "No board"` key, which no Django
  company has.
- **Why.** `src/app/actions/companies.ts` imports `toCompanyRow`, `BOARD_LABEL` (indexed with the
  legacy `sourceType`, which includes `none`) and `CompanyRow` from this module. Nothing calls that
  action any more, but `tsc` still compiles it, and the file is step 11's to delete
  (`src/app/actions/**`). Deleting `toCompanyRow` here would leave the branch failing `tsc` until
  then.
- **Handoff (step 11).** Delete `toCompanyRow`, the `Company` import and the `none` key together
  with `src/app/actions/**`.
- **Evidence.** With `toCompanyRow` renamed away (a scratch edit, reverted), `tsc --noEmit`
  reports `src/app/actions/companies.ts(6,38): error TS2724: … has no exported member named
  'toCompanyRow'`. As committed, `tsc` exits 0.

## 2. One hook for the components that report their own run: `useRunOutcome`

- **What.** `useRunOutcome(runId, onEnd)` (`src/components/profile/run-outcome.ts`) is used by
  `ResumeDropzone`, `AddCompany`, `CompanyTable`'s per-row refresh and `RefreshStaleButton`.
  - It registers the run with `watchRuns([runId])` while mounted, so `announce()` stays quiet
    (§6.3, step-8A.md §5). The caller toasts with `{ id: runToastId(runId) }`.
  - It reports the run's ending event (`run.completed`, `run.failed` or `run.cancelled`) exactly
    once per run id. It finds that event by scanning the live window (`useLive().events`), not
    only the events dispatched to listeners.
  - It returns `useRun(runId)`.
- **Why the ending event, not the run.** `LiveRun` carries no message, and the toast's sentence is
  the ending event's message ("Read 2 roles, 1 schools and 0 skills.", "Added Linear. …"). R4
  makes that event unique and last. `live.snapshot` reads events before runs, in separate
  queries, so one poll can show the run finished while its event only arrives with the next poll.
  Acting on the event avoids reconstructing the sentence.
- **Why scan the window.** The provider never dispatches the events of its first snapshot. A run
  that ended inside it would otherwise never be reported, and the component would spin forever.
  The scan costs a `find` over at most 300 events per poll.
- **Row refreshes are watched from the table.** `CompanyTable` renders one `RefreshWatch` (a
  `null`-rendering component around the hook) per running refresh. So a row hidden by the search
  box or a filter still reports its outcome, and the generic toast stays quiet.

## 3. What each component shows and toasts

- **`ResumeDropzone`.**
  - `POST /api/uploads` with a `FormData` body. openapi-fetch passes a `FormData` body through and
    leaves the multipart `Content-Type` to the browser.
  - Refusals before any run (400, 403, 413, 415, 429) stay inline under the zone, as today; they
    are not toasted.
  - On 202 it shows a compact `RunCard` under the zone.
  - On `run.completed` it shows one `toast.success(message, {id, description})`. The description
    is today's: the vision sentence when `data.usedVision`, else "Check the details…". It then calls
    `router.refresh()`.
  - On `run.failed` it shows `toast.error(message, {id})`.
  - On `run.cancelled` (Stop on its card) the zone is free again, and the stopped run's card
    stays, saying "Stopped…", until the next upload. Nothing was saved, so there is no refresh
    and no toast.
  - Its props are unchanged (`hasResume`, `compact`, `className`), so Home (8B) keeps using it
    as is.
- **`AddCompany`.**
  - A 202 shows today's "Looking for a job board for …" line and a compact `RunCard`. Start
    refusals (400, 429) are toasted as errors.
  - Registered (or already listed): `toast.success(message, {id})`, then `onAdded(output.company)`.
    The table highlights the row and scrolls to it, as before.
  - No board (the run completes at level `warn`): today's toast, "No job board found for “q”."
    when the trail is not empty, else the message. The miss box lists the trail.
  - `run.failed`: the error is toasted and shown in the miss box, with no trail.
  - `run.cancelled`: the form is free again, and the stopped run's card stays until the next
    add, as in the dropzone.
- **`CompanyTable`.**
  - "Fetch now" answers 202. The row dims, shows a spinner instead of its menu, and gets a
    compact `RunCard` in an extra table row under it. That row's divider is removed, so the card
    reads as part of the row.
  - The outcome: `run.completed` at level `info` is `toast.success`. At level `warn` (a 404, an
    error, an unsupported board) it is `toast.error`, because today's `refreshCompanyAction`
    returned `ok:false` for those and the table toasted them as errors. `run.failed` is
    `toast.error`. `run.cancelled` (Stop on the row's card, which goes with the run) is a plain
    `toast(message, {id})`: "Stopped. Everything found so far was kept.".
  - The row's new counts arrive through the page's `RefreshOn`, so the table does not refresh
    the page itself.
  - Pause and Resume are `PATCH /api/companies/{slug}`. They show the returned row at once (the
    `fresh` overlay), toast the message, then `router.refresh()`.
- **`RefreshStaleButton`.**
  - On 202 it toasts the start message ("Refreshing N stale boards…" or "Every active board was
    fetched…") under the run's toast id. When the run ends, its outcome replaces that toast in
    place, so one toast is ever on screen.
  - It shows no run card, so a stop can only come from elsewhere (Activity, chat). The cancel
    message then replaces the start toast, under the same id.
  - It no longer listens for `company.refresh.done`.
  - The spinner gives up after today's 3 minutes. After that the run is unwatched, and its
    outcome comes from the generic toast, under the same id.
- **`ProfileHeader`.** It calls `POST /api/resume/render`, then `router.refresh()`. The PDF link
  itself arrives with the page's `RefreshOn(run.completed)`.

## 4. The editor's resume type: `editableResume`

- **What.** `src/components/profile/resume.ts` defines `EditableResume` from the generated
  components: `basics.links`, `work`, `education`, `projects`, `skills` and every item's
  `highlights` are required. `editableResume(resume)` fills them in once, on the profile page,
  before `ProfileEditor` gets the resume. `skills-editor.tsx` imports `SkillItem` from
  `lib/api/types` (§6.7).
- **Why.** The generated `Resume` is ninja's validation-mode schema, so every field with a
  default (`default_factory=list`) is optional in TypeScript. The page always sends every key
  (`tests/pages/snapshots/profile.json`), and the editor indexes `resume.work`,
  `item.highlights` and so on without guards. What the editor sends back is assignable to the
  generated `Resume`, which `PUT /api/resume` takes.

## 5. Smaller choices

- **`WorkerPanel`.**
  - The answer reads "Answered by process 4123 in under a second.", using `worker.ping`'s
    `data.workerPid`.
  - It matches the answer to its own `jobId` once `POST /api/worker/ping` has replied. Before
    that, any answer counts.
  - The heartbeat line no longer shows a pid.
  - When `lastSeenAt` is null, the heartbeat line says "The worker is not running. Start it with
    `pnpm dev:worker`.". The note under it then mentions only `pnpm dev:all`, so the hint is not
    repeated.
  - A refused ping shows Django's message. `PingOut` has no `message` field, so the code reads it
    through a cast; the fallback is today's "The test job could not be queued…".
- **`PreferencesForm`.**
  - It takes `slots` (served by the page) instead of importing `SLOTS`, and "Needed to search"
    comes from `slot.required`.
  - It has no `canSave` and no disabled fieldset.
  - After a save it calls `router.refresh()`. The page re-renders with the stored answers, and
    the form, keyed on them, remounts clean.
- **`BehaviourForm`.** It has no `canSave`. After a save it calls `router.refresh()`, and the new
  `initial` becomes the baseline, which hides the save bar.
- **`ProfileEditor`.** It calls `PUT /api/resume` and toasts Django's message ("Profile saved")
  with today's description. It then calls `router.refresh()`; the editor is keyed on
  `base.version`, so it remounts on the new version.
- **Profile page.**
  - It reads `GET /api/pages/profile` and branches on `hasResume`.
  - Without a resume it shows: the header, "Start with your resume" with the dropzone, then the
    Preferences section, described by the same blocking rule as the other branch.
  - With a resume, the layout is today's. The "stored resume could not be read" line stays as a
    type guard; `hasResume` now implies a readable resume.
  - Both branches have `RefreshOn(["run.completed"])`, per §6.7's table.
- **Settings page.** It reads `GET /api/pages/settings`.
  - The behaviour description is always "Defaults for every search…".
  - The per-purpose `rate` and `reasoning` come from the page.
  - The month name and reset date come from `monthStart`, in UTC.
- **Companies page.** It reads `GET /api/pages/companies` and uses its `active` and
  `openPostings`. Its `RefreshOn` kinds are `run.completed`, `company.discovered` and
  `company.refresh.done` (§6.7).

## 6. API gaps for 8E (none blocking)

No Done item needs a backend change. These would let the frontend drop hand-written types:

- **Run outputs and event data are not in the OpenAPI schema.** The affected shapes:
  - `ImportOutput` (the dropzone reads `usedVision`);
  - `CompanyAddOutput` (`AddCompany` reads `registered`, `company`, `trail`; its `company` is a
    plain `dict` in `catalogue.py`, though it is always a `CompanyRow`);
  - `worker.ping`'s `{jobId, workerPid}`.

  The frontend types them by hand: `AddOutput` in `add-company.tsx`, `{usedVision}` in
  `resume-dropzone.tsx`, `PingAnswer` in `worker-panel.tsx`, as 8A did for `DiscoveryStep`
  (step-8A.md §3). Registering them (`register_schema`) and typing `CompanyAddOutput.company` as
  `CompanyRow` would type them for real.
- **`Resume` is generated in validation mode**, so list fields are optional (§4). An output-mode
  component for the profile page's `resume` would make `editableResume` unnecessary.
- **Error bodies are not in any operation's responses.** Only the 200/202 shape is generated, so
  `call()` returns a refusal (`{ok:false, message}`) typed as the success shape. That works
  wherever the success shape has `message` (every 8C endpoint but one). `PingOut` is
  `{ok, jobId}`, so `WorkerPanel` reads a refused ping's `message` through a cast (§5). Declaring
  the 4xx responses (an `ErrorOut {ok: false, message}` on each operation) would type it.

## 7. Handoffs

- **Step 11:** §1 (`toCompanyRow`, the `Company` import, `BOARD_LABEL.none`).
- **8B:** the cards this step shows are 8B's `RunCard`. Its `describe()` gains `profile_import`,
  `company_discovery` and `company_refresh` in 8B (§6.7). Until both lanes are merged, those
  cards show 8B's current text for the three kinds.

## 8. Checks run in this step

This lane ran twice in the same worktree. The second round built on the first round's commit
(`WIP step 8C`), re-checked it against §5.3, §6.2, §6.3, §6.7, §9.7, §11.1, step-5B.md §7 and
step-5C.md §2, and changed only the `run.cancelled` handling of §3 (the first round's comments
said a stopped run "says so on its card", but the card was removed with the run). Every check
below was re-run in the second round.

- **Static.**
  - `pnpm exec next typegen`, then `tsc --noEmit`: exit 0.
  - `eslint` over every owned path, and over the whole repo: exit 0, no output.
  - `pnpm build`: exit 0, with every route compiled (`/companies`, `/profile` and `/settings`
    are dynamic).
- **The contracts the components rely on.** A scratch pytest file (`backend/tests/
  test_scratch_lane8c.py`, deleted afterwards, never committed) drove the exact requests the
  components send. It ran on `test_foray_app_lane8c` with a real in-process worker
  (`run_deferred`), a fake `extract` model and a canned web (5B's and 5C's test fixtures), and
  read `/api/live?since=` the way `LiveProvider` does: a first snapshot, then the cursor, keeping
  both in the window that `useRunOutcome` scans. 3 passed:
  - **Profile, no resume:** `hasResume:false`, `resume`, `base` and `render` null, 8 slots, some
    of them `required`.
  - **Preferences:** a `PUT /api/preferences` body built the way `toPreferences` builds it (every
    key, empty answers null) gave 200 `{ok:true, message:"Preferences saved"}`. The page's
    `preferences` changed, so the form's key changes and it remounts clean.
  - **Settings:** `PATCH /api/settings` gave 200 `{ok:true, message:"Settings saved", settings}`,
    and the settings page then held exactly the sent values (the new baseline).
  - **Worker:** before any worker ran, `/api/live` gave `worker = {online:false,
    lastSeenAt:null, pid:null}` (the panel's "The worker is not running." case). `POST
    /api/worker/ping` gave `{ok:true, jobId:1}`; the poll then held one `worker.ping` event with
    `data == {jobId: 1, workerPid: 59009}`. After the in-process Worker stopped gracefully,
    `lastSeenAt` was null again (§9.7's declared change).
  - **Upload:**
    - a text file named `resume.pdf` gave 415 `{ok:false, message:"Upload a PDF or a Word
      (.docx) file."}`, the body the dropzone shows under the zone;
    - a request with no Origin gave 403 `{ok:false, message:"Cross-site request refused."}`;
    - `two-column.pdf` gave 202 `{ok, runId, documentId, message:"Reading your resume…"}`. The
      window then held exactly one ending for the run: `run.completed` at level info, "Read 2
      roles, 1 schools and 0 skills.", with `data.usedVision` false.
  - **Resume editor:** the page's `resume` held every key the editor indexes (`basics`,
    `summary`, `work`, `education`, `projects`, `skills`, every item's `highlights`). `PUT` of
    it, edited, gave `{ok:true, message:"Profile saved"}`, and `base.version` went from 1 to 2.
    A 41-character `startDate` gave 400 `{ok:false, message:"Experience item 1: the start date
    is too long (40 characters at most)."}`.
  - **Add company:**
    - `https://jobs.ashbyhq.com/linear` gave 202 `{ok, runId, message}`. Its one ending was
      `run.completed` at level info, "Added Linear. Its Ashby board has 4 open roles.", with
      `registered:true`, a `company` holding exactly the 14 `CompanyRow` keys, and the trail
      `[{step:"ats-link", detail:"pasted ashby:linear"}]`. The row was on the companies page.
    - "Nowhere Labs" completed at level `warn`, "No job board found for “Nowhere Labs”. Try its
      website or a link to its jobs page.", with `registered:false` and a two-step trail.
  - **Refresh a row:** 202 `{ok, runId, message}`, then one ending, `run.completed` at level
    info ("Linear: 4 open roles, no changes."). A 404 board gave one ending at level `warn`
    ("Gone Co: board not found, so it is marked invalid.").
  - **Pause:** `PATCH` gave 200 with the row's `status:"paused"` (14 keys) and "Paused Linear.
    Its board is skipped until you resume it.".
  - **Refresh stale:** 202 `{ok, runId, queued:0, message:"Every active board was fetched in the
    last two hours, so none are stale."}`, then one ending, `run.completed` "None of your boards
    are stale.", in the same pass.
- **Backend suites behind these contracts.** `tests/pages`, `tests/resumes/test_upload.py`,
  `tests/resumes/test_pipeline.py`, `tests/jobs/test_pipeline.py`, `tests/runs/test_api.py` and
  `tests/accounts/test_interview.py`, with `-m "not slow"`: 168 passed, 2 deselected. No backend
  file differs from the base commit.
- **Money.** No model was called ($0).
- **Legacy `foray`.** The write counters were 41158/1014040/5351 before and after.
- **Processes.** No server or worker was started (the in-process Workers above belong to
  pytest and stop with each `run_deferred`).
- **Deferred to 8E / integration** (group G4 rule: no browser pane, no dev servers, no worker).
  Each of the step's Done items runs in the pane:
  - **Fresh user:** the dropzone and the Preferences form show, and a Preferences save
    succeeds.
  - **Upload:** the fixture shows a run card, then exactly one counts toast.
  - **Saves:**
    - a Preferences save hides the SaveBar;
    - a resume editor save bumps the version;
    - a settings save hides the SaveBar.
  - **Worker:** Test shows "Answered by process N". With the worker stopped normally, the panel
    says "The worker is not running.".
  - **Companies:**
    - adding a company and refreshing a row each show a run card and exactly one outcome toast;
    - "Refresh stale boards" answers within seconds.

## 9. Observations, no change

- **The first round saw the Docker database's clock about 11.7 s behind the host,** and two
  render-retry tests (`tests/resumes/test_pipeline.py::test_render_retries_once_and_keeps_the_
  run_running` and `::test_render_fails_after_its_retry`) failed under it: procrastinate
  scheduled the retry at the host's "now", the database's future. In the second round the
  clocks agreed (`clock_timestamp()` in `foray-db-1` read 1791565529.03, between host readings
  of 1791565528.98 and 1791565529.05) and both tests passed. If they fail again, check the skew
  first.
- **The panel tolerates such a skew.** `WorkerPanel` falls back to the time the page heard the
  answer when `event.createdAt` is earlier than the send.
