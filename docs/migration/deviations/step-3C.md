# Step 3C: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. The ATS JSON response cap is 64 MB, not 32 MB

- **What.** `apps.core.http.ATS_MAX_BYTES = 64 MB`. JSearch (5 MB) and HTML (3 MB) are as
  designed.
- **Why.** A real seed board is larger than 32 MB: Anduril's Greenhouse board
  (`andurilindustries`, 2,466 postings with `content=true`) is 43 MB. Under the design's cap it
  could never refresh, which is a regression (the TS client had no cap). 64 MB keeps the cap's
  intent (abort runaway responses) with headroom.
- **Evidence.** `curl -s -o /dev/null -w '%{size_download}'
  'https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs?content=true'` →
  `43035285`. The first `pnpm seed:companies --fetch` logged `refresh of andurilindustries
  failed: The response ... is too large.`; after the change the next run inserted its 2,466
  postings.

## 2. An errored fetch keeps `last_fetch_job_count`

- **What.** `registry.record_fetch(company_id, status, job_count=None)` leaves the count as it
  was. Board and careers-page refreshes pass `None` on `error`; `ok`, `needs-browser` and
  `not-found` (which also marks the company invalid) write a number as before.
- **Why.** TS wrote 0 on every failure. The empty-board guard (§10.4) reads this count: after an
  error had written 0, a single flaky empty answer would close every posting, which is what the
  guard exists to prevent ("only a second consecutive empty fetch deactivates"; an error is not
  an empty fetch).
- **Evidence.** `tests/jobs/test_refresh.py::test_only_a_404_marks_a_company_invalid` (a 503
  keeps 9), `tests/jobs/test_registry.py::test_record_fetch_keeps_the_count_when_none`.

## 3. The HTTP client's shape (clarification of §10.1)

- `HttpClient(transport=None, resolve=None)` wraps one `httpx2.AsyncClient`; use one per job
  (`async with HttpClient() as client`). `fetch_text`/`fetch_json` are its methods with the
  §10.1 keywords (`timeout`, `retries`, `accept`, `headers`, `max_bytes`); `max_bytes` defaults
  to the HTML cap when `accept` is HTML, else the ATS cap. Adapters take the client:
  `fetch_board(client, slug)`.
- Errors: `FetchError` is the base. `HttpError(status)` carries an int status or one of
  `"blocked"`, `"too-large"`, `"content-type"`, `"redirects"`, `"invalid"`;
  `str(HttpError("blocked"))` is "That address is not reachable from Foray.". Timeouts,
  connection and DNS failures become `NetworkError` after the retries. Only 408, 429 and 5xx
  (and network errors) are retried.
- The content-type check refuses an HTML fetch whose `Content-Type` is present and is not
  `text/html` or `application/xhtml+xml`; a missing header is accepted.
- Bodies decode as UTF-8 whatever the charset says, BOM dropped, invalid bytes replaced (what
  Node's `response.text()` does). Environment proxies are ignored (`trust_env=False`), as Node's
  fetch ignores them. `localhost` and `*.localhost` are refused without resolving.
- Brave search now goes through this client with `retries=0`, so it sends Foray's User-Agent.

## 4. Careers pages: the cache write-back, and the crawl's signature

- `fetch_careers_page(client, *, name, slug, careers_urls, cache, reached, max_postings)` fills
  `reached` in place with the entries of every listing page it dequeued. A page that was
  reached but failed to load keeps its old entry (it was reached; only pages the crawl no longer
  reaches are dropped). On success the refresh writes back `reached`; when the crawl raises
  (say the model fails on page 2) it writes back `{**old, **reached}`, so an extraction already
  paid for is never lost, and the fetch is recorded as `error` (never `invalid`).
- `RefreshOutcome` gains `slug` and `model_calls` (0 when every listing page was a hit).
- The listing prompt wraps the page text in `<untrusted source="careers page">…</untrusted>` in
  place of `<page>…</page>` (the declared difference of §8.10); `tests/jobs/test_prompts.py`
  pins everything else byte for byte against both TS goldens.

## 5. The `extract` fake override and how it is registered (open contract with 3B)

- **What.** `apps/jobs/fakes.py` defines `careers_listing_reply(messages, info)`, a
  `FunctionModel` function that answers only a careers-listing request (recognised by
  `LISTING_INSTRUCTIONS` in `info.instructions`, or an output schema with `postings` and
  `more_listing_urls`) with the listing's own links as postings, and returns **None** for any
  other `extract` request. At import it calls `apps.llm.fake.register("extract",
  careers_listing_reply)` when that module and function exist (it is absent in this lane).
- **Why.** §8.9 names `fake.register("score", fn)` but not its contract, and both 3C and 3D
  register an `extract` override. See the handoff in the step report.
- **Evidence.** `tests/jobs/test_careers_page.py::test_the_fake_answers_through_a_real_output_tool`
  runs it through a real pydantic-ai `Agent` with `output_type=ExtractedListing`.

## 6. Registry details

- A careers-page company matches only careers-page rows (by domain, then by slug); a careers
  page never updates a board company, so one named like an existing board gets a `-2` slug.
- `upsert_company` returns `(company, created)`, and `DiscoveryResult` carries `created`, which
  company runs need for "already on the list". The name is overwritten on update, as in TS;
  `discovered_via` and `added_by` never change.
- Discovery records a refused address once per discovery as
  `{"step": "blocked", "detail": "That address is not reachable from Foray."}`.
- `seed_companies --fetch` refreshes the loaded companies that are `active` (TS refreshed every
  active company; the design says "the loaded companies only"). Its totals line adds
  `N unchanged`, and each careers-page company prints its model calls.

## 7. Smaller clarifications

- `src/lib/companies/seed.json` is copied to `backend/apps/jobs/seed.json` (byte-identical) and
  left in place: `src/lib/**` is not this step's to delete.
- Posting dates are truncated to milliseconds like a JS `Date`; a naive date-time is read as UTC
  (TS used the process's zone; no fixture or API sends one); RFC 2822 dates parse as JS parses
  them; anything else is `None` with a debug log.
- `content_hash` sorts locations by UTF-16 code units, exactly as JS `sort()` does. Lengths and
  slices are Python-native, as §10.3 says.
- NUL characters are not stripped (Postgres refuses them in text, as it did for TS); only lone
  surrogates are replaced, everywhere text or JSON is stored.
- `ingest_jobs(company_id, jobs, *, deactivate_missing=False)` takes the company's id;
  `search_jsearch(client, query, *, country, date_posted, remote_only, max_requests)`;
  `jsearch_budget()` returns `{used, budget, remaining, enabled}`.

## 8. How the seed `--fetch` Done check ran

- **What.** On the empty `foray_app`, the first `pnpm seed:companies --fetch` registered 225
  companies and inserted 24,727 postings (221 ok, 3 gone, Anduril errored on the cap of #1).
  After the cap change the mirror could not be emptied again: `DELETE FROM jobs; DELETE FROM
  companies` was refused by the session's permission classifier. So the second run inserted
  Anduril's 2,466 postings (and updated 1 posting that really changed between runs), and an
  immediate third run reported `27193 postings seen → 0 inserted, 0 updated, 27193 unchanged`.
- **Evidence.** The three runs' output is in the step report. `foray_app` holds 225 companies
  (3 invalid: amplitude, hextechnologies and hightouch answer 404 on Greenhouse) and 27,193
  active jobs. The legacy counters stayed at 41158/1014040/5351.
