# Area map: chat-agent

Repo root for all paths below: `/Users/haydenbarolette/Projects/JobSearchAgent/.claude/worktrees/middleware-django-migration-389b1a`.
AI SDK sources cited as `ai/src/...` live under `node_modules/ai/src/` (ai@7.0.106), `@ai-sdk/react@4.0.109`,
`@ai-sdk/provider-utils@5.0.44` (under `node_modules/.pnpm/@ai-sdk+provider-utils@5.0.44_zod@4.6.5/node_modules/@ai-sdk/provider-utils/src/`).

Wire captures in section 3 were produced by running the real `createAgentUIStreamResponse` with a mock model
(scratch scripts `capture-stream.mjs`, `capture-cancel.mjs`, `convert.mjs`, `schemas.mjs` in the scratchpad, next to this map).
They are byte-exact output of the installed SDK.

---

## 0. One-paragraph summary

A chat turn is `POST /api/chat` with body `{id, message}`. The server authenticates, claims/creates the
conversation, loads the persisted history (Postgres `messages`, one AI-SDK `UIMessage` per row), saves the
incoming user message, builds a per-user `ToolLoopAgent` (model purpose `chat`, a system prompt assembled
from the user's profile row, 16 tools with the user id closed over, `stopWhen: isStepCount(8)`), and streams
the agent as an AI SDK v7 "UI message stream" (SSE, `x-vercel-ai-ui-message-stream: v1`). When the stream ends
(or the client disconnects) the assembled assistant `UIMessage` is upserted into `messages`. Background runs
started by tools carry `conversation_id`; when such a run pauses/finishes/fails, `emit()` enqueues a
`chat.follow_up` pg-boss job; the worker writes a synthetic user-role "run update" message, runs the same agent
headless with read-only tools (`activeTools`, 4 steps, 90 s timeout), saves its reply, and emits a
`chat.followup` agent event that the open chat (polling `/api/live`) reacts to by re-fetching
`GET /api/conversations/:id`. No AI-SDK tool-approval (`needsApproval`) is used anywhere; the product's
approvals are domain rows answered via the `answer_approval` tool or the UI card.

---

## 1. HTTP surfaces

### 1.1 `POST /api/chat` — one chat turn (`src/app/api/chat/route.ts:21-87`)

Auth: `requireUser()` (`src/lib/auth/session.ts:21-25`) — Better Auth session from cookies; on no session it
calls Next `redirect("/sign-in")`, which in a Route Handler becomes **HTTP 307 `Location: /sign-in`, empty body**
(`node_modules/next/dist/server/route-modules/app-route/module.js:465-492`). Not covered by `src/proxy.ts`
(matcher excludes `/api/*`, `src/proxy.ts:20-33`). Port: return 401 JSON instead (client shows response text as
the error message, see 4.4).

Request body (JSON, `Content-Type: application/json`, credentials same-origin cookies):
```
{ "id": "<conversation uuid chosen by client>", "message": <UIMessage> }
```
- `id?: string`, `message?: ForayUIMessage`, `messages?: ForayUIMessage[]` (`route.ts:24-28`).
- `incoming = body.message ?? body.messages?.at(-1)` (`route.ts:32`). `messages` is accepted only so a plain
  SDK-default request (`{id, messages, trigger, messageId}`) also works.
- Missing → `400`, body text `No message provided.` (plain text) (`route.ts:33-35`).
- The client always sends exactly (see 4.1):
  `{"id":"<uuid>","message":{"id":"<16-char id>","role":"user","parts":[{"type":"text","text":"..."}]}}`
  (`metadata` is `undefined` client-side and dropped by `JSON.stringify`; no `trigger`/`messageId` is sent because
  `prepareSendMessagesRequest` replaces the body — `src/components/chat/chat.tsx:73-75`).
- **No validation** of `incoming.role`, `incoming.id`, `incoming.metadata` or part types happens in the route.
  Only the SDK's `validateUIMessagesForAgent` (section 3.7) runs later, over history+incoming.

Steps (in order):
1. `firstText = incoming.parts?.find(p => p.type === "text")` (`route.ts:37`).
2. `conversation = ensureConversation(user.id, body.id, firstText ? firstText.text.slice(0, 80) : undefined)`
   (`route.ts:38-44`). Title = first 80 UTF-16 code units of the first text part, used **only when the
   conversation is created** (never updated later). See 5.1 for claim semantics.
3. `history = loadMessages(user.id, conversation.id)` re-typed, not revalidated ("trusted because this server wrote
   them") (`route.ts:46-51`).
4. `uiMessages = [...history, incoming]` (`route.ts:52`).
5. `saveMessages(user.id, conversation.id, [incoming])` — **user message is persisted before the model runs**
   (`route.ts:54`). (Also bumps `conversations.updated_at`.)
6. `agent = await chatAgent(user.id, { conversationId: conversation.id, userName: user.name })` — built *outside*
   the LLM context so the agent's own profile read is not attributed (comment `route.ts:56-60`, code `61-64`).
7. Return `withLlmContext({ userId: user.id }, () => createAgentUIStreamResponse({...}))` (`route.ts:66-86`):
   - `agent`, `uiMessages`, `originalMessages: uiMessages`
   - `headers: { "x-conversation-id": conversation.id }` (`route.ts:71`) — **not read by any client code** (grep).
   - `generateMessageId: generateId` (`route.ts:78`). Why (comment `72-77`): without it the response message id
     is empty (the SDK only derives an id from `originalMessages` when the last one is an assistant message), and
     an empty id collides on the `messages` primary key so every reply upserts onto one orphan row.
   - `onEnd: async ({ responseMessage }) => { if (responseMessage) await saveMessages(user.id, conversation.id,
     [responseMessage]) }` (`route.ts:80-84`). Runs on normal end **and** on client cancel (3.5); saves regardless of
     outcome (completed / failed / aborted), so a failed turn persists whatever parts streamed (possibly only
     `[{"type":"step-start"}]` or `[]`).
   - Not passed: `abortSignal`, `timeout`, `onError` (so all error text sent to the client is masked to
     `"An error occurred."`), `messageMetadata` (assistant replies from chat have `metadata` = null),
     `sendReasoning` (defaults true: reasoning chunks are streamed), `sendSources` (false).
   - **No timeout** on the chat turn (DESIGN.md invariant 12 claims "isStepCount(8) and a timeout"; only the
     follow-up path has a timeout). Bounded only by the 8-step cap and the provider. No `maxDuration` export.

Response: `200`, headers exactly (captured):
```
content-type: text/event-stream
cache-control: no-cache
connection: keep-alive
x-vercel-ai-ui-message-stream: v1
x-accel-buffering: no
x-conversation-id: <uuid>
```
(`ai/src/ui-message-stream/ui-message-stream-headers.ts:1-7`, merged by `prepareHeaders` in
`create-ui-message-stream-response.ts:39-43`). Body: SSE, see section 3.

Failure modes before streaming starts (all happen before the `Response` exists, so Next returns a 500 with its
default body): DB errors, `ModelNotConfiguredError` from `model("chat")` (`src/lib/llm/models.ts:90-110`),
and **history validation failure** inside `createAgentUIStream` (3.7). Note the user message was already saved in
step 5 in these cases.

### 1.2 `GET /api/conversations` — sidebar list (`src/app/api/conversations/route.ts:7-21`)
- `requireUser()`; `listConversations(user.id, 30)`.
- 200 JSON, header `Cache-Control: no-store`:
  `{"conversations":[{"id":"<uuid>","title":"<title or 'Untitled chat'>","updatedAt":"<ISO>"}]}` ordered by
  `updated_at desc`. `updatedAt` via `Date.toISOString()` (`YYYY-MM-DDTHH:MM:SS.sssZ`).
- Consumer: `src/components/shell/recent-chats.tsx:35-40` (shows first 20; refetches on window event
  `foray:conversations-changed`).

### 1.3 `GET /api/conversations/:id` (`src/app/api/conversations/[id]/route.ts:16-22`)
- `requireUser()`; `getConversation(user.id, id)` → null (non-UUID, missing, or someone else's) →
  `404 {"error":"Not found"}`.
- 200 `{"messages":[UIMessage...]}` with `Cache-Control: no-store`. Each message `{id, role, parts, metadata?}`
  (`metadata` omitted when DB null). Title is **not** returned (the page loader reads it server-side).
- Consumer: `chat.tsx:105-110` (`reload()` after a follow-up).

### 1.4 `DELETE /api/conversations/:id` (`[id]/route.ts:25-30`)
- `deleteConversation(user.id, id)`; false → `404 {"error":"Not found"}`; else `200 {"ok":true}`.
  Messages cascade (FK `on delete cascade`, `src/lib/db/schema/chat.ts:43-45`).
- Consumer: `recent-chats.tsx:53-57` (optimistic removal; navigates to `/chat` if viewing it).

### 1.5 Page loader `/chat/[[...id]]` (`src/app/(app)/chat/[[...id]]/page.tsx:20-59`) — server component
- `requireUser()`.
- No id: `draftId = crypto.randomUUID()`; renders `<Chat chatId={draftId} isNew initialMessages={[]} title={null}
  suggestions={await suggestionsFor(user.id)} key={draftId}/>` (`page.tsx:28-40`).
- With id: `getConversation` → `notFound()` (404 page) if missing/foreign; `loadMessages` → `<Chat chatId=… isNew={false}
  initialMessages={history} title={conversation.title} suggestions={[]}/>` (`page.tsx:44-58`).
- `suggestionsFor(userId)` (`page.tsx:62-115`) uses `getOnboardingState` (`src/lib/profile/onboarding.ts:31-54`):
  - `!hasResume` → `[{title:"How does this work?", prompt:"What can you do, and what do you need from me to get started?"}]`
  - `!ready` → "Set up my search" / "Let's set up my job search. Ask me what you need to know."; "What's on my resume?" /
    "Summarize my resume and tell me what kinds of roles it fits."
  - ready → `top = listMatches(userId,{stages:["found","shortlisted"],limit:1})[0]`; `role = target_roles[0].toLowerCase() ?? "roles"`;
    `where = locations[0]`: "Find new roles" / `Find new ${role}${where ? ` in ${where}` : ""} for me.`;
    "Compare my best matches" / "Which of my matches fit me best, and why? Be specific."; top ?
    "Tailor my resume" / `Tailor my resume for the ${top.title} role at ${top.company}.` : "Improve my resume" /
    "What would make my resume stronger for the roles I want?"; "Add a company" / "Start tracking this company for me: " (`prefill: true`).
- In a Django world this loader needs an endpoint that returns `{conversation: {id,title} | 404, messages}` and one for
  suggestions (or keep computing them from other endpoints).

---

## 2. Database (`src/lib/db/schema/chat.ts`, queries `src/lib/db/queries/conversations.ts`)

### 2.1 Tables
`conversations` (`schema/chat.ts:16-32`):
| column | type | notes |
|---|---|---|
| id | uuid PK default `gen_random_uuid()` | client may *claim* an id (5.1) |
| user_id | text not null FK `user.id` on delete cascade | |
| title | text null | first 80 chars of the first message |
| created_at | timestamptz not null default now() | |
| updated_at | timestamptz not null default now() | bumped by every `saveMessages` |
Index `conversations_user_updated_at_idx (user_id, updated_at desc)`.

`messages` (`schema/chat.ts:38-65`) — "One AI SDK `UIMessage` per row, keyed by the SDK's own message id":
| column | type | notes |
|---|---|---|
| id | text PK | AI SDK id: client-generated 16-char `[0-9A-Za-z]{16}` for user messages, server `generateId()` (same format) for assistant replies and run updates |
| conversation_id | uuid not null FK conversations on delete cascade | |
| user_id | text not null FK user on delete cascade | |
| role | text not null | `user` / `assistant` (`system` possible per SDK schema, never written) |
| parts | jsonb not null | `UIMessage.parts` verbatim |
| metadata | jsonb null | see 2.3 |
| created_at | timestamptz not null default now() | **only ordering key** for history |
Index `messages_conversation_created_at_idx (conversation_id, created_at)`.

`llm_calls` (`schema/chat.ts:74-108`) is written by ledger middleware for every model call of the agent:
`purpose='chat'`, `user_id` from context, `run_id` null for chat turns and follow-ups. (Owned by the LLM area.)

### 2.2 Query functions (all take `userId` first — DESIGN invariant 8)
- `listConversations(userId, limit=50)` (`conversations.ts:14-21`): `SELECT * FROM conversations WHERE user_id=$1 ORDER BY updated_at DESC LIMIT $2`.
- `getConversation(userId, id)` (`24-32`): returns null if `id` fails `UUID` regex (`83-84`,
  `/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i`); else `WHERE id=$1 AND user_id=$2 LIMIT 1`.
- `deleteConversation(userId, id)` (`35-42`): UUID check → false; `DELETE ... WHERE id AND user_id RETURNING id`; true iff a row.
- `createConversation(userId, title?)` (`44-50`): `INSERT (user_id, title) RETURNING *`.
- `ensureConversation(userId, id?, title?)` (`64-81`): if `id` is a UUID: existing own row → return it; else
  `INSERT (id, user_id, title) ON CONFLICT DO NOTHING RETURNING *` → return if inserted; otherwise (no id, bad id,
  or id held by someone else) `createConversation` with a fresh random id. Comment (`52-63`): the client picks the
  id up front so the URL it navigates to is known; claiming can never attach to another user's row.
- `loadMessages(userId, conversationId)` (`86-107`): `SELECT * FROM messages WHERE conversation_id=$1 AND user_id=$2
  ORDER BY created_at ASC` → `[{id, role, parts, metadata: metadata ?? undefined}]`.
- `saveMessages(userId, conversationId, uiMessages)` (`116-148`): no-op on empty list;
  `INSERT INTO messages (id, conversation_id, user_id, role, parts, metadata) VALUES ... ON CONFLICT (id) DO UPDATE SET
  parts = excluded.parts, metadata = excluded.metadata`; then `UPDATE conversations SET updated_at=now() WHERE id=$conv`
  (note: the update is not user-scoped, and the upsert's conflict branch does **not** check user/conversation — see hazards).
  Why upsert (comment `109-115`): an agent that takes several steps can persist the same message id more than once;
  the later write must update.

### 2.3 Persisted `UIMessage` JSON shapes (exact, from capture)
Message: `{"id": str, "role": "user"|"assistant", "parts": [...], "metadata"?: any}` (metadata stored in the column).

Parts written by this app:
- User text (client): `{"type":"text","text":"..."}` (no `state`).
- Step boundary: `{"type":"step-start"}` (one per agent step, first part of every assistant message).
- Assistant text: `{"type":"text","text":"...","state":"done"}` plus optional `"providerMetadata": {...}` (e.g. OpenAI
  `{"openai":{"itemId":"msg_..."}}`) — opaque, replayed to the provider by the TS SDK.
- Reasoning (streamed because `sendReasoning` defaults true): `{"type":"reasoning","id"?:str,"text":str,"state":"done",
  "providerMetadata"?:{...}}`. Not rendered by the UI.
- Static tool part, type `tool-<toolName>`:
  - success: `{"type":"tool-list_jobs","toolCallId":"call_..","state":"output-available","input":{...},"output":{...}}`
  - tool threw: `{"type":"tool-x","toolCallId":..,"state":"output-error","input":{...},"errorText":"An error occurred."}`
  - invalid model input: `{"type":"tool-x","toolCallId":..,"state":"output-error","rawInput":<raw>,"errorText":"An error occurred."}`
  - interrupted (client stop while tool runs): `{"type":"tool-x","toolCallId":..,"state":"input-available","input":{...}}`
  - optional `callProviderMetadata` / `resultProviderMetadata` / `title` / `providerExecuted` keys may appear.
  Undefined keys are absent (JSON). Tool outputs were serialized with `JSON.stringify`: `Date` → ISO string with ms and
  `Z`, `undefined` properties dropped, top-level `undefined` output → `null` (`ai/src/ui-message-stream/to-ui-message-chunk.ts:281-283`).
- `dynamic-tool` parts are never written by this app, but the SDK converts persisted terminal parts of tools no longer
  registered into `dynamic-tool` in memory during validation (3.7).

Metadata values:
- `null` — normal user messages and chat replies.
- Run update (synthetic user message): `{"kind":"run_update","runId":"<uuid>","eventId":<int>,"label":"<divider text>"}`
  (`src/lib/chat/follow-up.ts:47-53,109-114`).
- Follow-up reply: `{"kind":"follow_up","runId":"<uuid>","eventId":<int>}` (`follow-up.ts:56,143-144`).

---

## 3. Wire protocol: AI SDK v7 UI message stream (what Django must emit byte-compatibly)

### 3.1 Framing (`ai/src/ui-message-stream/json-to-sse-transform-stream.ts:6-17`, `create-ui-message-stream-response.ts:19-44`)
Each chunk is one SSE event: `data: ` + `JSON.stringify(chunk)` + `\n\n`. No `event:`/`id:` lines. After the last chunk:
`data: [DONE]\n\n`. UTF-8 (`TextEncoderStream`). The client parser (`provider-utils/src/parse-json-event-stream.ts:18-32`)
uses `eventsource-parser`, ignores `[DONE]`, and validates every other `data` payload against `uiMessageChunkSchema`
(`ai/src/ui-message-stream/ui-message-chunks.ts:23-220`, `z.looseObject` per type, so extra keys are allowed but
required keys/types and the `type` discriminator must be valid). An invalid chunk aborts the client turn with an error.
Key order is irrelevant. The client does **not** check response headers; it requires `response.ok` and a body
(`ai/src/ui/http-chat-transport.ts:204-218`). On non-2xx it throws `APICallError` whose `message` is the response body
text (or "Failed to fetch the chat response.") (`ai/src/ui/create-ui-api-call-error.ts:3-23`) — that text is what the
chat shows under "That reply did not come through."

### 3.2 Chunk types (complete union, `ui-message-chunks.ts:231-411`)
Required fields in **bold**:
- `start` {messageId?, messageMetadata?}
- `start-step` {} / `finish-step` {} / `reset-step` {}
- `text-start` {**id**, providerMetadata?} / `text-delta` {**id**, **delta**, providerMetadata?} / `text-end` {**id**, providerMetadata?}
- `reasoning-start|reasoning-delta|reasoning-end` (same shapes as text)
- `tool-input-start` {**toolCallId**, **toolName**, providerExecuted?, providerMetadata?, toolMetadata?, dynamic?, title?}
- `tool-input-delta` {**toolCallId**, **inputTextDelta**}
- `tool-input-available` {**toolCallId**, **toolName**, **input**, ...same optionals}
- `tool-input-error` {**toolCallId**, **toolName**, **input**, **errorText**, ...}
- `tool-approval-request` {**approvalId**, **toolCallId**, approvalDescriptor?, reason?, isAutomatic?, signature?}
- `tool-approval-response` {**approvalId**, **approved**, reason?, providerExecuted?, providerMetadata?}
- `tool-output-available` {**toolCallId**, **output**, providerExecuted?, providerMetadata?, toolMetadata?, dynamic?, preliminary?}
- `tool-output-error` {**toolCallId**, **errorText**, ...}
- `tool-output-denied` {**toolCallId**}
- `error` {**errorText**}
- `finish` {finishReason?: "stop"|"length"|"content-filter"|"tool-calls"|"error"|"other", messageMetadata?}
- `abort` {reason?}
- `message-metadata` {**messageMetadata**}
- `data-<name>` {id?, **data**, transient?} (unused here), `source-url`, `source-document`, `file`, `reasoning-file`, `custom` (unused).

### 3.3 Exact stream for a two-step turn (captured, success)
```
data: {"type":"start","messageId":"oYcuWUwQDuwmYJtJ"}

data: {"type":"start-step"}

data: {"type":"text-start","id":"msg_1"}

data: {"type":"text-delta","id":"msg_1","delta":"Checking "}

data: {"type":"text-delta","id":"msg_1","delta":"your runs."}

data: {"type":"text-end","id":"msg_1"}

data: {"type":"tool-input-start","toolCallId":"call_1","toolName":"get_run_status"}

data: {"type":"tool-input-delta","toolCallId":"call_1","inputTextDelta":"{\"run_id\":"}

data: {"type":"tool-input-delta","toolCallId":"call_1","inputTextDelta":"null}"}

data: {"type":"tool-input-available","toolCallId":"call_1","toolName":"get_run_status","input":{"run_id":null}}

data: {"type":"tool-output-available","toolCallId":"call_1","output":{"ok":true,"runId":"1111...","kind":"search","status":"completed","when":"1970-01-01T00:00:00.000Z"}}

data: {"type":"finish-step"}

data: {"type":"start-step"}

data: {"type":"text-start","id":"msg_2"}

data: {"type":"text-delta","id":"msg_2","delta":"Your search is done."}

data: {"type":"text-end","id":"msg_2"}

data: {"type":"finish-step"}

data: {"type":"finish","finishReason":"stop"}

data: [DONE]

```
Persisted `responseMessage` for that stream:
```json
{"id":"oYcuWUwQDuwmYJtJ","role":"assistant","parts":[
 {"type":"step-start"},
 {"type":"text","text":"Checking your runs.","state":"done"},
 {"type":"tool-get_run_status","toolCallId":"call_1","state":"output-available","input":{"run_id":null},"output":{...}},
 {"type":"step-start"},
 {"type":"text","text":"Your search is done.","state":"done"}]}
```
Rules the emitter must follow:
- `start.messageId` **must** be the id that will be persisted (the client adopts it as its assistant message id,
  `ai/src/ui/process-ui-message-stream.ts:913-923`; `chat.tsx` `mergeMessages` dedupes server vs local by id).
  Server-generated id per turn (route passes `generateId`; `get-response-ui-message-id.ts:15-35` would reuse the last
  message id only if the last original message were an assistant message — never the case here).
- One `start-step` … `finish-step` pair per LLM call. `text-start` must precede `text-delta`/`text-end` for the same `id`
  (client throws otherwise, `process-ui-message-stream.ts:442-476`). `tool-output-*` requires a prior
  `tool-input-start` or `tool-input-available` for that `toolCallId` (`process-ui-message-stream.ts:131-159`).
  `tool-input-start`/`-delta` are optional (they only make the part appear earlier in `input-streaming` state).
  Text ids are provider item ids (e.g. OpenAI `msg_...`); any unique string works.
- Tool results are emitted inside the step that called them, before that step's `finish-step`.
- Final `finish.finishReason` = last step's finish reason (`"stop"` normally; `"tool-calls"` if the 8-step cap was hit
  on a tool step; `"error"` after a provider error).
- `tool-input-end` provider events are dropped (`to-ui-message-chunk.ts:378-381`).
- Reasoning chunks are forwarded (`sendReasoning` default true). The UI ignores reasoning parts, so a port may omit them.

### 3.4 Errors (captured)
- Tool `execute` throws → `{"type":"tool-output-error","toolCallId":"call_1","errorText":"An error occurred."}`; the loop
  continues; the **model** sees the real message as an `error-text` tool result `"Error: db down"` (i.e. `String(error)`).
  UI and persistence see the masked text only.
- Invalid tool input (fails zod) → `tool-input-error` with `errorText:"An error occurred."`
  (`to-ui-message-chunk.ts:209-231`); the model gets the validation error and may retry next step.
- Provider error mid-turn (captured): `…{"type":"start-step"}`, `{"type":"error","errorText":"An error occurred."}`,
  `{"type":"finish-step"}`, `{"type":"finish","finishReason":"error"}`, `[DONE]`. A thrown setup error yields
  `error` and close without finish (`ai/src/generate-text/stream-text.ts:3160-3176`). The masking comes from the default
  `onError = () => 'An error occurred.'` (`to-ui-message-chunk.ts:41`, `to-ui-message-stream.ts:29`).
  Client: an `error` chunk throws inside the client's stream processor → `status: 'error'`, `error.message` =
  errorText (`ai/src/ui/chat.ts:806-873`), the streamed partial assistant message stays on screen.
- Server-side `onEnd` still runs and persists the partial message (outcome `completed`/`failed`).

### 3.5 Client stop / disconnect (captured)
`useChat.stop()` aborts the fetch. The response body cancel propagates through the TransformStream chain:
`handleUIMessageStreamFinish`'s `cancel` calls `onEnd` (`ai/src/ui-message-stream/handle-ui-message-stream-finish.ts:216-223`)
with the partial message (`isAborted:false`, `outcome:"unknown"` in capture), and the source reader is cancelled
(`to-ui-message-stream.ts:102-113`). Observed: **a tool already executing keeps running to completion** (side effects
happen, e.g. a run is created and queued) but its result is never streamed or persisted; no further LLM call happens;
the persisted part is left in `state:"input-available"`. On the next turn that dangling call is sent to the provider as a
`tool-call` with **no tool result** (captured via `convertToModelMessages`, `ignoreIncompleteToolCalls` is not set by
`createAgentUIStream`, `ai/src/agent/create-agent-ui-stream.ts:92-94`) — OpenAI/Anthropic reject such histories. Latent bug;
the port should drop or synthesize results for incomplete tool calls.

### 3.6 `onEnd` ordering guarantee
`flush()` awaits `callOnEnd()` (which awaits `saveMessages`) before the stream closes, and `[DONE]` is written in the SSE
transformer's flush after that (`handle-ui-message-stream-finish.ts:221-223`, `json-to-sse-transform-stream.ts:12-14`).
So **the assistant row is in Postgres before the client sees end-of-stream**. `follow-up.ts:replyInFlight` relies on
"reply is saved when the turn ends".

### 3.7 History validation and conversion (before the first token)
`createAgentUIStream` (`ai/src/agent/create-agent-ui-stream.ts:80-117`):
1. `validateUIMessagesForAgent({messages: uiMessages, tools: agent.tools})` (`ai/src/ui/validate-ui-messages.ts:698-711`):
   schema check (non-empty list; each message `{id:string, role in system|user|assistant, metadata?, parts:[...]}`;
   non-assistant messages need ≥1 part); for each `tool-*` part: unknown tool + terminal state → converted to
   `dynamic-tool`; unknown tool + non-terminal → error; `output-available` input must validate against the tool's
   current zod input schema (empty `{}` input is tolerated by converting to dynamic) else **throws** (→ 500);
   `input-available`/`approval-*`/`output-denied` input must validate else throws; `output-error` invalid input →
   converted to dynamic. Consequence: changing a tool's input schema incompatibly can brick old conversations.
2. `convertToModelMessages(validated, {tools})` (`ai/src/ui/convert-to-model-messages.ts:48-454`):
   - user → `{role:'user', content:[{type:'text',text}...]}` (metadata ignored — run updates look like normal user text).
   - assistant parts are split into blocks at each `step-start`; each block → one `assistant` message (text,
     reasoning with `providerOptions = providerMetadata`, `tool-call` for every tool part not in `input-streaming`)
     followed by a `tool` message with `tool-result` for `output-available` (`{type:'json',value}` or `{type:'text'}` for
     string outputs) and `output-error` (`{type:'error-text', value: errorText}`), via
     `ai/src/prompt/create-tool-model-output.ts:4-30`. `input-available` parts produce a tool-call without result (3.5).
3. `agent.stream({prompt: modelMessages})`; `originalMessages` = the route's raw `uiMessages`.

### 3.8 Tool definitions as sent to the provider (captured format)
Each tool: `{"type":"function","name":"<tool>","description":"<desc>","inputSchema":<JSON Schema draft-07>}`.
zod→JSON Schema conventions (captured): every property listed in `required`, `additionalProperties:false`,
`.nullable()` scalar → `{"type":["string","null"]}`, nullable array/object/enum → `{"anyOf":[{...},{"type":"null"}]}`,
`.int().min(a).max(b)` → `{"type":"integer","minimum":a,"maximum":b}`, `.describe(s)` → `"description"`.
Empty object → `{"type":"object","properties":{},"additionalProperties":false}`. No `strict` flag is set on tools
(OpenAI provider forwards `strict` only when the tool defines it).

### 3.9 Tool approvals (AI SDK protocol) — NOT used
No tool sets `needsApproval`, the agent sets no `toolApproval`, the client never calls `addToolApprovalResponse` and sets no
`sendAutomaticallyWhen` (grep). The tool-part renderer handles `approval-requested`/`approval-responded`/`output-denied`
only generically (falls into the "done" activity line, `src/components/chat/tool-part.tsx:84-86`). For reference, the
protocol would be: server emits `tool-approval-request {approvalId, toolCallId}` (client sets part `state:'approval-requested',
approval:{id}`), client `addToolApprovalResponse({id, approved, reason})` mutates the part to `approval-responded`
(`ai/src/ui/chat.ts:507-561`) and, with `sendAutomaticallyWhen`, re-POSTs; with this app's transport that body would be
`{id, message: <last assistant message>}` — which the current route would mishandle (it appends `incoming` to history that
already holds that id). Product approvals (deep-eval batch, shortlist) go through `answer_approval` / the UI card instead.

### 3.10 Resume / reconnect — not used
`useChat` `resume` defaults false (`node_modules/@ai-sdk/react/src/use-chat.ts:68-74,233-236`); no
`GET /api/chat/:id/stream` exists or is needed.

---

## 4. Client side (unchanged by the port; Django must satisfy it)

### 4.1 `Chat` component (`src/components/chat/chat.tsx`)
- `useChat<ForayUIMessage>({ id: chatId, messages: initialMessages, transport: new DefaultChatTransport({ api: "/api/chat",
  prepareSendMessagesRequest: ({id, messages}) => ({ body: { id, message: messages.at(-1) } }) }), onFinish })`
  (`chat.tsx:67-89`). Path is relative: Django must be reachable at same-origin `/api/chat` (proxy/rewrite) with the
  session cookie.
- `onFinish` (`77-88`, runs after success, error **and** abort): `requestLiveRefresh()`; if the chat was new:
  `history.replaceState(null,"","/chat/<chatId>")`, `setPersisted(true)`, `announceConversationsChanged()`.
  (Uses the client's `chatId`, not `x-conversation-id`.)
- `send(prompt)` (`143-149`): trim, ignore when busy, `clearError()`, `sendMessage({text})`. The SDK pushes
  `{id: generateId(), role:'user', parts:[{type:'text', text}]}` (`ai/src/ui/chat.ts:394-447`).
- Pending prompt hand-off (`155-174`): on a new chat, after a 0 ms timer, reads and removes
  `sessionStorage["foray:pending-prompt"]` (`src/components/home/ask-box.tsx:10`) and sends it.
- `busy = status in {submitted, streaming}`; "Thinking…" shown while `submitted`, or streaming with no text/tool part yet
  (`176-181`).
- Error banner (`231-255`): shows `error.message` (server text or "An error occurred.") and "Try again" →
  `clearError(); regenerate()`. `regenerate()` drops the last assistant message client-side (if any) and re-POSTs with
  `message` = the last user message (same id) (`ai/src/ui/chat.ts:455-487`). The server then saves it again (upsert no-op)
  and builds `[...history, incoming]` where history **already contains that user message and any partial assistant reply**
  → the model sees the user message twice. Port should dedupe `incoming.id` against history (and probably drop a trailing
  partial assistant message on regenerate).
- Follow-up integration (`93-141`):
  - `runIdsIn(messages)` (`274-285`): every `output.runId` string from assistant static tool parts in `output-available`.
  - `watchRuns(runIds)` suppresses toasts for those runs (`src/components/live/live-provider.tsx:197-209`).
  - On any live event whose `runId ∈ runIds` and `kind ∈ FOLLOW_UP_KINDS` → `catchingUp=true` ("Foray is looking at the
    results…"), auto-cleared after 90 000 ms (`112-121`).
  - On `chat.followup` with `data.conversationId === chatId` → `catchingUp=false`; if `data.messageId` → `reload()` now,
    or after the current stream ends (`123-137`). `reload()` = `GET /api/conversations/<chatId>` then
    `mergeMessages(server, local)` = server list + local messages whose id the server lacks (`293-296`).
- Rendering: `runUpdateLabel` (`299-303`) — a `user` message with `metadata.kind === "run_update"` renders as a divider
  with `metadata.label` (fallback "Update"); user messages render text parts joined by `\n`; assistant messages render
  text parts (Markdown via Streamdown `MessageResponse`) and static tool parts via `ToolPart`; everything else ignored
  (`326-367`). Header title = `title ?? firstUserText(messages)` (first non-run-update user text, 80 chars) (`186-188, 320-324`).

### 4.2 `ToolPart` (`src/components/chat/tool-part.tsx:66-150`) — output contract per tool
- `input-streaming`/`input-available` → "<running copy>…" pending line; `output-error` → "<running> failed" + `errorText`;
  other non-output states → done line. COPY table `40-57`.
- `output.ok === false` → warning line with `output.message` (string) else "<running> did not work" (`91-98`).
  (`update_profile_preferences` failure returns `error`, not `message`, so it shows "Saving what you told me did not work".)
- `typeof output.runId === "string"` → `<RunCard runId>` (live card via `useRun`, `/api/runs/:id`) (`101-103`). This is why
  `start_search_run`, `evaluate_job`, `tailor_resume` return `runId` (and `get_run_status` too, which therefore renders
  as a card).
- `list_jobs` → `output.jobs[]` rendered as `MatchLine`s, fields `matchId,title,company,locations,remote,score|deepScore|fastScore,
  deepScore,recommendation,salary,postedAt,stage,headline` (`206-253`); empty → "No matches fit that yet."
- `get_job` → reads `output.matchId,title,company,locations,remote,salary,postedAt,match.{deepScore,fastScore,deepEval.recommendation,stage}` (`255-278`).
- `update_profile_preferences` → labels from non-null **input** keys + `output.saved` (expandable) + "Ready to search." when
  `output.blockingSearch` is empty and `output.ok` (`292-310`).
- `add_company` → `output.company`, `output.source` (first `_`→space) (`312-325`).
- `discover_companies` → `output.queued[]` (`114-124`).
- `list_pending_approvals` → `output.approvals[].id` intersected with live pending approvals → `<ApprovalCard>`s; none listed →
  "Nothing is waiting on you."; all answered since → renders nothing (`327-345`).
- `move_job_stage`, `answer_approval` → `output.message` (`127-146`). Default → done line with raw output expandable.

### 4.3 Other client pieces
- `AskBox` / `startChatWith(router, prompt)` (`src/components/home/ask-box.tsx:16-23`): `sessionStorage.setItem("foray:pending-prompt", prompt)`
  then `router.push("/chat")` (prompt never in URL). Used on Home with suggestions.
- `OnboardingChat` (`src/components/home/onboarding-chat.tsx:11-23`): button → `startChatWith(router, "Let's set up my job search. Ask me what you need to know.")`.
- `src/components/ai-elements/{conversation,message,code-block}.tsx`: pure presentational (StickToBottom scroll container,
  Streamdown markdown with cjk/code/math/mermaid plugins, Shiki code blocks). No server calls.
- `ForayUIMessage` type is imported by client code from the server module (`chat.tsx:33`, `page.tsx:5`,
  `src/lib/agents/chat-agent.ts:114-116`, `InferAgentUIMessage<chatAgent>`): `UIMessage<unknown /*metadata*/, never /*data*/,
  {<16 tool names>: {input, output}}>`. After the port the frontend needs a hand-written equivalent.
- `FOLLOW_UP_KINDS`, `FOLLOW_UP_EVENT` are imported client-side from `src/lib/chat/follow-up-kinds.ts` (no server deps; keep
  as a shared constant, Django must use identical strings).

### 4.4 What the client tolerates
- Any 2xx with a readable body; non-2xx → error banner with body text.
- Missing headers are fine; but proxies must not buffer/compress the SSE (keep `x-accel-buffering: no`, disable gzip).

---

## 5. Server modules

### 5.1 `chatAgent(userId, {conversationId?, userName?, followUp?})` (`src/lib/agents/chat-agent.ts:43-97`)
- Reads the profile row: `SELECT * FROM profiles WHERE user_id=$1 LIMIT 1` (`55-59`).
- `new ToolLoopAgent({...})`:
  - `model: model("chat")` (`62`) — `LLM_CHAT ?? LLM_DEFAULT` env `provider:model` (dev: `openai:gpt-5.6-luna`, OpenAI
    Responses API), wrapped in ledger middleware (`src/lib/llm/models.ts:124-132`). **Not** passed: `reasoning`
    (so `REASONING_DEFAULTS.chat = "low"` / `LLM_REASONING_CHAT` do not affect the agent — provider default applies),
    temperature, the per-provider rate limiter (`acquire` is only in `callLLM`), `maxRetries` (SDK default 2).
  - `instructions: systemPrompt({ hasProfile: Boolean(profile), preferences: PreferencesSchema.safeParse(profile?.preferences).data
    ?? EMPTY_PREFERENCES, interviewState: InterviewStateSchema.safeParse(profile?.interviewState).data ?? EMPTY_INTERVIEW_STATE,
    settings: profile ? parseSettings(profile.settings) : null, userName: options.userName ?? null })` (`63-73`).
  - `tools` (`74-91`): `get_profile_summary, get_resume, update_profile_preferences, add_company, discover_companies,
    list_companies, start_search_run(userId, conversationId), get_run_status, cancel_run, list_jobs, get_job,
    list_pending_approvals, answer_approval, move_job_stage, evaluate_job(userId, conversationId), tailor_resume(userId, conversationId)`.
    Insertion order above = order sent to the model.
  - `activeTools: FOLLOW_UP_TOOLS` only when `followUp` (`94`); `FOLLOW_UP_TOOLS = ["get_profile_summary","get_resume",
    "get_run_status","list_jobs","get_job","list_companies"]` (`100-107`). Inactive tools are still registered (history
    validation still knows them) but not offered to the model. Why: "A follow-up reports and asks; it must not start,
    approve or change anything the user has not asked for in their own words" (`92-93`).
  - `stopWhen: isStepCount(followUp ? 4 : 8)` (`95`). `isStepCount(n)` = `steps.length === n`
    (`ai/src/generate-text/stop-condition.ts`); default would be 20 (`ai/src/agent/tool-loop-agent.ts:132`). The loop also
    stops when a step has no client tool calls. At the cap, tools of the last step still execute and stream; no final
    text follows. Why (`35-42`): a chat turn runs inside a request, so a non-terminating loop must be bounded.
- Tools close over `userId` — "a tool can never be talked into reading someone else's row — the model has no way to name a
  different user" (`src/lib/agents/tools/get-profile-summary.ts:8-14`).

### 5.2 System prompt (`src/lib/agents/system-prompt.ts`)
Built per request (why, `9-16`: an agent that cannot see stored answers re-asks them).
- `BASE` constant `18-37`: identity ("You are Foray, a job-search agent working for one person…"), behaviour rules: brevity,
  check `get_profile_summary`/`get_resume` before asking, never invent facts, background work shows as a live card and the
  agent says what/how long and that it will report back (don't pretend to wait, don't repeat card numbers), messages
  starting `"[Automatic update about run"` are Foray's own updates (report back as asked), use `list_jobs`/`get_job` for real
  cards, plain-text formatting (no tables/headings for short answers, no emoji).
- `capabilities(settings)` `39-85`: `mode = settings?.defaultMode === "autopilot" ? "autopilot" : "approve_each"`; interpolates
  `settings?.deepEvalTopN ?? 15` and the mode sentence (`47-48`); documents every tool and the "search follows the request"
  brief rules (`50-55`), approvals (`57-59`), matches (`61-70`), resume (`72-76`), companies (`78-81`), and what it cannot do (`83-84`).
- `systemPrompt(input)` `87-151`:
  - `who = userName ? "\nThe user's name is <name>." : ""` (`94`).
  - `today = "\nToday is " + new Date().toLocaleDateString("en-US",{weekday:"long",year:"numeric",month:"long",day:"numeric"})`
    → e.g. "Sunday, October 4, 2026", in the **Node process's local time zone** (`95`).
  - No profile row → `BASE+who+today+"## Right now"` (ask to upload resume on Home or Profile, PDF/Word) `+capabilities` (`97-105`).
  - `progress = interviewProgress(preferences, interviewState)`; if `progress.complete || progress.blocking.length === 0` →
    "## What they are looking for" + `describePreferences(preferences)` + "do not re-interview… update_profile_preferences (only the
    fields that changed)" + optional line `Not yet known (optional, raise naturally only if relevant): <labels lowercased, ", ">.`
    when optional outstanding slots exist (`110-124`).
  - Else "## The interview": "Already recorded:" describePreferences; "Still to find out:" one line per outstanding slot
    `- <label>[ (needed to search)]: <intent>`; interview rules (2–3 items at a time, save immediately with only answered
    fields, `declined_slots`, infer implied answers and confirm, follow tangents, offer first search); final line
    `A search cannot start until these are known: <blocking labels, ", ">.` (`127-150`).
- Dependencies (`src/lib/profile/interview.ts`): `SLOTS` (`99-162`: ids/labels/intents/required: target_roles, seniority,
  locations, remote_pref, work_auth required; salary_floor, start_date, constraints[must_haves,dealbreakers] optional),
  `isFilled` = **some** field of the slot non-null and (if array) non-empty (`179-186`), `interviewProgress` (`199-223`:
  outstanding = not filled and not skipped; blocking = required and not filled; answered = 8 − outstanding;
  complete = outstanding empty), `describePreferences` = `- <field>: <JSON.stringify(value)>` per non-empty field in slot
  order, or `- nothing recorded yet` (`226-237`). `PreferencesSchema` fields are each `.nullable().default(null)` (`43-56`);
  `InterviewStateSchema = {version: number default 1, skipped: string[] default []}` (`165-169`). Settings
  (`src/lib/profile/settings.ts:18-36`): `defaultMode ∈ {approve_each, autopilot}` default approve_each, `scoreThreshold` int 0–100
  default 55, `deepEvalTopN` int 0–60 default 15; unparseable → defaults.

### 5.3 Tools (each `tool({description, inputSchema, execute})`; none has `needsApproval`/`toModelOutput`/`outputSchema`)
Descriptions are by file:line; input schemas field-level. "Output" is the JSON returned (becomes `tool-output-available.output`).

1. **get_profile_summary** (`tools/get-profile-summary.ts:15-52`). Input `{}`. Reads profile row. No profile →
   `{hasProfile:false, note:"No profile yet. The user still needs to upload a resume."}`. Else
   `{hasProfile:true, counts:{work, education, skills, projects}` (lengths of `profile.data.*` arrays, 0 if absent),
   `preferences: profile.preferences ?? {}` (raw jsonb), `knownPreferences: Object.keys(preferences)` (includes keys whose value is null)}.
   Follow-up allowed.
2. **get_resume** (`tools/resume.ts:21-67`). Input `{sections: ("basics"|"summary"|"work"|"education"|"projects"|"skills")[] | null}`
   (desc `33-36`). `getBaseResume(userId)` = latest `resumes` row `kind='base'` by `version desc` (`src/lib/db/queries/profile.ts:20-28`).
   None → `{ok:false,message:"No resume on file. The user needs to upload one on the profile page."}`; `ResumeSchema.safeParse(stored.data)`
   fails → `{ok:false,message:"The stored resume could not be read; it may need re-uploading."}`; else
   `{ok:true, version: stored.version, resume: resumeToText(parsed, sections?.length ? sections : ALL)}`.
   `resumeToText` (`src/lib/resume/text.ts:23-117`) renders prose with `[id]` tags per item (format spelled out there:
   `Name:`/`Headline:`/`Location:`/`Email:`/`Links:` lines; `SUMMARY`, `EXPERIENCE` (`[id] Position — Company (location)`,
   `  start – end|present · source: <source>`, `  summary`, `  • highlight`), `EDUCATION`, `PROJECTS`, `SKILLS` grouped by
   category ("Other" default); sections joined by blank lines). Follow-up allowed.
3. **update_profile_preferences** (`tools/update-profile-preferences.ts:28-112`). Input (all required-nullable; null = "leave
   stored value alone", comment `20-27`): `target_roles: string[]|null, seniority: enum(intern,new_grad,entry,mid,senior,staff_plus)|null,
   locations: string[]|null, remote_pref: enum(remote,hybrid,onsite,any)|null, salary_floor: {amount:number,currency:string,period:enum(year,hour)}|null,
   work_auth: string|null, start_date: string|null, must_haves: string[]|null, dealbreakers: string[]|null,
   declined_slots: enum(target_roles,seniority,locations,remote_pref,work_auth,salary_floor,start_date,constraints)[]|null`.
   Execute: no profile → `{ok:false, error:"No profile yet — the user needs to upload a resume before preferences can be saved."}`;
   `merged = {...current, ...answered}` where answered = keys of EMPTY_PREFERENCES whose input is non-null (an empty array
   **does** overwrite); `nextState = {...state, skipped: unique([...state.skipped, ...declined_slots])}`;
   `UPDATE profiles SET preferences=$merged, interview_state=$nextState, updated_at=now() WHERE user_id=$1`; returns
   `{ok:true, saved: merged, answered, total, stillNeeded: outstanding labels, blockingSearch: blocking labels}`.
4. **add_company** (`tools/companies.ts:18-56`). Input `{name_or_url: string}` (desc `26-27`). Runs **inline** (one company,
   user waiting) `discoverCompany({nameOrUrl, addedByUserId:userId, discoveredVia:"user"})` (`src/lib/companies/discovery.ts:189+`,
   companies area; makes live HTTP calls with 12 s timeouts, optional Brave search). Not registered →
   `{ok:false, message:'No job board found for "<input>".', tried: trail[{step,detail}]}`; else `{ok:true, company:name,
   source: sourceType, slug: atsSlug, needsBrowser, message:"Registered <name>. Its postings will be pulled in on the next refresh."}`.
5. **discover_companies** (`companies.ts:58-112`). Input `{description: string, count: int 1..25}`. Calls
   `generateStructured("chat", ProposedCompanies, {instructions: <companies.ts:89-92>, prompt: "Name up to <count> companies matching: <description>"}, {userId})`
   (`src/lib/llm/structured.ts:63-93`: `callLLM` with rate limiter + reasoning `low` for chat, `Output.object`, one retry on
   malformed/no output). Schema `ProposedCompanies = {companies: [{name: string, why: string}]}`. `names = companies.slice(0,count).map(name)`;
   empty → `{ok:false, message:"Could not think of matching companies."}`; else `send("company.discover", {userId, names})` and
   `{ok:true, queued: names, message:"Looking for job boards for <n> companies: <names, ", ">. Progress will appear in the activity feed."}`.
   (Nested LLM call inside a tool: ledgered purpose `chat`.)
6. **list_companies** (`companies.ts:114-139`, factory takes no userId — shared table). Input `{limit: int 1..50 | null}`.
   `countByStatus()` (`SELECT status, count(*)::int FROM companies GROUP BY status`) and `listCompanies({status:'active', limit: limit ?? 20})`
   (order by name asc). Output `{counts:{<status>:<n>}, sample:[{name, source: sourceType, lastFetched: lastFetchedAt, jobs: lastFetchJobCount}]}`.
   Follow-up allowed.
7. **start_search_run** (`tools/search.ts:39-149`). Input (desc per field `52-89`): `prompt: string` (user's words),
   `companies: string[]|null`, `roles: string[]|null`, `seniority: Seniority|null`, `locations: string[]|null`,
   `remote: RemotePreference|null`, `focus: string|null`, `mode: enum(approve_each, autopilot)|null`.
   Execute:
   - `assertCanSearch(userId)` (`src/lib/runs/search-run.ts:122-138`): blocking slots → `PreferencesIncompleteError(missing labels)`
     → return `{ok:false, needs: missing, message:"Cannot search yet — still need: <labels, ", ">. Ask for those first, then start the search."}`.
     Why (`31-37`): a search on incomplete preferences "looks plausible and is mostly unusable".
   - `activeSearch(userId)` (`src/lib/runs/runs.ts:40-54`: latest `runs` row kind `search` with status in queued/running/awaiting_approval)
     → return `{ok:true, runId, mode, alreadyRunning:true, message: awaiting_approval ? "A search is already waiting on the user's go-ahead; this is its card. Do not start another." : "A search is already running; this is its card. Do not start another."}`.
   - `resolvedMode = mode ?? getSettings(userId).defaultMode`; `createRun({userId, kind:"search", mode, status:"queued",
     input:{prompt, hints: cleanBrief(hints)}, conversationId: conversationId ?? null})`; `send("search", {userId, runId})`.
     `cleanBrief` (`src/lib/ranking/brief.ts:65-86`): trim strings, drop blanks and case-insensitive duplicates, empty list → null,
     `focus.trim() || null`.
   - Output `{ok:true, runId, mode, searchingFor: describeBrief(cleanBrief(hints)) /* string|null, brief.ts:151-168 */,
     message: autopilot ? "Search started on autopilot. It will score, evaluate and shortlist without stopping." :
     "Search started. It will pause for approval before the deep evaluation and before shortlisting."}`.
8. **get_run_status** (`search.ts:151-176`). Input `{run_id: string|null}`. `getRun(userId, id)` (null for non-UUID) or most
   recent run of any kind (`listRuns(userId,1)`). None → `{ok:false,message:"No runs yet."}`; else
   `{ok:true, runId, kind, status, mode, stats, output, error}`. Follow-up allowed. (Renders as a RunCard because of `runId`.)
9. **cancel_run** (`search.ts:178-189`). Input `{run_id: string}`. `requestCancel(userId, id)` (`runs.ts:113-141`: conditional
   update to `cancelled` from queued/running/awaiting_approval, expires the run's pending approvals and emits `run.cancelled` warn
   if any). → `{ok:true, message:"Cancelled. Everything found so far was kept."}` | `{ok:false, message:"That run is not running."}`.
10. **list_jobs** (`search.ts:226-267`). Input `{run_id: string|null, limit: int 1..50|null, stage: enum(found,shortlisted,applied,
    interviewing,offer,rejected,archived)|null, min_score: int 0..100|null, query: string|null}`. If `run_id`: `getRun`; missing or
    `kind !== "search"` → `{ok:false, message:"No such search run."}`; `jobIds = run.input.candidateJobIds` (strings only, else []).
    `listMatches(userId, {jobIds, limit: limit ?? 10, stages: stage ? [stage] : ["found","shortlisted","applied","interviewing","offer"],
    minScore, query})` (`src/lib/db/queries/matches.ts:104-141`: join `job_matches`↔`jobs`, `coalesce(deep_score, fast_score)` as score,
    `jobIds=[]` → empty result, `ILIKE %q%` on title/company, order `score desc nulls last, posted_at desc nulls last`).
    Output `{count, jobs: JobCard[]}`. `jobCard` (`192-210`): `{matchId, jobId, title, company, locations, remote, stage, score,
    fastScore, deepScore, recommendation (deep_eval->>'recommendation'), headline (coalesce(deep_eval->>'summary', fast_eval->'fit_reasons'->>0)),
    salary: formatSalary, postedAt, url}`. `formatSalary` (`214-224`): null if neither min/max truthy; `fmt(n) = n>=1000 ? round(n/1000)+"k" : String(round(n))`;
    range `min–max` (en dash) when both and different else single; currency `$` if absent or USD else `"<CUR> "`; suffix
    `/<period>` unless period null or `year`. Follow-up allowed.
11. **get_job** (`search.ts:269-322`). Input `{id: string}` (match id or job id). `resolveMatch` (UUID → own match by id, else by
    job id) (`matches.ts:197-213`); `jobId = match?.jobId ?? id`; `SELECT * FROM jobs WHERE id=$1`; missing → `{ok:false,message:"No such job."}`.
    `own = match ?? getMatchByJob`. Output `{ok:true, matchId|null, jobId, title, company: companyName, locations, remote,
    salary: (min||max) ? "<cur or ''> <min ?? '?'>–<max ?? '?'> per <period ?? '?'>".trim() : null /* numeric columns arrive as strings */,
    postedAt, isOpen: isActive, url, applyUrl, match: own ? {stage, fastScore, fastEval, deepScore, deepEval, notes, tailoredResumeId} : null,
    description: descriptionText?.slice(0, 8000) ?? null}` (truncation comment `317`). Follow-up allowed.
12. **list_pending_approvals** (`tools/approvals.ts:32-60`). Input `{}`. `listPendingApprovals(userId)` (`src/lib/runs/approvals.ts:459-486`:
    expires stale ones first; pending, unexpired, run status active; newest first). Output `{approvals:[{id, kind, title, runId,
    createdAt, items: payload.items ?? [], estimate: payload.estimate ?? null}]}`. Item shapes: deep_eval_batch
    `{matchId, jobId, title, company, fastScore}`, shortlist_proposal `{matchId, jobId, title, company, deepScore, recommendation, summary}`;
    estimate `{usd, perItemUsd, model}` (`approvals.ts:58-97`).
13. **answer_approval** (`tools/approvals.ts:62-141`). Input `{approval_id: string, approve: boolean, item_ids: string[]|null}`
    (desc `65-72`, `79-81`). Execute: `getApproval` missing → `{ok:false, message:"No approval with that id. Call list_pending_approvals for the current ones."}`.
    If approving with ids: `matchItems` maps model ids onto payload items accepting `matchId` **or** `jobId`, trimmed, payload order
    (`144-150`); none match → `{ok:false, message:"None of those ids are items of this approval, so nothing was answered. Use the matchId values from list_pending_approvals."}`
    (why `93-96`: approving an empty subset would count as an irreversible rejection). `decideApproval(userId, id, {approved, itemIds}, "chat")`
    (`approvals.ts:255-363`, conditional single-winner update, emits `approval.decided`, enqueues the continuation, reverts on enqueue failure).
    Throw → `{ok:false, message:"Could not resume the run just now; the request is still open. Try again."}`; `!ok` →
    `already_decided` "That request was already answered, so nothing changed." / `expired` "That request has expired and can no longer be answered." /
    else "No approval with that id."; ok → `{ok:true, message: confirmation(approval)}` (`152-176`: count = `"<n> role(s)"` or
    `"<chosen> of <total> roles"`; deep_eval_batch approved "Approved deep evaluation of <count>. It runs in the background; the search card shows progress, and the shortlist proposal follows." / rejected "Skipped the deep evaluation. The search will wrap up without it.";
    shortlist approved "Approved: shortlisting <count>." / rejected "Left the shortlist as it was. The search will wrap up."; other "Approved."/"Declined.").
    The description forbids deciding without the user's explicit say (`66-68`; system prompt `57-59`).
14. **move_job_stage** (`tools/matches.ts:31-75`). Input `{id: string, stage: enum(7 stages), note: string|null}`.
    `resolveOrCreateMatch(userId, id.trim())` (`matches.ts:238-251`: resolve, else if a `jobs` row with that id exists, create a
    `found` match `ON CONFLICT DO NOTHING`). None → `{ok:false, matchId:null, stage:null, message:"No role with that id."}`.
    `setMatchStage` (`matches.ts:274-289`: `stage_changed_at` only changes if the stage changes); if `note.trim()` →
    `appendMatchNote` (`567-583`: append with `\n\n` separator, never overwrite). Label from `getMatchRows` → `"<title> at <company>"`
    else "That role". Output `{ok:true, matchId, stage, message:"Moved <label> to <stage>." + (note ? " Note added." : "")}`.
15. **evaluate_job** (`matches.ts:77-125`). Input `{id: string}`. `resolveOrCreateMatch`; none → `{ok:false, matchId:null, message:"No role with that id."}`.
    `startMatchEvaluation(userId, [match.id], {conversationId})` (`src/lib/runs/evaluate-run.ts:515-563`: reuses a queued/running
    `rank_deep` run with identical sorted `input.matchIds`, else creates one with `conversation_id` and sends `match.evaluate`).
    Output `{ok:true, runId, matchId, message: reused ? "<label> is already being evaluated; the card shows its progress." :
    "Evaluating <label> in the background" + (cost ? " (<cost>)" : "") + ". The card shows progress; the result is saved on the role."}`
    with `cost = costPhrase(estimateEvalCost(1).perItemUsd)` ("under a cent" / "about $X.XX" / null; `src/lib/ranking/deep-eval.ts:453-509`).
    `NoResumeError`/`UnknownMatchError` → `{ok:false, matchId, message: error.message}`; other errors rethrown (→ tool-output-error).
16. **tailor_resume** (`tools/tailor.ts:13-35`). Input `{id: string}`. `startTailorRun(userId, id, {conversationId})`
    (`src/lib/runs/tailor-run.ts:289-356`: no base resume → message; resolve/ensure match; reuse an active tailor run for the same
    match created < 15 min ago; else create `tailor` run `input:{matchId, prompt:"Tailor your resume for <title> at <company>"}` and send
    `resume.tailor`). Output `{ok:false, message}` | `{ok:true, runId, matchId, message}`.

Runs created by tools 7, 15, 16 carry `conversation_id` → they report back in that chat (section 6).

---

## 6. Follow-up turns (headless agent)

### 6.1 Trigger (`src/lib/runs/events.ts:28-74`, `src/lib/chat/follow-up-kinds.ts:10-22`)
`emit(input)` inserts an `agent_events` row; if `event.runId && FOLLOW_UP_KINDS.has(event.kind)` with
`FOLLOW_UP_KINDS = {approval.requested, run.completed, run.failed, tailor.done, tailor.failed}` it calls `queueFollowUp`:
`SELECT conversation_id FROM runs WHERE id=$runId`; if set, `send("chat.follow_up", {userId, runId, eventId: event.id},
{singletonKey: "follow-up-<eventId>"})`. Errors are logged and swallowed ("must never fail the run").
Queue policy: `retryLimit: 0, expireInSeconds: 300` (`src/lib/queue/boss.ts:53-55`, why: a retried follow-up would report stale
news); worker registration `concurrency: 2`, `batchSize: 1` (`src/worker/main.ts:64,73-86`). Handler
`handleChatFollowUp(jobs)` loops jobs sequentially, `console.log("[worker] chat follow-up for run <id>: <posted|skipped>")`
(`src/worker/handlers/chat.ts:7-12`). Payload type `ChatFollowUpPayload {userId, runId, eventId:number}` (`src/lib/queue/queues.ts:92-97`).

### 6.2 `runChatFollowUp(payload) → "posted" | "skipped"` (`src/lib/chat/follow-up.ts:63-171`)
1. `event = SELECT * FROM agent_events WHERE id=$eventId AND user_id=$userId`; `run = getRun(userId, runId)`; no event or no
   `run.conversationId` → `"skipped"` (no announce) (`68-74`). `conversation = getConversation` missing → `"skipped"` (`76-77`).
2. `skip()` = `announce({skipped:true})` then `"skipped"` (`79-83`). Skip when: `run.status === "cancelled"` (`88`);
   `event.kind === "approval.requested"` and the approval (`getApproval(userId, event.approvalId)`) is not `pending` (`89-90`);
   `alreadyFollowedUp(conversationId, eventId)` (`91`, `205-218`: exists message in the conversation with
   `metadata->>'kind' = 'run_update' AND (metadata->>'eventId')::bigint = $eventId`).
3. Wait for an in-flight chat reply: `while (replyInFlight(conversationId) && now < start+90_000) sleep(1_500)` (`99-102`;
   `REPLY_WAIT_MS = 90_000`, `REPLY_POLL_MS = 1_500`, `60-61`). Why (`93-98`): the user's own message can finish the run
   ("yes, shortlist both"); writing now would put the update above a reply still streaming. Waits in-process rather than
   re-queueing because delayed jobs wait for pg-boss's slow poll (+30 s).
   `replyInFlight(conversationId)` (`193-203`): last message by `created_at desc`; true iff `role === "user"`, `metadata.kind !== "run_update"`,
   and `now - created_at < 90_000 ms`.
4. `proposed = approval ? proposedRoles(userId, approval) : []` (`107`, `173-186`): first 10 `payloadItemIds(approval.payload)`
   (`approvals.ts:401-410`: `matchId` else `id`, deduped), `getMatchRows`, re-sorted to payload order, mapped to
   `{title: title.trim(), company, score: deepScore ?? fastScore, headline, locations: locations.slice(0,3).join(", ")}`.
5. `update = describeUpdate(run, event, approval, proposed)` (6.3). `notice = {id: generateId(), role:"user", parts:[{type:"text",
   text: update.text}], metadata:{kind:"run_update", runId, eventId, label: update.label}}` (`108-114`).
6. `history = loadMessages(...)` (before the notice), then `saveMessages([notice])` (`118-119`).
7. Inside try (`121-162`): user name `SELECT name FROM "user" WHERE id=$1`; `agent = chatAgent(userId, {conversationId, userName,
   followUp:true})`; `withLlmContext({userId}, …)`: `stream = createAgentUIStream({agent, uiMessages: [...history, notice],
   originalMessages: same, timeout: 90_000, generateMessageId: generateId, messageMetadata: () => ({kind:"follow_up", runId, eventId}),
   onEnd: ({responseMessage}) => reply = responseMessage ?? null})`; `consumeStream({stream, onError: e => failure = e})`; rethrow
   `failure` (`136-155`). Nobody watches this stream: "reading it to the end is what runs the agent".
   - `timeout: 90_000` = total-ms abort for the whole agent call (`ai/src/prompt/request-options.ts:13-40`). On expiry the stream
     emits `abort`, `onEnd` still yields the partial message.
   - `messageMetadata` makes `start`/`finish` carry the metadata and adds a `message-metadata` chunk after every other chunk
     (`ai/src/ui-message-stream/to-ui-message-stream.ts:120-148`); the reply's `metadata` = `{kind:"follow_up", runId, eventId}`.
   - Provider errors become `error` chunks, which do **not** throw out of `consumeStream`; they surface as an empty reply.
8. `hasContent(reply)` = some part is non-blank text or `type.startsWith("tool-")` (`220-224`); otherwise throw
   "The follow-up came back empty." (`157-158`). Partial replies (after a timeout or mid-turn error) with any content **are saved**.
9. Success: `saveMessages([reply])`, `announce({messageId: reply.id, label})`, `"posted"` (`160-162`).
10. Catch: log, `DELETE FROM messages WHERE id = notice.id` (why `165-166`: an unanswered update would be answered out of the blue
    by the next turn), `announce({failed:true})`, `"skipped"` (`163-170`).
`announce(userId, conversationId, runId, data)` (`226-239`) → `emit({userId, kind:"chat.followup", level:"debug",
message: data.messageId ? "Foray followed up in chat." : "No chat follow-up was needed.", data:{conversationId, runId, ...data}})`
— **no `runId` column** on that event (so it never triggers another follow-up and is not filtered by `watchRuns`).
Consumers: `chat.tsx:123-131`, `live-provider.tsx:214-226` (toast "Foray followed up in chat: <label lowercased>." with "Open chat",
suppressed when already on `/chat/<id>`; also re-announces the sidebar list).

### 6.3 `describeUpdate(run, event, approval, proposed) → {label, text}` (`follow-up.ts:276-389`, pinned by `src/test/unit/follow-up.test.ts`)
- `what` (`283-288`): search → `describeBrief(parseBrief(input.brief ?? input.hints)) ?? input.prompt ?? "a search"`; tailor →
  `input.prompt?.replace(/^Tailor /, "tailoring ") ?? "tailoring a resume"`; else `input.prompt ?? KIND_NAME[kind] ?? "background work"`
  (`KIND_NAME` `391-397`, `KIND_TITLE` `399-405`).
- `head = "[Automatic update about run <run.id>, not written by the user]"` (`289`); `listIt` (`290`); `RULES` (`266-270`);
  `roleLines` = `- <title> at <company>[ (<score>)][, <locations>][: <headline>]` joined by `\n` (`254-264`). All texts are lines joined by `\n`.
- Cases (label → text lines):
  1. approval.requested + deep_eval_batch → "Search paused for your go-ahead" (`292-305`); omits the roles line when `proposed` empty (filter(Boolean)).
  2. approval.requested + shortlist_proposal → "Deep review done" (`307-323`); items = proposed or payload `items[{title,company}]`.
  3. other approval.requested → "Waiting for your go-ahead" (`325-330`).
  4. run.failed / tailor.failed → `"<KIND_TITLE ?? 'Run'> stopped"` (`332-342`), text uses `capitalize(what)`.
  5. search finished → "Search finished" (`344-359`); empty when `(run.output.candidates ?? 0) === 0` (suggest widening) else
     `listIt` + "Show the best few (limit 5)…".
  6. rank_deep → "Deep review finished" (`361-371`). 7. tailor → "Tailored resume ready" (`373-383`).
  8. fallback → `"<KIND_TITLE ?? 'Run'> finished"` (`385-388`).
  (Read the exact sentences at the cited lines; tests assert substrings such as "do not call list_jobs", "Do not answer the
  approval yourself", "can only look things up", "limit 5", "widen the search", "offer to try again".)

---

## 7. LLM attribution (`src/lib/llm/context.ts`, `ledger.ts`)
`withLlmContext({userId, runId?}, fn)` = `AsyncLocalStorage.run` (`context.ts:20-22`); the ledger middleware reads it on every
`doGenerate`/`doStream` and writes one `llm_calls` row per model call (stream rows written at stream flush using the provider
`finish` usage) (`ledger.ts:89-170`). Chat turns: one row per agent step, `purpose='chat'`, `user_id` set, `run_id` null. Ledger
write failures are swallowed. Port: `contextvars.ContextVar`, propagated into streaming generators and tool threads.

---

## 8. Env vars and constants touching this area
- `LLM_CHAT` / `LLM_DEFAULT` (`provider:model`), provider keys (`OPENAI_API_KEY`, …) via `src/lib/llm/models.ts:46-110`,
  `src/lib/llm/registry.ts:54-75`. `LLM_REASONING_CHAT` affects only `discover_companies`' structured call.
- `DATABASE_URL` (pg-boss + DB).
- Constants: title slice 80; step caps 8 / 4; follow-up timeout 90 000 ms; REPLY_WAIT_MS 90 000; REPLY_POLL_MS 1 500; client
  catch-up fallback 90 000 ms; `get_job` description slice 8000; `list_jobs` default limit 10 (max 50); `list_companies` default 20 (max 50);
  `discover_companies` count 1–25; sidebar list 30 (UI shows 20); follow-up queue expiry 300 s, retryLimit 0, concurrency 2;
  `proposedRoles` max 10; tailor-run dedupe window 15 min; approval TTL 7 days (approvals area).

---

## 9. Hazards for the Django port (details)
1. **Streaming from Django**: needs ASGI (`StreamingHttpResponse` over an async generator under uvicorn/daphne/granian) so one
   long turn does not pin a sync worker; disable `GZipMiddleware`/compression for the route; send `X-Accel-Buffering: no`;
   detect client disconnect (`asyncio.CancelledError` / `request.is_disconnected`) and persist the partial message like `onEnd`
   on cancel; write `[DONE]` only after the assistant row is saved.
2. **Re-implementing the AI SDK agent loop** in Python: step loop with tool execution, `isStepCount` semantics (stop when
   `steps == n`, tools of the last step still run), `activeTools`, zod-equivalent input validation (pydantic) producing
   `tool-input-error`, masked error text to the UI ("An error occurred.") vs `"Error: <msg>"` to the model, JSON
   serialization identical to `JSON.stringify` (datetimes `toISOString()` with ms + `Z`, drop `None`-for-`undefined` keys
   where TS omitted them — note TS keeps explicit `null`s), `output: null` when undefined. The chat UI depends on exact output
   key names (section 4.2).
3. **History compatibility**: existing `messages.parts` rows were written by the TS SDK and may carry OpenAI provider metadata
   (`itemId`, reasoning encrypted content) used for `item_reference` replay; a Python LLM client must either replay or strip it.
   Conversion rules in 3.7 must be reproduced (step-start blocks, tool-call + tool-result pairing, error-text results). Fix the
   dangling `input-available` tool-call case (3.5).
4. **Validation brittleness**: the TS SDK rejects history whose stored tool inputs no longer validate (500 before streaming).
   Decide whether the port validates history at all.
5. **Shared types**: `ForayUIMessage` is inferred from the server agent; the frontend needs a hand-maintained TS type (tool
   names + input/output shapes) once the agent is Python. `FOLLOW_UP_KINDS` / `chat.followup` strings must stay identical.
6. **Security gaps to close, not reproduce**: client controls `incoming.role`, `metadata` (could forge `run_update` dividers or
   `system` messages) and parts; `saveMessages`'s `ON CONFLICT (id) DO UPDATE` has no user/conversation predicate, so a client
   sending an existing message id from another conversation/user overwrites that row's `parts`/`metadata`. The port should
   force `role='user'`, text-only parts, strip metadata, and scope the upsert (`WHERE messages.user_id = excluded.user_id AND
   messages.conversation_id = excluded.conversation_id`).
7. **Auth semantics**: `requireUser` in route handlers yields a 307 to `/sign-in`; the chat client would follow it and choke.
   Use 401 JSON; the client shows the body text.
8. **`regenerate()` duplication** and **id-claim fallback** (a foreign/invalid `id` silently creates a different conversation whose id
   the client never learns; `x-conversation-id` is ignored client-side).
9. **No chat-turn timeout** today despite DESIGN invariant 12; follow-up has 90 s. Pick explicit limits in the port.
10. **Chat agent bypasses** the per-provider limiter and per-purpose reasoning effort; SDK `maxRetries=2` applies. Decide
    whether the Python agent should go through the same call wrapper (probably yes).
11. **Follow-up worker** sleeps up to 90 s polling plus a 90 s agent run inside one job (< 300 s expiry): needs a worker that
    tolerates long async waits (e.g. Procrastinate/Celery with adequate time limits), and `singletonKey` dedupe per event.
12. **Context propagation** for ledger attribution (AsyncLocalStorage → `contextvars`; copy context into thread pools used by
    sync DB/tool code).
13. **Ordering**: history is ordered by `created_at` only (`now()` per statement); ties are theoretically possible. Consider a
    secondary key (e.g. a bigserial) in the port.
14. **Locale/time details**: "Today is …" uses the server's local TZ and `en-US` long format; JS `slice(0,80)` counts UTF-16 code
    units (Python slices code points); `get_job` salary prints Postgres `numeric` text verbatim.
15. **Side effects survive client stop**: a tool mid-execution completes (e.g. a search run is created) even though the user never
    sees its card; the run still follows up in the chat because it carries `conversation_id`.
