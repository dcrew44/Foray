# Step 5B: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. The upload view reads its own body; the multipart body is documented by `openapi_extra`

- **What.** `POST /api/uploads` declares no ninja `File` parameter. The view counts
  `upload:<user>` first, then reads `request.FILES`. That access is what runs
  `MaxSizeUploadHandler`. The view then checks for a missing file and sniffs the bytes. The
  `multipart/form-data` body with its `file` field is added to the OpenAPI operation through
  `openapi_extra`.
- **Why.** §11.1 sets the order: throttle, then the 413, then the 400, then the 415. ninja parses
  a declared `File` before the view body runs, so with one an oversized upload would get its 413
  before the throttle counted it. The handler raises `UploadRejected(413, …)` inside Django's
  multipart parser. The view turns that into `ApiError`, which gives the envelope.
- **Evidence.** `tests/resumes/test_upload.py::test_upload_size_cap`, `::test_upload_throttle`
  and `::test_upload_without_a_file` pass. `::test_upload_through_asgi` runs the same checks
  through `AsyncClient` (an `ASGIRequest`). `export_openapi_schema` shows the `requestBody` on
  `/api/uploads`.

## 2. The 413 gives the file's real size

- **What.** Past 10 MB, `MaxSizeUploadHandler` stops handing chunks to the in-memory handler, so
  that handler never holds more than the cap. It keeps counting, and at the end of the file it
  refuses with Django's exact byte count: "That file is 12.3 MB; the limit is 10 MB.". The size
  is `(size / 1024 / 1024).toFixed(1)`, with halves rounding up: 10.25 MB prints `10.3`.
- **Why.** The sentence is today's. Under ASGI, Django has already spooled the whole body before
  the view runs, so counting to the end costs no extra network. Exactly 10 MB is allowed, as in
  TS (`size > MAX_BYTES`).
- **Evidence.** `::test_too_large_message_rounds_like_to_fixed` and `::test_upload_size_cap`
  (10 MB + 9 bytes gives "10.0", 12.3 MB gives "12.3", exactly 10 MB gives 202).

## 3. Sniffing: what is stored, and the import sniffs again

- **What.** `parse/sniff.py`:
  - `sniff(data)` returns `"pdf"` or `"docx"`, or raises `UploadRejected(415, …)`.
  - A PDF is anything starting with `%PDF-`.
  - A DOCX is a readable ZIP that holds both `[Content_Types].xml` and `word/document.xml`, with
    at most 2,000 entries and at most 50 MB declared uncompressed.
  - `documents.mime` stores the sniffed type (`application/pdf`, or the DOCX type), never the
    client's.

  The `profile_import` step sniffs the stored bytes again to pick the parser. A refusal at that
  point fails the run as "Could not read that resume: <sentence>".
- **Why.** §11.1 says "magic bytes, not the client's MIME". A reused document can be a
  `resume_pdf` or an imported row, so its bytes are the only reliable type.
- **Evidence.** `::test_sniff_reads_magic_bytes`, `::test_docx_guard_refuses_zip_bombs` (a 51 MB
  entry and 2,001 entries are refused; 2,000 entries and 49 MB pass) and
  `::test_upload_sniffs_bytes_not_names`.

## 4. A re-upload repairs a document whose stored file is missing (addition)

- **What.** On the reuse path (the same sha256 is already a document of this user),
  `accept_upload` checks `storage().exists(document.storage_path)`. If the file is gone, it
  writes the identical bytes under a new key. It then updates `storage_path` only if the row
  still has the old key; if another request got there first, it deletes its own copy. The
  import that follows reads the restored file.
- **Why.** §11.1 says "start_run only (reuse the document)". Taken literally, a document whose
  file had gone missing could never be read again: every re-upload would reuse it and fail. The
  conditional update keeps the "no orphan" rule.
- **Evidence.** `::test_reupload_restores_a_missing_stored_file` and
  `test_pipeline.py::test_import_of_a_missing_file_says_so` ("Could not read that resume: The
  file is missing from storage.").

## 5. Concurrent identical uploads

- **What.** If a concurrent upload of the same bytes commits first, `accept_upload`'s insert hits
  `documents_user_sha256`. The upload then deletes the file it just wrote and takes the reuse
  path once. Any other failure of the document-plus-run transaction deletes the file and is
  re-raised (500, logged).
- **Evidence.** `::test_concurrent_identical_upload_reuses_the_winner` and
  `::test_upload_leaves_no_orphan_when_its_transaction_fails`.

## 6. The `tailor` throttle lives in `start_tailor_run` (contract for step 6)

- **What.** `pipeline.start_tailor_run(user_id, match_or_job_id, *, conversation_id=None) ->
  Started` begins with `throttle("tailor:<user>", 30, 1 h)`, so the endpoint and the chat tool
  share one count. Over the limit it raises `ApiError(429, "Too many attempts. Try again in a few
  minutes.")`. `Started.as_dict()` is the chat tool's `{ok, runId, matchId, message}` /
  `{ok: false, message}`.
- **Why.** §4.5 and §7.2 throttle tailoring in both places. Putting the throttle in the shared
  start function means neither caller can forget it.
- **Handoff to step 6.** The `tailor_resume` tool calls `await db(pipeline.start_tailor_run,
  user_id, id, conversation_id=deps.conversation_id)` and returns `.as_dict()`. Its error
  wrapper already returns an `ApiError`'s message. Do not throttle a second time.

## 7. Response shapes and statuses the design left open

- `POST /api/resumes/tailor` answers 202 `{ok, runId, matchId, message}`. It adds `matchId`
  (today's action dropped it; the chat tool has it). The refusals:
  - 400 "No job was given to tailor for." for an unreadable body, a non-string or a blank id;
  - 404 "I couldn't find that job.";
  - 422 "There is no resume on file to tailor. Upload one on your profile first.";
  - 429.

  A second start while one is active answers 202 with that run's id and today's "Already
  tailoring your resume for … It will be ready in about a minute.". The old 15-minute window is
  replaced by `runs_one_active_per_key` (§9.8).
- `POST /api/resume/render` answers 202 `{ok, runId, message: "Rendering your resume to PDF. It
  takes a few seconds."}`, or 422 "There is no resume on file yet. Upload one first.". Its dedupe
  key is `render:<resumeId>`; a start while one is active returns that run with the same message.
- `PUT /api/resume` answers 200 `{ok: true, message: "Profile saved"}`; the message is the
  client's former toast. Without a base resume it answers 422 "Upload a resume before editing your
  profile." (today's refusal, now under `message`).
- `GET /api/documents/{id}`: a missing row answers 404 `{ok:false, message:"That no longer
  exists."}`. A row whose file is gone answers 404 "The file is missing from storage.". Both were
  `{error}` bodies before.
- `GET /api/resumes/{id}/preview`: 404 "That no longer exists." or 422 "This resume could not be
  read.".

## 8. `PUT /api/resume` validation sentences

- **What.** `api.resume_invalid` builds `"<Where>: <problem>."` from the first pydantic error.
  - **Where:** "Experience item N", "Education item N", "Project N", "Skill N", "Link N",
    "Basics" or "Summary".
  - **Field words:** start date, end date, title, company, school, degree, field of study, grade,
    link, and so on.
  - **Problems:**
    - "the start date is too long (40 characters at most)";
    - "the start date is empty";
    - "the company is missing";
    - "the degree should be text";
    - "the highlights should be a list";
    - "the source is not one of the options";
    - otherwise "the … is not valid".

  A body that is not an object answers "That resume could not be saved. Check the fields and try
  again.".
- **Why.** §5.2 gives one example sentence and bans pydantic's own text. Today's zod messages
  were raw.
- **Evidence.** `test_pipeline.py::test_put_resume_refusals` produces the design's exact example,
  "Experience item 2: the start date is too long (40 characters at most).".

## 9. Stop and the pipelines

- **What.**
  - The pipelines call `is_cancelled` between stages.
  - Every write that saves work first locks the run (`select_for_update`, active statuses only)
    and writes nothing once the run is no longer active:
    - the import's document text, new base version and `run.completed`, which commit in one
      transaction;
    - the tailored version, `job_matches.tailored_resume`, the stats and `tailor.verified`, also
      one transaction.
  - A stopped tailor or import returns quietly. `run.cancelled` comes only from
    `lifecycle.cancel()` (§9.9), so TS's "Tailoring cancelled. Nothing was saved." event is gone.
- **Evidence.** `::test_import_stopped_mid_run_saves_nothing`,
  `::test_import_save_refuses_a_finished_run` and `::test_tailor_stopped_mid_run_saves_nothing`.
  In each, the only `run.*` event is `run.cancelled`, it comes last, and nothing is saved.

## 10. Smaller clarifications

- **Phase `starting`.** The tailor writes it as its step's first statement. `run_step` and
  `claim` are step 4's, so "written on claim" means "as the claimed step begins".
- **Failures.** A `RenderError` (the resume is gone or unreadable) fails a render run at once as
  "Could not render the PDF: …", with no retry. Any other render error is retried once, and the
  run stays `running` across the retry. Import read errors map to "Could not read that resume:
  <PDFium/mammoth words>". Tailoring's own refusals are today's sentences, unprefixed; anything
  else gets `FAILURE_PREFIX` plus the short error ("Tailoring failed: Fake failure").
- **`resolve_match` lives in `resumes/pipeline.py`.** It is today's `resolveMatch` +
  `ensureMatch` (a match id, else the user's match for that job id, else a new `found` match
  when the job exists). It is here because `apps/matching/**` is step 5A's. If 5A writes an
  equivalent, step 6 or 7 keeps one of the two.
- **Ledger attribution.** `run_step`'s `llm_context` attributes the `extract`, `tailor` and
  `verify` calls to the run. The tests assert `llm_calls.run_id`.
- **The tailor reads a copy of the posting.** `pipeline._Posting` is copied off the `Job` row, so
  `tailor_resume` gets plain strings (pyright: a django-types `Job` does not satisfy
  `tailor.Posting`).

## 11. Checks that need the dev servers or a running worker (deferred to integration)

These are the step's **Live** checks. In this group they are deferred (three lanes share
`foray_app`'s queue), so the same behaviour is proven in pytest with `run_deferred()`. The
evidence is `tests/resumes/test_pipeline.py`, 30 tests:

- **Uploading `two-column.pdf` answers 202, and the `profile_import` run completes with counts.**
  Proven by `::test_upload_is_read_by_an_import_run`, on the fake `extract` behind the real
  ledger.
- **A renamed `.exe` gets 415.** Proven by `test_upload.py::test_upload_sniffs_bytes_not_names`.
- **Render produces a PDF served with the right headers, and a second render reuses it.** Proven
  by `::test_render_produces_a_served_pdf_and_reuses_it` (`slow`, real local Chrome). The first
  render stores one `resume_pdf`, served with `application/pdf`, `Content-Length`, the RFC 6266
  `inline`/`attachment` disposition, `private, no-store` and `nosniff`. The second answers the
  same `documentId` with `density: null`, and there is still one document.
- **Tailoring the match completes with a `documentId`, and the preview's CSP header is intact.**
  Proven by `::test_tailor_end_to_end` (fake models and fake render; CSP asserted) and
  `::test_tailor_with_a_real_render` (`slow`, real Chrome).

Still to run live at integration, with `pnpm dev:all`, a `create_test_user --with-resume
--with-match` user and a `dev_session` cookie:

- the upload through :3000;
- the 415;
- the render plus re-render;
- one real-model tailor, with a $0.20 ceiling.

No real model was called in this lane ($0).
