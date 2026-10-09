# Step H1: open handoffs sweep — deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step H1 closes handoffs that earlier lanes left in files that no later step owns, plus small fixes
the verifiers found. Each item has a test that fails without its fix. Every fix was reverted to
`HEAD` in turn and its tests were run (§11).

## 1. The worker always finishes its shutdown (G3 integration verifier)

- **What.**
  - `apps/runs/management/commands/worker.py` closes the PDF browser with
    `asyncio.wait_for(_close_browser(), BROWSER_CLOSE_TIMEOUT_S)` (10 s). On a timeout it logs
    "the PDF browser did not close within 10.0s; exiting without it" and carries on, so `stopped`
    is always logged.
  - `apps/resumes/render.py` `_Chromium._close_unlocked` runs `browser.close()` and then
    `playwright.stop()`, each inside `asyncio.timeout(CLOSE_TIMEOUT_S)` (4 s). A step that times
    out is logged as a warning. A step that fails is logged at debug level, as before. `stop()`
    still runs after a stuck `close()`: it ends the driver process.
  - **Addition: the stop handlers stay installed until the browser is closed.** `serve()` used
    to restore Python's default SIGINT handler first and close the browser afterwards. It now
    closes the browser first.
- **Why the addition.** The design names only the bounds. The live check showed that the bounds
  alone do not get `stopped` logged under `pnpm dev:worker`.
  - **The hang.** Before the fix, the hang was reproduced with SIGINT sent to the whole process
    group, as a terminal's Ctrl-C does.
    - In round 2 the lanes stopped and unregistered (0 `procrastinate_workers` rows), but the
      worker never logged `stopped` and never exited.
    - Chromium was gone, the Playwright driver was still alive, and the driver's stdin was still
      open: `browser.close()` never answered.
  - **The cause.** A debug run (temporary logging, since removed) showed the sequence:
    1. at `.028`, `stopping`;
    2. at `.036`, the handlers were removed;
    3. at `.037`, the close started, with `default_int_handler` installed;
    4. at `.052`, watchfiles caught *its* KeyboardInterrupt and sent its own SIGINT to the worker
       (`CombinedProcess.stop`).

    That second SIGINT interrupted the Playwright close mid-way.
  - **With the bounds alone.** The close was then cancelled four seconds later and ended in
    `KeyboardInterrupt`, in 3 of 3 rounds, with no `stopped`.
  - **With the handlers kept.** The second SIGINT only sets the stop event again, and the close
    finishes in milliseconds.
- **The bound values.**
  - The design gives the worker's bound: 10 s.
  - The render bound is not given. 4 s per step means both steps fit inside the worker's 10 s,
    and a healthy close takes about 10 ms.
  - `pnpm dev:worker`'s 5 s graceful window plus 10 s fits watchfiles' `--sigint-timeout 15`.
- **Observed, not changed: watchfiles does not always enforce its 15 s window.** Under a
  group-wide SIGINT, watchfiles' own `join()` is interrupted by a second KeyboardInterrupt, and
  it exits without waiting or sending SIGKILL. A hung worker is then orphaned. The worker's own
  bound is therefore the only guarantee that it exits (dev only; the prod compose sends SIGTERM to
  the worker directly).
- **Evidence.**
  - `tests/runs/test_worker_shutdown.py` has three tests:
    - a close that never returns: `serve()` finishes, with the warning and then `stopped`;
    - a SIGINT sent to the process during the close: no `KeyboardInterrupt`, `stopped` last, and
      the default handler restored afterwards;
    - the bounds fit the dev window.
  - `tests/resumes/test_render.py::test_closing_gives_up_on_a_browser_that_never_answers`
    covers a hung `close()` and a hung `stop()`; both are bounded and the driver is still stopped.
  - **Mutations.**
    - With the bounds removed, the first test hit its 30 s outer bound and the render test its
      5 s bound.
    - With the handler removal moved back before the close, the second test failed with
      "a second SIGINT interrupted the shutdown". The pytest session survived.
  - **The live Done check.** It ran with `LLM_FAKE=1`, and with `JSEARCH_ENABLED=false` in the
    process environment. The harness is a scratch script.
    - Each round:
      1. start `pnpm dev:worker` in its own session;
      2. create a `create_test_user --with-resume` user;
      3. `pipeline.render_base_resume` for that user (the render run completed with 1 page, and
         two Chromium processes were up);
      4. send SIGINT;
      5. time how long until `stopped` is logged.
    - SIGINT to the whole process group, 3 rounds: `stopped` after **0.06 s, 0.07 s and
      0.06 s**.
    - SIGINT to the watchfiles processes only (integration G2's method), 3 rounds: **0.13 s,
      0.12 s and 0.07 s**.
    - `manage.py worker` run directly: 0.08 s.
    - No worker, driver or watchfiles process was left. `create_test_user --purge` printed
      "Deleted 3 test users." after each set of rounds. `foray_app` ended with 0 users, 0
      sessions and 0 worker rows.

## 2. A body ninja cannot parse gets the operation's sentence (step-4.md §6)

- **What.**
  - `apps/core/errors.py` `on_http_error` answers any ninja `HttpError` with status 400 with
    the running operation's `invalid` message. ninja raises only one: "Cannot parse request
    body", with the parser's own error appended under DEBUG.
  - An operation with no `@messages` answers `DEFAULT_INVALID`.
  - A callable `invalid` is called with an empty error list and answers its generic sentence:
    - `PUT /api/preferences`: "Those preferences could not be saved. Check the fields and try
      again.";
    - `PATCH /api/matches/{id}`: "That change could not be read.";
    - sign-up and sign-in: the default sentence.
  - The shared `_invalid_message()` now also serves the validation handler.
  - Other `HttpError`s still pass their message through. ninja raises none of them here: CSRF is
    off and nothing uses ninja throttles.
- **Evidence.** `tests/core/test_errors.py`, run with `DEBUG` false and true, sends `this is {not
  json` to four endpoints:
  - `PUT /api/preferences`;
  - `POST /api/searches` (no `@messages`);
  - `POST /api/approvals/{id}/decision`;
  - `PATCH /api/matches/{id}`.

  Each answers 400 with the operation's sentence, and no response contains "parse". A readable
  body still gets its field-level sentence.

## 3. `js_num` and `js_locale` match JavaScript (step-3B.md §9)

- **What.**
  - **`js_num`** implements ECMAScript's Number::toString. It takes the shortest round-trip
    digits `s` and the exponent `n` from `repr`, then prints the integer form for
    `k ≤ n ≤ 21`, a point inside the digits for `0 < n ≤ 21`, `0.000…` for `-6 < n ≤ 0`, and
    otherwise `d.ddde±x`.
  - **`js_locale`** quantizes inside `localcontext(prec=400)`, so the largest double no longer
    raises `InvalidOperation`. It keeps the `-` whenever the value is negative, even when it
    rounds to zero (ICU prints `-0`).
  - The 10 strict xfails in `tests/llm/test_jsonio.py` are now ordinary tests, and their markers
    and the `HANDOFF` constant are gone.
- **Evidence.**
  - `tests/llm/test_jsonio.py`: 90 passed.
  - A scratch fuzz compared both functions with Node 26's `String(x)` and
    `x.toLocaleString("en-US")` on 68,938 doubles. The doubles were:
    - 20,000 random bit patterns;
    - every power of ten from 1e-330 to 1e308 with 7 mantissas, both signs;
    - 40,000 values from the salary and fraction ranges;
    - the edge cases.

    There were 0 mismatches.
  - Every prompt golden still passes in the full suite.

## 4. The purge flake: the G2 fix is on the branch, and step-4.md §9 is corrected

- **What.**
  - `test_create_test_user_purge` compares `sorted(...)` with `sorted([keep.pk,
    "unrelated.txt"])`. Integration G2 made that change in commit `7452f7a`.
  - `docs/migration/deviations/step-4.md` §9 had given the cause as "an order-dependent count of
    `@example.test` users". It now gives the real one: a sorted listing was compared with an
    unsorted list, so the test failed whenever the random id sorted after `unrelated.txt`.
- **Evidence.**
  - 30 separate runs of the test: 30 passed.
  - With a scratch pytest plugin (since deleted) that makes every new user id start with `z`, the
    current test passes.
  - Under the same plugin, the pre-G2 version (`93f4055`) fails, with `['unrelated.t…'] ==
    ['zxDrnk41nFz…']`. So the cause is the one stated now.

## 5. `create_test_user --purge` deletes the purged users' sessions

- **What.** `purge_test_users()` decodes every `django_session` row
  (`session.get_decoded().get(SESSION_KEY)`) and deletes the purged users' sessions in the same
  transaction as the users.
  - A session of another user is kept.
  - So is an anonymous session.
  - So is a session that does not decode (another `SECRET_KEY`, or corrupt data). Django's
    `get_decoded()` returns `{}` for those.
- **Why.** A session names its user only inside its encoded data, so nothing cascades to it.
  Integrations G1, G2 and G3 each deleted the orphans by hand.
- **Evidence.** `tests/accounts/test_commands.py::test_create_test_user_purge_deletes_their_sessions`
  sets up five sessions:
  - two for the purged user;
  - one for a kept user;
  - one anonymous;
  - one corrupt.

  After the purge exactly the last three remain.

## 6. One resolve-match implementation

- **What.**
  - `apps.resumes.pipeline.resolve_match` is deleted.
  - `start_tailor_run` calls `apps.matching.queries.resolve_or_create_match(user_id, wanted)` on
    the trimmed id, looked up as a module attribute at call time.
  - `apps.matching.queries` keeps `resolve_match`. It is the lookup only, with no create; it is
    the building block of `resolve_or_create_match` and what step 6's `get_job` and
    `move_job_stage` use (step-5A.md §8).
  - `grep -rn "def resolve_match\|def resolve_or_create_match" backend/apps` finds exactly those
    two, both in `apps/matching/queries.py`.
- **Differences, none of which change behaviour.**
  - The matching version creates the match with `upsert_matches`, which is race-free (`ON
    CONFLICT DO NOTHING`).
  - It does not `select_related("job")`, so the tailor's label costs one extra query.
- **Evidence.**
  - `tests/resumes/test_pipeline.py::test_tailoring_resolves_ids_through_the_one_match_resolver`:
    a spy on the matching function sees both a match id and a trimmed job id, the job id creates a
    `found` match, and `pipeline` has no `resolve_match`.
  - The new `tests/matching/test_queries.py` covers:
    - a match id or a job id finds the match, and its stage is kept;
    - a posting without a match gets a `found` match once;
    - another user's match id, an unknown UUID, an unknown job id and garbage all resolve to
      None.
  - `tests/resumes` and `tests/matching` pass.

## 7. A pasted board link no longer renames a company or sets the ATS host as its domain (step-5C.md §7)

- **What.** In `apps/jobs/discovery.py`, when the input contains an ATS board link:
  - `domain` is set to None, so the ATS host is never the company's domain and is never probed
    for `/careers`.
  - `name` is the link's slug, marked as a guess. The company is registered as
    `title_case(slug with - and _ as spaces)`, so `acme_corp` becomes "Acme Corp" and `linear`
    becomes "Linear".
  - When the registration would update an existing company, that company's name is kept. The
    existing company is found by the upsert's own identity: `(source, ats_slug)` for a board,
    the careers-page slug otherwise.
  - The existing domain is kept too, because the upsert coalesces a None domain.
  - A dead board link falls through to the slug guesses and the search, and its trail is
    `[ats-link, slug-guess …]`. That trail finds a company that moved to another ATS.
- **Evidence.**
  - `tests/jobs/test_discovery.py`:
    - `::test_a_pasted_board_link_is_named_after_its_slug_with_no_domain`;
    - `::test_a_pasted_board_link_keeps_the_name_and_domain_a_company_has` ("Linear Orbit" and
      `linear.app` are kept);
    - `::test_a_dead_board_link_never_probes_the_ats_host_as_a_company_site` (no request to
      `https://boards.greenhouse.io/…`).
  - `tests/jobs/test_pipeline.py::test_a_pasted_board_link_is_verified_and_filled` now asserts
    the name "Linear", domain None and "Added Linear. Its Ashby board has …".

## 8. `OkMessageOut` is defined once, and a test guards component names

- **What.**
  - `OkMessageOut` lives in the new `apps/core/schemas.py`. Its shape is unchanged and it has no
    class docstring, so no `description` reaches the schema.
  - `apps.accounts.api`, `apps.matching.api` and `apps.runs.api` import it.
  - `tests/core/test_openapi_names.py` has two checks:
    1. **One schema per name.** It records every definition ninja hands to
       `OpenAPISchema.add_schema_definitions` while the real schema is built, plus the
       registry's, and fails if one name ever gets two different schemas.
    2. **One type per name.** It walks every operation's parameter and response annotations, and
       every registered type, recording the types pydantic names: models, enums, dataclasses,
       TypedDicts and `type` aliases. It fails if one name comes from two Python types, even
       identical ones, because one copy could drift later.
  - A probe API with two classes named `Clash` shows that both checks fail on a clash, and that
    ninja keeps only the last schema.
- **Evidence.**
  - The walk finds 65 names.
  - Ten names are produced both by operations and by the registry (`RunKind`, `LiveSnapshot`, …),
    and their definitions are identical.
  - Without the fix, check 2 fails on `OkMessageOut`; check 1 passes, because the three copies
    were equal (integration-G3.md §1).

## 9. The search reads `MULTI_BOARD_GATE` at call time

- **What.**
  - `apps/matching/search.py` imports `apps.jobs.refresh as refresh_module` and holds
    `refresh_module.MULTI_BOARD_GATE`, as `jobs.pipeline` does (step-5C.md §4).
  - `tests/matching/test_search_pipeline.py` gives every test a fresh gate with an autouse
    fixture, as 5C's tests do.
  - The gate tests read the gate from the module.
- **Evidence.**
  `::test_an_unlimited_search_refreshes_the_stale_set_under_the_gate` asserts that the
  test's own gate is held. With the import-time binding the test fails, because the search holds
  the original lock instead.

## 10. A tailored PDF downloads under its own name even when its HTML equals the base's

- **What.**
  - For a tailored resume, `render_hash` now also covers the download name:
    sha256(html_normal | RENDER_VERSION | "<Name> - Resume - <Company>.pdf"). A base resume's
    key is unchanged: sha256(html_normal | RENDER_VERSION), as in §11.6.
  - A tailored resume whose HTML equals the base's (tailoring changed nothing) therefore no
    longer reuses the base's "… - Resume.pdf" document. It renders its own and stores it as
    "… - Resume - <Company>.pdf".
  - `render_hash(resume, company=None)` takes the company. `render_resume` builds the key from
    the filename it stores.
- **Why not the suggested "own row pointing at the same stored bytes".** `documents` has two
  unique constraints, `(user, sha256)` (`documents_user_sha256`) and `(user, render_hash)`
  (`documents_user_render_hash`). A second row with the same bytes or the same key is
  impossible without a migration. A migration would change step 2's model, and only a serial
  step that depends on a lane adds one. Keying the tailored render by its name needs no schema
  change. It costs one extra render (no model call), and only when tailoring changed nothing.
- **The edge case.** Chromium's PDFs of one HTML are byte-identical within the same second; a
  scratch render of one page three times in 0.3 s gave three equal sha256 values.
  - So a tailored render in the same second as an identical base render would hit
    `documents_user_sha256`.
  - `_record` then uses that byte-identical document: the earlier behaviour, rather than a
    failed render.
  - The next render of the tailored resume gets its own document.
- **Evidence.** `tests/resumes/test_render.py`:
  - `::test_render_hash_is_the_normal_html_and_the_version` pins the tailored formula.
  - `::test_a_tailored_pdf_identical_to_the_base_still_downloads_under_its_own_name` (faked
    `render_fitted`, no browser) checks:
    - two documents, "Avery Quinn - Resume.pdf" and "Avery Quinn - Resume - Stripe.pdf";
    - each resume points at its own document;
    - each is reused by its own key afterwards, with exactly two renders.
  - `::test_a_tailored_pdf_byte_identical_to_the_base_reuses_that_document` checks one document,
    one stored file, and the tailored resume pointing at it.
  - **Mutations.**
    - With the key reverted, the identical-HTML test fails (the same document id).
    - With the byte fallback removed, the byte-identical test fails with `IntegrityError …
      documents_user_sha256`.
- **Handoffs.** These are the texts that state the old formula. Neither step owns them.
  - The comment on `Document.render_hash` in `apps/resumes/models.py` (step 2's file) still reads
    "sha256(html_normal | RENDER_VERSION)".
  - Design §11.6 states the same formula. Step 11 brings DESIGN.md up to date, and this entry
    overrides the design.

## 11. Checks run in this step

- **pytest** (`FORAY_TEST_DB_NAME=test_foray_app_h1`, created with `--create-db`):
  - **before the step:** 1522 passed, 6 skipped and 10 xfailed;
  - **after the step:** 1552 passed and 6 skipped, in 65 s. The 10 former xfails pass, 20
    tests are new, and the skips are the same six (R7, R8, R13, R17 pending;
    `apps/chat/tools.py`; the profile page).
  - The render tests ran on local Chrome.
- **Static checks.** `ruff check` and `ruff format --check` are clean, pyright reports 0 errors,
  and `tsc --noEmit` and `eslint` exit 0. `next typegen` was not needed.
- **Mutation check** (scratch script). Each fix's files were restored from `HEAD` and their tests
  run, then the fix was put back. Every item's tests failed without its fix:
  - 1 (3 failed; the targeted mutations above);
  - 2 (2 failed);
  - 3 (10 failed);
  - 5, 6 and 9 (1 failed each);
  - 7 (4 failed);
  - 8 (1 failed);
  - 10 (2 failed).
- **Money.** No real model was called ($0). JSearch was off in every live process.
- **Legacy `foray`.** The write counters stayed at 41158/1014040/5351.
- **Processes.** No server or worker is left running, and :3000, :8000 and :8001 are free.
