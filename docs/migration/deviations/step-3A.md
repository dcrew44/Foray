# Step 3A: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. `Preferences` is a snake_case OpenAPI component; `test_wire_format` needs its allowlist entry

- **What.** `PUT /api/preferences` takes the stored `accounts.schemas.Preferences` as its body, so
  `openapi.json` has a `Preferences` component with snake_case properties (`target_roles`, …).
  `tests/core/test_wire_format.py::test_camel_case_response_and_schema` fails on it until
  `SNAKE_CASE_COMPONENTS` gains `"Preferences": "§2.1: preferences keep today's snake_case keys"`.
- **Why.** §2.1 keeps preference keys snake_case so neither the frontend nor the import maps keys,
  and the test's allowlist exists for exactly this ("preferences and evaluations keep today's
  spelling"). The test file is step 1's, so this lane does not edit it (handoff).
- **Evidence.** Full suite in this lane: 179 passed, 18 skipped, 1 failed (that test, on
  `Preferences.target_roles`). With a scratch pytest plugin that adds the one entry at collection
  time, `tests/core/test_wire_format.py` passes 7/7 (including the camelCase GET sweep over
  `/api/me` and `/api/auth/config`).

## 2. The sliding-session stamp: what "stale" means, and `dev_session`

- **What.** `foray.middleware.sliding_session` keeps `_seen` (epoch seconds) in the session. A
  missing or non-numeric stamp counts as stale. Sign-in and sign-up stamp the new session
  (`stamp_session`), so a fresh login does not slide again for a day. `dev_session` does **not**
  stamp: its session slides on the first browser request, which hands the browser a real
  `HttpOnly` cookie with `Max-Age=604800`.
- **Why.** §4.1 names the stamp but not its initial value. Leaving the dev session unstamped also
  makes the deferred Done check direct: with a fresh `pnpm dev:session` cookie, a request with
  `X-Foray-SSR: 1` gets no `Set-Cookie`, and the same request without the header gets one.
- **Also.** Only requests that carry the session cookie are considered, and the check runs after
  the view, so a sign-out in the same request (which flushes the session) never creates a new
  stamped session. Under uvicorn the whole middleware chain runs async (every middleware in the
  list is async-capable), so the async path uses Django's async session API (`aget`, `ahas_key`,
  `aset`) and `request.auser()`; it looks the user up only when the stamp is stale.
- **Evidence.** `tests/accounts/test_auth.py::test_sliding_session_ignores_ssr`,
  `::test_sliding_session_rules`, `::test_sliding_session_async_path` (through `AsyncClient`, i.e.
  `ASGIHandler`), `::test_dev_session_output_shape`.

## 3. `login()` cycles an anonymous session; a same-user re-sign-in keeps its key

- **What.** §4.4's "`login()` rotates the key" holds for the session-fixation case: any session
  that existed before sign-in (anonymous, or another user's) is replaced. Django keeps the key when
  the same user signs in again over a valid session of their own.
- **Evidence.** `test_auth.py::test_sign_in_rotates_the_session_key` plants an anonymous session;
  after sign-in its key is gone and the signed-in key differs.

## 4. Throttle helpers beyond `throttle()`

- **What.** `apps/core/throttle.py` has `throttle(key, limit, window_s)` as §4.5 specifies, plus:
  `check(key, limit, window_s)` (read-only; 429 when the count has reached the limit; the sign-in
  key uses it before the password), `current()`, `client_key(request)`, `prune(older_than=1 day)`
  for `maintenance.prune`, and `Limit` constants for the §4.5 table (`SIGNIN_FAILURES`, `SIGNUP`,
  `CHAT`, `UPLOAD`, `COMPANY_ADD`, `EVALUATE`, `TAILOR`). The window is passed as the string
  `'<n> seconds'` and cast with `::interval`, so the SQL is §4.5's verbatim (django-types does not
  accept a `timedelta` parameter).
- **Order in sign-up.** Closed (403) → email and password checks (400) → `signup:<client_key>`
  counted → duplicate check (409) → create. Malformed attempts are not counted; duplicates are,
  because the duplicate check answers "does this email have an account?".
- **`client_key`.** With `TRUSTED_PROXY_HOPS = n ≥ 1` and fewer than `n` `X-Forwarded-For`
  entries, the key is `"local"`: such a header cannot have come through the trusted proxies.
- **Evidence.** `test_auth.py::test_sign_in_throttle` (10 failures → 400, the 11th → 429, the
  right password → 429 with `authenticate` never called, successes never counted, another email
  unaffected), `::test_sign_up_throttle`, `::test_throttle_counts_per_window`, `::test_client_key`.

## 5. Validation sentences the design left open

- **Sign-up and sign-in bodies.** A missing or non-string `email` answers "Invalid email" (as an
  invalid address does); any other unreadable body answers §5.2's default "That request could not
  be read.". Emails are checked with zod 4.6's `z.email()` regex (`fullmatch`, ASCII) and at most
  320 characters. Password lengths are JavaScript's (UTF-16 code units), as Better Auth measured
  them: 64 emoji is 128 and allowed. A sign-up `name` that is missing or blank becomes the
  email's local part; a longer one than 200 characters is cut to fit `users.name`.
- **`PUT /api/preferences`.** `"<Slot label> could not be saved: <problem>."` for the first failing
  field, where `<problem>` is ours, never pydantic's: "it should be a list", "it should be text",
  "that is not one of the options", "the amount should be a number", "the <field> is missing",
  else "the value is not valid" (today's fallback). A body that is not an object answers "Those
  preferences could not be saved. Check the fields and try again.".
- **`PATCH /api/settings`.** Partial and strict as today: unknown keys, nulls, strings, booleans
  and fractions all answer "Those settings are out of range. Check the values and try again."
  (zod's `z.number().int()`); camelCase and snake_case keys are both accepted (§5.2).
- **Evidence.** `test_auth.py::test_sign_up_validation`, `::test_wrong_credentials_are_400_not_401`;
  `test_interview.py::test_put_preferences_validation_messages`,
  `::test_patch_settings_rejects_out_of_range_and_unknown`.

## 6. Settings: stored whole, answered with every field

- **What.** `update_settings` merges the sent keys into the stored settings (defaults applied) and
  stores all three fields, as `updateSettings` did. The response's `settings` is `SettingsOut`
  (all three fields required), so the generated TypeScript type is not all-optional;
  `accounts.schemas.Settings` (fields with defaults) stays the column's reader.
- **Concurrency.** `save_preferences` and `update_settings` lock the user row
  (`select_for_update`) for their read-modify-write, so a concurrent chat-tool update of
  `interview_state` or a second settings save is not lost.

## 7. Commands print the TypeScript scripts' messages verbatim

- **What.** `dev_session`, `create_test_user` and `set_password` write a refusal to stderr as is and
  exit 1 (`apps.accounts.management.fail`), instead of raising `CommandError`, whose output is
  prefixed "CommandError: ". `dev_session` refuses with "dev_session is for local development
  only."; `set_password` keeps today's five messages ("No user with email X.", "Run this in a
  terminal: the password is typed at a hidden prompt.", "Passwords must be 8 to 128 characters.",
  "Those did not match. Nothing was changed.", "Cancelled.") and "Password updated. Sign in at
  <PUBLIC_ORIGIN>/sign-in". Any user can be given a password now, so today's "<email> does not sign
  in with a password." has no case.
- **`create_test_user`.** The user is named "Test User". `--with-match` also prints
  `match: <uuid>` (5B's checks need the id). "Newest active job" is the prefilter index's order:
  `posted_at DESC NULLS LAST`, then `first_seen_at DESC`. An `--email` that exists, `--purge` with
  another option, and `--with-match` on an empty mirror each refuse before anything is written.
  `--purge` prints `Deleted N test users.` (`user` for one) and removes only `STORAGE_DIR/<id>/`
  folders that are direct children named after a deleted user.
- **Evidence.** `tests/accounts/test_commands.py` (6 tests).

## 8. For step 3F: `encode_imported`

- **What.** `apps.accounts.hashers.encode_imported("<salt>:<key>")` returns
  `ba_scrypt$<salt>$<key>` (171 characters) and raises `ValueError` for anything that is not a
  Better Auth scrypt hash (32 + 128 lowercase hex). The hasher's `verify` returns False for a
  malformed value instead of raising, and replaces a lone UTF-16 surrogate with U+FFFD before
  encoding, as Node does.
- **Evidence.** `test_auth.py::test_golden_hashes_verify` (both goldens through the hasher and
  through Django's `check_password`), `::test_first_sign_in_upgrades_to_pbkdf2` (the column
  becomes `pbkdf2_sha256$…`; a wrong password first leaves the imported hash untouched).

## 9. The profile page's `slots` test waits for the pages step

- **What.** `test_interview.py::test_profile_page_serves_slots` skips while
  `GET /api/pages/profile` answers 404, and asserts `slots == [s.model_dump(by_alias=True) for s
  in SLOTS]` once it exists. The SLOTS themselves are pinned byte for byte against the TypeScript
  through the `chat_system_prompt/interview_empty.json` golden (labels, intents, required).
