# Group G4 integration: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

G4 is steps 8B, 8C, 8D and 9. Each lane's single commit was cherry-picked onto
`claude/middleware-django-migration-389b1a` in that order:

- 8B: `ede1324` became `5780b12`;
- 8C: `89e044c` became `5ea1fe5`;
- 8D: `8bda934` became `7e4662e`;
- 9: `3228c4f` became `a13fdc5`.

**There were no conflicts.** The lanes own disjoint files (8B: `(app)/{home,matches,activity}`
and `components/{home,matches,runs,activity}`; 8C: `(app)/{profile,settings,companies}` and
`components/{profile,companies,settings}`; 8D: `(app)/chat`, `components/chat` and
`lib/api/chat-types.ts`; 9: `Dockerfile`, `docker-compose.yml`, `.dockerignore`). No lane file
needed a change to make the lanes work together. The one code change in this commit is §1, a
handoff in step 3A's test file.

## 1. Handoff applied: `test_sliding_session_rules` expects the chat view's 400

- **What.** `backend/tests/accounts/test_auth.py::test_sliding_session_rules` (step 3A's file)
  now asserts `chat.status_code == 400` for `client.post("/api/chat", {})`, with a comment citing
  step-6.md §4. The rest of the test (no `Set-Cookie`, no stamp: `/api/chat` never slides) is
  unchanged.
- **Why.** It still asserted step 1's 503 stub, so the full suite had one failure on the combined
  code. That failure is older than G4: step 6 replaced the stub, and step-6.md §4, step-7.md §9
  and step-9.md §5 each left it open. It is not a combination problem. It was applied here,
  as integrations G1 and G2 applied test handoffs, so that the suite is green for 8E and step 10.
  The fix is the one step-6.md §4 suggests.
- **Evidence.** Before: `1 failed, 1700 passed`, `assert 400 == 503` at `test_auth.py:515`.
  After: `1701 passed`. A form-encoded `{}` is "That request could not be read." (400,
  `text/plain`), as step-6.md §5 says.

## 2. What combining the lanes resolved

- **The chat route no longer traces Better Auth or `pg`** (step-8D.md §6). 8B converted
  `runs/{run-card,approval-card}.tsx` off the legacy server actions. `pnpm build` now prints no
  `BetterAuthError` line, and none of the 13 built routes' `page.js.nft.json` lists
  `better-auth`, `pg`, `drizzle-orm` or `playwright`. (Before G4, 8A's build printed those lines
  from the legacy `(app)` pages.)
- **8C's cards show 8B's `describe()`** (step-8C.md §7). In the pane, the dropzone's card read
  "Profile import / Starting…", the add-company card "Company discovery / Looking for job
  boards", and the row-refresh card "Board refresh / Refreshing job boards", then "Linear: 31
  open roles, no changes.".
- **No legacy import is left in pages or components** except the two that are documented: the
  type-only `Company` import in `companies/company-row.ts` (step-8C.md §1, step 11) and
  `lib/chat/follow-up-kinds` (step 6's, pure TypeScript that stays).

## 3. How the deferred pane checks ran

- **Servers.** `pnpm dev`, `pnpm dev:api` and `pnpm dev:worker` ran as three background processes,
  each logging to a file through a Python line timestamper. Each had `LLM_FAKE=1` and
  `JSEARCH_ENABLED=false` in its process environment only. `.env` was not touched. The pane was
  opened on `http://localhost:3000` with `preview_start {url}`, not `foray-web` (which would
  start a second `dev:all`).
- **The worker was restarted once, with `LLM_FAKE_DELAY_MS=500`, for the Stop check.** On
  plain fakes a search has no window to press Stop in. A second search scored 259 roles in under
  1 s, and a run awaiting approval shows no Stop button (the card shows the `ApprovalCard`
  instead, as before). With the delay, a fresh search was still scoring when Stop was pressed.
  The stop before the restart doubled as 8C's "worker stopped normally" check.
- **The pane was hidden throughout.** Clicks, typing and key presses through the pane tools
  worked anyway (each was confirmed by reading the DOM). Screenshots taken right after a
  navigation were current. As in step-8A.md §10, the checks dispatched `foray:live-refresh`
  every 1.5–2.5 s in place of a visible tab's poll. Sonner pauses its timers while the
  document is hidden, so toasts stay on screen. A `MutationObserver` recorded every toast that
  was added or rewritten, and those records are what "exactly one toast" counts.
- **The upload.** The pane cannot pick a file, so the fixture was copied to
  `public/g4-two-column.pdf` for the duration (deleted afterwards). The page fetched it, wrapped
  it in a `File` named `two-column.pdf`, and handed it to the dropzone's own `<input type=file>`
  through a `DataTransfer` and a `change` event. That is the component's real upload path.
- **The `list_jobs` card** needs a fake plan that 8E owns (step-8D.md §2). For this check
  only, `apps/chat/fakes.py` got exactly the plan step-8D.md §2 describes, before the servers
  started. It was removed after they stopped: `git diff --exit-code backend/apps/chat/fakes.py`
  is clean and nothing of it is committed. **8E still adds the plan for real.**
- **Users.** Two `create_test_user` users:
  - U1, fresh, with no resume: every 8C, 8B and 8D check;
  - U2, fresh, with no resume: the dark and light visual check of the no-resume profile and of
    a run card.

  The switch from U1 to U2 used the Settings "Sign out" button. Both users were purged at the
  end.

## 4. Observations, no change

- **Each board refresh shows twice in the event feeds.** A `company_refresh` run emits
  `company.refresh.done` and then `run.completed`, both at level `info` with the same sentence
  (step-5C.md §3). Home's "Latest activity" and Activity's event log list both. After two row
  refreshes and one stale refresh, Home showed "Linear: 31 open roles, no changes." four times
  and "Refreshed 22/22 boards — 4876 postings, 28 closed" twice. The legacy app had no run
  here, so it showed one line. No Done item covers this, and either fix lies outside the
  integration's files:
  - the backend could emit `company.refresh.done` at `debug`;
  - the feeds could drop an event whose run's terminal event repeats its message.

  Left to step 10 (acceptance) or 8E.
- **`next dev` logged `⨯ Error: The destination stream closed early.` three times.** Each came
  right after "Search again" on Matches, when the check navigated to `/home` within a second.
  The `router.refresh()` RSC stream for `/matches` was cut by the navigation. The browser logged
  nothing.
- **`dev_session` with an empty email argument** mints a session for the oldest user, as
  designed (`dev_session.py`). One such stray session for U1 came from a scripting slip. It was
  never used, and the purge deleted it.
- **The JSearch counter** stayed at `jsearch 2026-10 = 3`: every search logged "JSearch
  unavailable: JSEARCH_ENABLED is false".

## 5. Handoffs not applied (still open)

- **8E (backend, then types).** These are 8E's by §15.2, and none of them is a combination
  problem:
  - register the chat tool input models and replace the `TODO(8E)` block in
    `src/lib/api/chat-types.ts` (step-8D.md §1);
  - add the `list_jobs` fake plan (step-8D.md §2; §3 above shows it works as described);
  - optionally type run outputs, event data, `CompanyAddOutput.company`, an output-mode
    `Resume` and 4xx error bodies (step-8C.md §6);
  - optionally re-export the page shapes from `lib/api/types.ts` (step-8B.md §4).

  8E's Done re-runs the pane items. §6 here shows they already pass on fakes, the `list_jobs`
  card included with the temporary plan.
- **8B's note for pane checks.** §5.2's 4xx refusals log "Failed to load resource" console
  errors in Chrome by design (step-8B.md §6). None of the checks here provoked one.
- **Step 11.** Delete `toCompanyRow`, the legacy `Company` import and `BOARD_LABEL.none` with
  `src/app/actions/**` (step-8C.md §1).
- **Step 9's compose change.** The next `pnpm db:up` (project `foray`) recreates `foray-db-1` on
  the same `foray_db-data` volume, because `POSTGRES_DB` and the healthcheck changed
  (step-9.md §6). The integration did not run `db:up`. `docker compose -p foray config --quiet`
  and `--profile prod config --quiet` both pass. The merged dev config has `POSTGRES_DB:
  foray_app` and `pg_isready -U foray -d foray_app`.
- **Test databases for the orchestrator to drop:**
  - `test_foray_app_g4` (this integration);
  - the lanes' `test_foray_app_lane8b`, `_lane8bv`, `_lane8c`, `_lane8cv`, `_lane8d`,
    `_lane8dv`, `_lane9` and `_lane9v`.

  No `test_foray_legacy_lane9` exists.
- **§4's double refresh line.** Open for step 10 or 8E.

## 6. Checks run on the combined code

- **Setup.**
  - `git status` was clean.
  - `:3000`, `:8000` and `:8001` were free.
  - `uv sync`, `pnpm install --frozen-lockfile --prefer-offline` and `pnpm db:migrate` ran, with
    no migrations to apply.
- **pytest** (`FORAY_TEST_DB_NAME=test_foray_app_g4 --create-db`):
  - before §1: 1700 passed, 1 failed (the handoff);
  - after §1: 1701 passed, in 76 s.
- **Static.**
  - `ruff check` passed, and `ruff format --check` found 271 files already formatted.
  - pyright gave 0 errors.
  - `next typegen`, then `tsc --noEmit`: exit 0. `eslint` exit 0.
  - `pnpm build` exit 0 with 13 routes and no `BetterAuthError` (§2).
  - `pnpm api:types`, then `git diff --exit-code backend/openapi.json src/lib/api/schema.d.ts`:
    no drift.
- **8C, in the pane** (U1, fresh, dark):
  - **No resume.** Profile showed "Start with your resume", the dropzone ("Drop your resume
    here, or choose a file") and the Preferences form, with five slots "Needed to search".
  - **Preferences save.** The form was filled by typing and clicking: roles, Mid level,
    location, Remote, work authorisation. The SaveBar read "Unsaved changes to your
    preferences." "Save preferences" toasted "Preferences saved". The SaveBar was gone (no
    Save button left), and the section description switched to "Every search, score and deep
    review starts from these answers…".
  - **Upload.** The dropzone read "Reading two-column.pdf…" with a compact card "Profile import
    / Starting… / Stop". The observer then recorded exactly one toast: "Read 2 roles, 1 schools
    and 0 skills." with "Check the details on your profile…". The page refreshed to "Version 1".
    Four more polls added no toast.
  - **Resume editor.** Typing a headline raised "Unsaved changes to your resume.". "Save
    changes" toasted "Profile saved", and the section then read "Version 2, saved just now". The
    database has base versions 1 and 2.
  - **Settings.** Five ArrowLefts on the deep-review slider gave "Deep review the best 10
    matches" and "Unsaved changes to how Foray works.". "Save settings" toasted "Settings
    saved", and the SaveBar was gone. Stored: `deepEvalTopN: 10`.
  - **Worker → Test.** "Answered by process 68971 in under a second." 68971 is the pid the
    worker logged as `ready (pid 68971)`.
  - **Worker stopped normally.** SIGINT went to its watchfiles process at 17:57:14.281. The log
    showed `stopping (running jobs get 5.0s)`, then `stopped` at 17:57:14.337. There were 0
    `procrastinate_workers` rows. By 17:57:20 the panel read "Offline / The worker is not
    running. Start it with pnpm dev:worker.", and the sidebar read "Worker offline".
  - **Add company.** `linear.app` showed the "Company discovery / Looking for job boards / Stop"
    card. Exactly one toast followed: "Linear was already on the list. Its Ashby board has 31
    open roles.".
  - **Refresh a row.** Linear → "Fetch now". The card row under Linear went "Board refresh /
    Refreshing job boards 0s / Stop", then "…1s", then "Linear: 31 open roles, no changes.".
    Exactly one toast carried that sentence. An earlier attempt, made without the card
    observer, also gave exactly one toast.
  - **Refresh stale boards.** The start toast "Refreshing 22 stale boards in the background…"
    appeared 50 ms after the click. 21.8 s later the outcome "Refreshed 22/22 boards — 4876
    postings, 28 closed" replaced it in place, so there was one toast.
- **8B, in the pane** (U1):
  - **Approve advances the run.**
    1. Home's "Run a search" toasted "Search started…". Gate 1 came 62 s later: "Deep-evaluate
       10 roles (under a cent)", with the approval toast.
    2. "Review 10 roles" (the `ApprovalCard`, the real decision endpoint, a live worker) at once
       read "Reading 0 of 10 postings in full". Gate 2 followed: "Shortlist 4 roles".
    3. "Shortlist 4" completed the run. The "Search started" toast was replaced in place by
       "Found 600 roles, scored 600, deep-evaluated 10. 4 look worth applying to; 4
       shortlisted.".
    4. Both approvals are `approved` with `decided_via = ui`. The stages were found 453,
       shortlisted 4 and archived 143.
  - **Render.**
    - Matches board: "457 open matches, 4 shortlisted and 0 applied".
    - List view: 457 rows.
    - Match detail: title "New Grad Software Engineer, Product Engineering at Nuro – Foray",
      with the fake review and the posting.
    - Activity: the runs table and the event log.
  - **Drag and drop.** A real pointer drag (`left_click_drag`) of the Coinbase card to Applied
    toasted "Moved to Applied … Undo", and the header read "1 applied". After a full navigation
    the card was still in Applied. The database has stage `applied` at 17:52:12.
  - **Stop.**
    1. A third search was started with "Search again" (target roles "Manager", worker with
       `LLM_FAKE_DELAY_MS=500`).
    2. On Home's "In progress" card, "Stop" was pressed while the run was in phase `scoring`
       ("Scored 160 of 594").
    3. The toast "Stopped. Everything found so far was kept." appeared, and the card left "In
       progress".
    4. The run is `cancelled`, and `run.cancelled` is its last event.
- **8D, in the pane** (U1):
  - **Streaming and the address.** "What's on my resume?" from `/chat`: the main element
    mutated 7 times over 3.8 s while the path changed from `/chat` to
    `/chat/a5ab80ee-…`. The `POST /api/chat` response was the SSE stream: `start`, the
    `get_resume` tool chunks, `text-delta`, `finish`, `[DONE]`.
  - **`users.timezone`.** It was `America/Los_Angeles` after the first turn. The default is
    `UTC` (`ENV.app_timezone`), and the pane's zone is `America/Los_Angeles`. To prove the turn
    sets it, the column was set back to `UTC` by SQL. The next turn set it to
    `America/Los_Angeles` again.
  - **`list_jobs` card** (temporary plan, §3). "Show me my matches" rendered 10 match lines with
    `/matches/<id>` links, stages (Shortlisted, Applied, Found) and scores, then "Here are your
    matches.".
  - **Follow-up divider.** "add linear.app" rendered the "Company discovery" card and "I am
    looking for a job board…". Within the polling loop a `role="note"` divider "Discovery
    finished" appeared, followed by the follow-up's card and "Foray checked the run: it is
    completed…". The same document was still loaded (a window marker survived), so there was
    no reload.
  - **Recent chats.** The sidebar listed "What's on my resume?". Options → "Delete chat" sent
    `DELETE /api/conversations/<id>`, which answered 200 `{"ok":true}`. The list emptied, the
    pane moved to `/chat`, and 0 conversations remained.
- **Visual (U2).** The no-resume profile and a chat with two "Company discovery" run cards and
  the divider were checked in dark, then light (`foray-theme=light`, reload; the key was removed
  afterwards; it was unset before). Both themes rendered cleanly.
- **Console.** `read_console_messages` (errors only, and a pattern search for `rror|arn|Failed`)
  found nothing across every page above. Only React DevTools, HMR and Fast Refresh logs
  appeared.
- **Server logs.**
  - The api answered no 4xx or 5xx except 4 `401`s on `/api/me` from signed-out moments.
  - The worker logs hold no warning or error.
  - The web log holds only §4's three aborted RSC streams.
- **Money.** $0. The ledger gained 1206 rows, all `fake`, at $0. Real spend since the start was
  $0.000000. JSearch made no request.
- **Afterwards.**
  - `create_test_user --purge` printed "Deleted 2 test users.".
  - `foray_app` has 0 users, 0 sessions, 0 runs, 0 conversations, 0 `todo`/`doing` jobs and
    0 worker rows.
  - The temporary fake plan was reverted and `public/g4-two-column.pdf` deleted.
  - Every server and the worker were stopped with SIGINT. The worker logged `stopped` both
    times, uvicorn logged "Finished server process", and `next dev` exited.
  - `:3000`, `:8000` and `:8001` are free.
- **Legacy `foray`.**
  - The write counters were 41158/1014040/5351 before and after.
  - `git --no-optional-locks -C <main checkout> status --porcelain` was exactly `?? reports/` and
    `?? research_notes/` before and after.
