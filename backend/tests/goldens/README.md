# Goldens

Written by `scripts/capture-goldens.mts` (migration step 0) from the TypeScript code, before
it is deleted. Do not edit by hand; re-run the script instead:

    pnpm tsx scripts/capture-goldens.mts

Conventions:
- Dates are ISO 8601 UTC strings. The capture clock is frozen at 2026-10-05T03:30:00.000Z, and the process
  time zone is America/Los_Angeles (the chat system prompt's date line depends on both).
- Salary inputs written as strings are what Postgres returns for the legacy `numeric` columns;
  numbers are what `::float8` reads return.
- `adapters/**` hold every `NormalizedJob` field except `raw` (the fixture payload itself);
  `descriptionRaw` is pinned as `{sha256, utf8Bytes}` of the exact string, rebuilt from the fixture.
- `prompts/<builder>/<case>.json` hold `{instructions, prompt}` exactly as the model received
  them (the system message and the user text). Vision prompts also list their parts; page
  images are recorded by media type only.
- `uistream/` holds the raw SSE and the persisted (`onEnd`) message per scenario;
  `scenarios.json` lists the inputs and the scripted model chunks. `uistream/prototypes/` holds
  the research's two TypeScript reference turns (`ts-turn{1,2}`), moved here from
  `docs/migration/prototypes/` in step H2 (not written by the capture script).
- `auth/better_auth_hashes.json` are real Better Auth hashes of test passwords (not secrets).
- Library versions: ai 7.0.106, better-auth 1.7.5,
  unpdf 1.8.1, mammoth 1.12.3.

Files:
- `README.md`
- `adapters/ashby/_not-found.json`
- `adapters/ashby/linear.json`
- `adapters/ashby/vanta.json`
- `adapters/greenhouse/_not-found.json`
- `adapters/greenhouse/anthropic.json`
- `adapters/greenhouse/stripe.json`
- `adapters/jsearch/entry-level-sf.json`
- `adapters/lever/_not-found.json`
- `adapters/lever/matchgroup.json`
- `adapters/lever/spotify.json`
- `auth/better_auth_hashes.json`
- `auth/source.json`
- `clean/cases.json`
- `clean/fixture_removed.json`
- `dedupe/cases.json`
- `discovery/cases.json`
- `links/cases.json`
- `links/crusoe.text.txt`
- `links/crusoe.with-links.txt`
- `links/retool.text.txt`
- `links/retool.with-links.txt`
- `normalize/locations.json`
- `normalize/plausible_salary.json`
- `normalize/primary_location.json`
- `normalize/remote.json`
- `normalize/salaries.json`
- `prompts/careers_listing/crusoe.json`
- `prompts/careers_listing/retool.json`
- `prompts/chat_system_prompt/complete_all_autopilot.json`
- `prompts/chat_system_prompt/complete_fractional_floor.json`
- `prompts/chat_system_prompt/complete_optional_outstanding.json`
- `prompts/chat_system_prompt/complete_optional_skipped.json`
- `prompts/chat_system_prompt/interview_empty.json`
- `prompts/chat_system_prompt/interview_partial_skipped.json`
- `prompts/chat_system_prompt/no_resume.json`
- `prompts/chat_system_prompt/no_resume_named_autopilot.json`
- `prompts/deep_eval/equal_min_max_truncated.json`
- `prompts/deep_eval/fractional_min_only_one_day.json`
- `prompts/deep_eval/integral_range_with_brief.json`
- `prompts/deep_eval/no_pay_no_date_no_description.json`
- `prompts/derive_search/no_request.json`
- `prompts/derive_search/request_no_hints_unicode.json`
- `prompts/derive_search/request_with_hints.json`
- `prompts/discover_companies/five.json`
- `prompts/discover_companies/one_unicode.json`
- `prompts/extract_text/plain-docx.json`
- `prompts/extract_text/serif-dense.json`
- `prompts/extract_text/single-column.json`
- `prompts/extract_text/two-column.json`
- `prompts/extract_vision/single-column-forced.json`
- `prompts/fast_score/fractional_salary_sparse_resume.json`
- `prompts/fast_score/integral_salary_long_description.json`
- `prompts/fast_score/missing_max_no_resume.json`
- `prompts/fast_score/no_salary_empty_resume.json`
- `prompts/tailor/html_only_no_notes.json`
- `prompts/tailor/no_description_fast_only.json`
- `prompts/tailor/with_notes_and_goals.json`
- `prompts/tool_salary/get_job.json`
- `prompts/tool_salary/list_jobs.json`
- `prompts/verify_judge/education_without_changes.json`
- `prompts/verify_judge/work_summary_project_with_changes.json`
- `resumes/docx/plain.json`
- `resumes/garble_cases.json`
- `resumes/pdf/serif-dense.json`
- `resumes/pdf/single-column.json`
- `resumes/pdf/two-column.json`
- `strings/describe_brief.json`
- `strings/describe_evaluations.json`
- `strings/describe_preferences.json`
- `strings/describe_update.json`
- `strings/resume_text.json`
- `strings/summarize_search_run.json`
- `template/base-compact-titled.html`
- `template/base.html`
- `template/cases.json`
- `template/empty.html`
- `template/helpers.json`
- `template/hostile.html`
- `template/sparse.html`
- `uistream/approval_turn1.persisted.json`
- `uistream/approval_turn1.sse`
- `uistream/approval_turn2.persisted.json`
- `uistream/approval_turn2.sse`
- `uistream/invalid_tool_input.persisted.json`
- `uistream/invalid_tool_input.sse`
- `uistream/length_finish.persisted.json`
- `uistream/length_finish.sse`
- `uistream/reasoning_text.persisted.json`
- `uistream/reasoning_text.sse`
- `uistream/scenarios.json`
- `uistream/step_cap.persisted.json`
- `uistream/step_cap.sse`
- `uistream/stream_error.persisted.json`
- `uistream/stream_error.sse`
- `uistream/text.persisted.json`
- `uistream/text.sse`
- `uistream/tool_parallel.persisted.json`
- `uistream/tool_parallel.sse`
- `uistream/tool_raises.persisted.json`
- `uistream/tool_raises.sse`
- `uistream/tool_returns_null.persisted.json`
- `uistream/tool_returns_null.sse`
- `uistream/tool_returns_undefined.persisted.json`
- `uistream/tool_returns_undefined.sse`
- `uistream/tool_two_steps.persisted.json`
- `uistream/tool_two_steps.sse`
- `uistream/prototypes/ts-turn1.persisted.json`
- `uistream/prototypes/ts-turn1.sse`
- `uistream/prototypes/ts-turn2.persisted.json`
- `uistream/prototypes/ts-turn2.sse`
- `uistream/unknown_tool.persisted.json`
- `uistream/unknown_tool.sse`
