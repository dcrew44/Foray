# Step 8D: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 8D owns `src/app/(app)/chat/**`, `src/components/chat/**` and `src/lib/api/chat-types.ts`.
It changed `chat/[[...id]]/page.tsx`, `chat.tsx` and `tool-part.tsx`, and added `chat-types.ts`
and `src/components/chat/chat-link.ts`. No backend file was touched.

## 1. Tool inputs are typed by hand until 8E registers them (API gap for 8E)

- **What.** `chat-types.ts` takes the 16 tool **outputs** from the generated schema
  (`S["GetResumeOutput"]`, …), but writes the **inputs** by hand: `GetResumeInput`,
  `SalaryFloorInput`, `UpdateProfilePreferencesInput`, `AddCompanyInput`,
  `DiscoverCompaniesInput`, `ListCompaniesInput`, `StartSearchRunInput`, `GetRunStatusInput`,
  `CancelRunInput`, `ListJobsInput`, `JobIdInput` (get_job, evaluate_job, tailor_resume),
  `AnswerApprovalInput`, `MoveJobStageInput`, and `NoInput` (`Record<string, never>`, for
  get_profile_summary and list_pending_approvals). They mirror `backend/apps/chat/tools.py`'s
  input models field for field: snake_case names, every field required, null meaning absent.
  The enums inside them come from the schema (`Seniority`, `RemotePreference`, `SlotId`,
  `Stage`, `AutonomyMode`). The block carries a `TODO(8E)` marker.
- **Why.** `chat/api.py` (step 6's) registers only `OUTPUT_MODELS`. The input models are not
  OpenAPI components yet (step-6.md §3, step-7.md §9), so `components["schemas"]` has no
  `GetResumeInput` to name. Step 8D may not change the backend (§15.2).
- **For 8E.**
  1. Add every `INPUT_MODELS[name].__name__` plus `SalaryFloorInput` to
     `tests/core/test_wire_format.py`'s `SNAKE_CASE_COMPONENTS` ("§7.2: tool arguments keep
     today's snake_case names").
  2. In `backend/apps/chat/api.py`, `register_schema` each of `dict.fromkeys(INPUT_MODELS.values())`
     next to the outputs.
  3. `pnpm api:types`, then replace each hand-written input in `chat-types.ts` with
     `S["<Name>Input"]` (`JobIdInput` becomes the three `GetJobInput`, `EvaluateJobInput`,
     `TailorResumeInput`; `NoInput` becomes `GetProfileSummaryInput` and
     `ListPendingApprovalsInput`) and delete the `TODO(8E)` block. `tsc` then checks the hand
     types against the real ones.

## 2. The `chat` fake never calls `list_jobs` (fake gap for 8E)

- **What.** The Done item "a `list_jobs` card renders match lines" is to run in the pane with
  `LLM_FAKE=1`, but `backend/apps/chat/fakes.py` (step 6's) has no plan that calls `list_jobs`:
  its plans are a notice → `get_run_status`, "find"/"search" → `start_search_run`,
  "yes"/"approve" → the approval pair, "resume" → `get_resume`, "add <name>" → `add_company`,
  anything else → text (step-6.md §8). No prompt makes the fake list jobs, so the card cannot
  appear on fakes.
- **For 8E.** Add one plan to `fakes.py`, checked after SEARCH and APPROVE: a prompt matching
  `\b(matches|jobs)\b` (for example "Show me my matches") calls `list_jobs` with
  `{run_id: null, limit: null, stage: null, min_score: null, query: null}`, then answers one
  sentence ("Here are your matches."). That is exactly the turn §5's scratch test played with a
  `FunctionModel`, so the UI side is known to render it.
- **Until then.** The UI's rendering of a real `list_jobs` output is checked statically (§5, B1).

## 3. A new chat takes its `/chat/<id>` address only once Django claimed the id

- **What.** `chat.tsx` used to rewrite the address bar (`history.replaceState`) and announce the
  conversation in `onFinish`, unconditionally. Now it does so only when the turn's response
  carried `X-Conversation-Id` equal to the chat's id. `chatLink()` (§4) records that in the
  transport's `fetch`.
- **Why.**
  - In `ai` 7.0.106 `onFinish` runs on every outcome, errors included: `AbstractChat.makeRequest`
    calls it from its `finally` (`node_modules/ai/dist/index.js`, `makeRequest`).
  - A refused turn writes nothing. Step 6 runs steps 3–7 of `prepare_turn` in one
    transaction, so a refusal "leaves nothing behind, not even the claimed conversation"
    (step-6.md §5). The refusals are a 429 throttle, a 400 unreadable or empty message, and a 404
    for someone else's id.
  - With today's unconditional rewrite, a refused first message would put `/chat/<id>` in the
    address bar and announce a chat that does not exist; a reload would then be a 404.
  - `X-Conversation-Id` is sent only on a streamed answer, after `prepare_turn` committed
    (`apps/chat/uistream.py` `ui_headers`, used by `apps/chat/views.py` for the
    `StreamingHttpResponse`).
- **Edge.** A Stop pressed before the response headers arrive leaves the address at `/chat`
  even if Django had already saved the message. The next message in the same chat reuses the id,
  Django claims it again (same user), and the address is rewritten then.
- **Evidence.** §5, A1 and A2, with a mutation (a claim set on any response fails A2).

## 4. The transport lives in `chat-link.ts`

- **What.** `src/components/chat/chat-link.ts` exports `chatLink(chatId, alreadyClaimed)`. It
  returns `{transport, claimed()}`: a `DefaultChatTransport<ForayUIMessage>` whose
  `prepareSendMessagesRequest` sends `{id, message, tz}`, and whose `fetch` notes the claim (§3).
  It also exports `browserTimeZone()`, which is `Intl.DateTimeFormat().resolvedOptions().timeZone`
  and is `undefined` if that throws or is empty. Django ignores a missing or invalid `tz`.
  `chat.tsx` creates the link once per chat with `useState(() => chatLink(...))`; the component
  is keyed by the chat id.
- **Why.**
  - Built inline, with a ref for the claim, the transport failed ESLint: the React compiler rule
    `react-hooks/refs` reported "Cannot access refs during render", because the transport is
    constructed during render.
  - A plain module also lets the transport be exercised in Node against Django's real stream
    (§5).
- **Unchanged.** `@ai-sdk/react` 4.0.109 `useChat` keeps the latest `transport` in a ref and
  creates its `Chat` once per `id`, so one transport per chat is what it used before as well.

## 5. Checks run in this step (the pane is deferred)

**Static.**
- `pnpm exec next typegen`, then `tsc --noEmit`: exit 0.
- `pnpm lint` (ESLint, `ruff check`, `ruff format --check`): exit 0.
- `pnpm typecheck` (`tsc` and pyright, 0 errors): exit 0.
- `pnpm build`: exit 0, with `/chat/[[...id]]` dynamic. Its `BetterAuthError` lines come from
  legacy modules still in the build (§6).
- `pnpm api:types`, then `git diff --exit-code backend/openapi.json src/lib/api/schema.d.ts`:
  no drift. Nothing was staged.
- `pytest tests/chat tests/pages tests/core/test_wire_format.py`
  (`FORAY_TEST_DB_NAME=test_foray_app_lane8d`): 252 passed.

**Real turns through Django's view.** A scratch pytest (not committed) ran on the lane test
database, with `-p tests.conftest`, so the network block applied.
- **Setup.** A `make_user()` user with two matches. A `FunctionModel` behind the real model stack
  stood in for the chat model: it is the turn §2 proposes for the fake.
- **The turns, through `AsyncClient` and the real `chat_view`, `pump_turn` and
  `ForayVercelStream`.** The real `list_jobs` and `add_company` tools answered.
  1. "Show me my matches", with `tz` set to Node's resolved zone, `America/Los_Angeles`.
  2. "add linear.app".
- **The follow-up.** The `company_discovery` run that `add_company` started was marked
  completed. `events.emit("run.completed")` followed, then the real `run_follow_up(event_id)`,
  which returned `posted`.
- **What came back:**
  - each turn: 200 with `X-Conversation-Id`;
  - `users.timezone` = `America/Los_Angeles`;
  - `GET /api/conversations/{id}`: `{conversation: {id, title: "Show me my matches"}, messages}`.
    `metadata` is absent on ordinary messages. It is `{kind: "run_update", label: "Discovery
    finished", runId, eventId}` on the notice and `{kind: "follow_up", …}` on the reply;
  - the live snapshot: `chat.followup` at level debug, with `data {conversationId, runId,
    messageId, label}`;
  - `add_company`'s output: `{ok: true, runId, message}`;
  - `DELETE`: `{ok: true}`. Then `GET` gave 404 `{ok: false, message: "That chat does not
    exist."}`, and the list was empty;
  - an empty message: 400 `text/plain` "That message could not be read.";
  - `GET /api/pages/chat`: `{suggestions: [{title, prompt, prefill: false}]}`.

**The UI on those bytes.** A scratch `tsx` script (not committed) imported the real modules. A
`module.registerHooks` resolve hook stubbed `@/app/actions/{runs,approvals}`, which
`RunCard`/`ApprovalCard` (8B's) still import (§6).
- **A1.** The AI SDK's `Chat` ran with `chatLink()`'s transport over a `fetch` that answered
  with turn 1's SSE bytes and headers.
  - The request body had exactly `{id, message, tz}`, with `tz` = `America/Los_Angeles` and the
    user's text part.
  - `claimed()` became true.
  - The rebuilt reply had the server's reply id, and its parts deep-equal the stored row's.
- **A2.** For a refusal (the 400 above, no header), `chat.error.message` was "That message could
  not be read.", `claimed()` stayed false, and `onFinish` reported `isError`.
- **B1.** `renderToStaticMarkup` of the real `<Chat>`, inside the real `LiveProvider` and an
  `AppRouterContext`, rendered the stored conversation. Two extra parts were added: a
  `tool-list_jobs` `output-error` and a `tool-no_such_tool` `output-error`, both with "Its input
  was not valid, so it did not run." (step-6.md §1–2). The render held:
  - the two match lines (titles, company, the "Found" stage, the score 88, `/matches/<id>` links);
  - `add_company`'s `RunCard` (its loading state, since no live poll ran);
  - the notice as a `role="note"` divider reading "Discovery finished", with the notice text
    itself not shown;
  - the follow-up reply;
  - "Looking through your matches failed" and "Running no such tool failed", each with the
    sentence.
- **B2.** A new chat rendered "What should Foray do?" and the page's suggestion.
- **Mutations.** Each mutation was reverted afterwards.
  - Dropping `tz` from the body fails A1.
  - Claiming on any response fails A2.
  - Keying the divider on the wrong `kind` fails B1.

**Deferred to the pane (8E, or the integration).** All five Done items:
- streaming, and the `/chat/<id>` address;
- recent chats list and delete;
- the `list_jobs` card, which needs §2's fake plan first;
- the follow-up divider without a reload;
- `users.timezone` equal to the pane's zone.

**Money and data.**
- $0: no provider was called.
- Only the lane test database was written. Nothing was written to `foray_app`.
- No server or worker was started.
- Legacy `foray` write counters stayed at 41158/1014040/5351.

## 6. Observations, no change

- **Legacy modules still in the chat route.** `tool-part.tsx` renders `RunCard` and
  `ApprovalCard`. Those import the legacy server actions `@/app/actions/runs` and
  `@/app/actions/approvals`, so the built `/chat` route still traces Better Auth and `pg`
  (`page.js.nft.json`). That goes when 8B converts `runs/{run-card,approval-card}.tsx`.
  `matches/match-line.tsx` (also 8B's) imports its `MatchRow` type from `lib/db`. That import is
  type-only and is erased.
- **Stored outputs are read loosely.** Tool outputs are what Django stored. A conversation
  imported from the TypeScript app keeps that app's shapes: an `add_company` that registered
  inline (`{ok, company, source, …}`), and job cards without a unified score. `tool-part.tsx`
  therefore keeps reading outputs defensively. `ForayTools` types today's outputs, as
  `O | ToolFailure`, because the one error wrapper can return `{ok: false, message}` from any
  tool, including those whose own output has no `ok`. Its type-level changes:
  - `COPY` is a `Record<ForayToolName, …>`, so a new tool without its lines is a type error;
  - the part state is `ToolUIPart<ForayTools>["state"]`;
  - a stage is shown only if it is a known `StageId` (`JobCard.stage` is a plain string in the
    schema).

  `CompanyAdded` is kept for the imported shape; today's `add_company` output takes the existing
  `runId` → `RunCard` branch.
- **The page checks the id before calling Django.** `/chat/<id>` answers `notFound()` for an id
  that is not a UUID before calling Django, which would answer 404 too. This keeps the segment
  out of the request path: `new URL("/api/conversations/..", origin)` would resolve to `/api/`.
- **`ChatSuggestion`** now comes from the schema with `prefill: boolean` required (it was
  optional), and moved from `chat.tsx` to `chat-types.ts` with the other chat types.
