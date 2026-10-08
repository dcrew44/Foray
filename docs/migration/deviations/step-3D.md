# Step 3D: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. Django escapes `'` as `&#x27;` (template golden `ALLOWED_DIFFS`)

- **What.** `templates/resume.html` renders through Django's autoescape (§11.5), which writes `'`
  as `&#x27;`; the TS template wrote `&#39;`. `tests/resumes/test_template.py` compares every
  golden byte for byte after mapping `&#x27;` to `&#39;` (its `ALLOWED_DIFFS`), and the ported
  hostile-text case asserts the same substring through that mapping. There is no `escape_html`
  helper: nothing hand-escapes, and None never reaches the template (the context builder turns
  a missing value into an empty string or no element).
- **Why.** Autoescape is the design's choice and Django's escaping is not configurable. Both
  spellings are the same character to every HTML parser; the PDF text is unchanged.
- **Evidence.** All five `goldens/template/*.html` (base, compact-titled, sparse, empty, hostile)
  are byte-equal after the mapping; only `hostile.html` contains a `'`.
- **Template layout.** `templates/resume.html` (the CSS is copied verbatim from the golden) and
  `templates/resume/parts.html`, an inline partial for title and contact lines. The partial must
  keep **no trailing newline**: its output is inline.

## 2. Tailoring reads step 3C's HTML cleaner (an unstated edge 3C → 3D)

- **What.** `tailor.posting_text` falls back to `apps.jobs.sources.clean.html_to_text` when a
  posting has no `description_text` (today's `htmlToText`). It is looked up with
  `importlib.import_module` at call time, so the module imports and type-checks without 3C.
  `test_prompts.py::test_tailor_prompt[html_only_no_notes]` uses the real cleaner when
  `apps.jobs.sources` exists and otherwise stands in with the TS output for that one input (taken
  from the golden), so the rest of the builder is pinned either way.
- **Why.** The design lists 3D as depending on step 2 only, but `tailor.ts` imports
  `htmlToText` from `sources/clean.ts`, which §10.3 gives to 3C.
- **Evidence.** All three tailor prompt goldens are byte-equal in this lane.

## 3. The fake overrides: the interface assumed for `fake.register`

- **What.** §8.9 names `fake.register(purpose, fn)` but not `fn`'s shape. `resumes/fakes.py`
  defines each override as a pydantic-ai `FunctionModel` function,
  `(messages, info) -> ModelResponse | None`, answering through `info.output_tools[0]`. It
  returns **None** when the output tool is not its schema (decided by the schema's property
  names), because `extract` has two owners (3C's careers-page extractor and these resumes), so
  the registry must try the next override, then its schema-driven default. The module calls
  `register()` on import when `apps.llm.fake` exists (`importlib.util.find_spec`), and also
  exposes `OVERRIDES` and `register()`.
- **Why.** Step 3B builds `apps/llm/fake.py` in parallel; this lane cannot see it.
- **Evidence.** `tests/resumes/test_fakes.py` runs every override through a real pydantic-ai
  `Agent(FunctionModel(...))`: each declines a foreign output type; the extract fake reads all
  four fixtures; `tailor` + `verify_tailored` on the fakes give zero findings and no judge call;
  `register()` hands all three to a stub registry.

## 4. `structured()` is still step 1's stub in this lane

- **What.** `extract.py`, `tailor.py` and `verify.py` call `apps.llm.structured.structured` as
  §8.4 specifies. The tests patch that module-level name with
  `tests/resumes/support.structured_with(fn)`, which runs a real pydantic-ai agent over a
  `FunctionModel` (no registry, ledger or budget), or with a capture that records the exact
  `instructions`/`prompt` sent. The vision prompt is a list: `"Transcribe this resume."` then one
  `BinaryContent(png, media_type="image/png")` per page.
- **Why.** Step 3B implements `structured()`, `model()` and the wrappers concurrently.

## 5. Page images are encoded without Pillow

- **What.** `parse/pdf.render_pages` renders with `page.render(scale=2, rev_byteorder=True)`
  (RGB) and writes the PNG itself (`_png`: IHDR, one zlib IDAT of unfiltered scanlines, IEND).
- **Why.** `bitmap.to_pil()` needs Pillow, which is not a dependency, and `pyproject.toml` is
  step 1's. The research's comparison (1224×1584 RGB) holds.
- **Evidence.** `test_pdf_text.py::test_page_images_for_the_vision_fallback` checks the size and
  inflates the IDAT to 1584 × (1 + 1224 × 3) bytes; the image was also viewed by eye.

## 6. Text and garble parity, precisely

- **PDF text.** pdf.js's *selective* NFKC (its `normalizeUnicode` code-point set, copied from
  unpdf's bundled pdf.js 6.1.200), U+2010/U+2011 to `-`, `rstrip()` per page, then unpdf's merge.
  `single-column` and `two-column` are byte-equal to the goldens. `serif-dense` differs in six
  lines, declared in `test_pdf_text.py::SERIF_DENSE_ALLOWED`: the letter-spaced name line
  (`ILONA BERGQUI ST-MAHONEY`), one letter-spaced heading (`E D U C AT I O N`) and four small-caps
  skill labels in source case. Its garble decision (not garbled, no reasons) is the same.
- **Garble, "counted the Python way".** Lengths are code points and letters are
  `str.isalpha()`; whitespace is JavaScript's `\s` spelled out (as §10.3 does for `clean.py`);
  the reason strings round like `Math.round` and `toFixed`. Metrics that are integral are written
  as ints (`900`, not `900.0`), as JSON from JS had them. All 8 golden cases and the DOCX report
  are equal.
- **PDFium errors** read: format or file errors → "Invalid PDF structure." (pdf.js's words); a
  password → "The PDF is password-protected."; an unsupported security handler → "The PDF uses
  an unsupported security scheme."; anything else → "The PDF could not be read.". A DOCX mammoth
  cannot read → "That Word file could not be read.". `extract.ResumeReadError` carries the words
  for "Could not read that resume: <message>".

## 7. Python signatures that differ from the TS ones

- `extract_resume(data, kind)` takes the sniffed type (`"pdf" | "docx"`, step 5B's
  `parse/sniff.py`), not the client's MIME. The run's phases are separate functions:
  `read_document` (reading), `extract_from` (extracting), `schema.with_ids` (saving).
- `tailor_resume(base, job, *, deep_eval, fast_eval, preferences)` takes the stored evaluations
  instead of a match object; `TailorDraft.retried` is gone (pydantic-ai does not report it).
  The model's output type is `tailor.TailorModelOutput` (not `TailorOutput`, which is the run's
  output model in the catalogue). `MappingDrop.as_revision()` builds the run's "dropped" record.
- The verify `Judge` is `async (base, claims, changes) -> dict[claim_key, Verdict]`: attribution
  comes from the §8.8 context, so there is no context argument. Finding targets are dataclasses
  (`BasicsTarget`, `ItemTarget`, `FieldTarget`, `SkillTarget`, `TextTarget`) and a field target
  names the attribute (`start_date`), not the camelCase key.
- `verify_tailored`'s `model_error` is `str(error)`, or the exception's class name when that is
  empty (a bare `TimeoutError()`).

## 8. Additions the design implies but does not name

- `edits.save_user_edit(user_id, resume) -> SavedEdit(row, changed)` is `PUT /api/resume`'s
  core (§11.3: stamp, no-op when nothing changed, else a new version);
  `edits.create_base_version(user_id, resume, source_document_id=None)` writes the next base
  version in a savepoint and retries once on `resumes_base_version` (the upload run uses it
  inside its own transaction). `tailor.save_tailored_resume(...)` does the same for a match's
  tailored version (constraint `resumes_tailored_version`) and sets
  `job_matches.tailored_resume` in the same transaction (§11.4 "Saving").
- `render.render_resume(user_id, resume_id) -> RenderResult(document_id, pages, density, reused)`.
  `density` is None when an earlier PDF is reused (the stored PDF does not record which density
  it used). If a reusable document's file is missing or unreadable, the resume is rendered again
  and that document row is repaired in place (the `(user, render_hash)` constraint forbids a
  second row). Two concurrent renders of the same resume keep one document; the loser deletes
  its file. A file whose row could not be written is deleted (no orphans).
- One Chromium per **event loop** (Playwright objects are bound to the loop that created them):
  the worker has one loop; each test gets its own and closes it with `render.close_browser()`.
- `core/storage.py`: `LocalStorage.open(key)` returns a binary file object (for `FileResponse`);
  `storage()` builds the store from `settings.STORAGE_DIR` at call time; `storage_key()` and
  `sha256_hex()` sit beside it. An empty key, the root itself, `..`, an absolute path and a
  symlink out of the root are all refused.
- `apps/resumes/parse/__init__.py` exists (a docstring only), so `parse` is a regular package.

## 9. Render check environment

- **What.** This dev machine has no bundled Playwright browsers (`~/Library/Caches/ms-playwright`
  is absent), so the slow render tests ran through the `channel="chrome"` fallback (the local
  Google Chrome). `test_render.py`'s browser tests skip, with the launch error, when no Chromium
  can start.
- **Evidence.** `single-column.html` rendered to 2 pages, the golden's `pageCount`; the second
  `render_resume` of the same resume returned the same document with `_render_once` and the
  browser patched to fail.
