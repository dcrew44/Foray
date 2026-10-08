# Area map: http-surface

Every HTTP entry point of the Next.js server in Foray: the edge `proxy.ts`, the Better Auth
catch-all, the 10 other route handlers under `src/app/api/**`, and the 16 server actions under
`src/app/actions/**`. For each: URL/name, method, auth, request shape, response shape,
status codes, headers, revalidation, lib calls, queue sends, and client call sites.

All paths below are relative to the repo root
`/Users/haydenbarolette/Projects/JobSearchAgent/.claude/worktrees/middleware-django-migration-389b1a`.
Versions: Next 16.3.5, React 19.2.8, `ai` 7.0.106, `better-auth` 1.7.5, zod 4, pg-boss 12
(`package.json`).

---

## 0. Cross-cutting conventions

### 0.1 Auth on every handler and action

- `requireUser()` (`src/lib/auth/session.ts:21-25`) calls `getSession()`
  (`session.ts:10-12` = `auth.api.getSession({ headers: await headers() })`). If there is no
  `session.user`, it calls Next's `redirect("/sign-in")`.
  - **In a route handler** that `redirect()` becomes an HTTP **307** with `Location: /sign-in`
    (not a 401). Browser `fetch` follows it and receives the sign-in HTML with status 200 and
    `response.redirected === true`. The only client that copes is the live poller
    (`src/components/live/live-provider.tsx:94-97`: `if (response.status === 401 ||
    response.redirected) router.refresh()`). The poller already accepts **401**, so a Django
    port should return 401 JSON for API endpoints.
  - **In a server action** the redirect is delivered via the action response and the client
    router navigates to `/sign-in`.
- `requireUser()` returns the Better Auth user object: `{ id: string, name: string, email:
  string, emailVerified: boolean, image: string|null, createdAt, updatedAt }` (table `user`,
  `src/lib/db/schema/auth.ts:23-34`). Handlers use `user.id` and (chat only) `user.name`.
- **User scoping (DESIGN.md invariant 8):** every per-user read/write is filtered by
  `user_id`. A row that belongs to someone else is reported exactly like a missing row
  ("indistinguishable from one that does not exist" — `documents/[id]/route.ts:24`,
  `runs/[id]/route.ts:15`, `runs/[id]/events/route.ts:25`, `actions/matches.ts:27-29`).
  **Exception:** the company registry is shared and NOT user-scoped
  (`actions/companies.ts:20-23`, `lib/db/queries/companies.ts:9-12`).
- UUID validation before hitting Postgres (so a malformed id is "not found" instead of a
  Postgres cast error): regex `/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i`
  in `runs.ts:251`, `approvals.ts:628`, `conversations.ts:83`, `resumes.ts:22`,
  `matches.ts:586`, `documents/[id]/route.ts:7-8`. The matches server actions instead use zod
  `z.uuid()` (`actions/matches.ts:44`), which is stricter (RFC 9562: version nibble 1-8 and
  variant 8/9/a/b; verified: `123e4567-e89b-02d3-…` fails). `/api/matches` uses a looser
  `/^[0-9a-f-]{36}$/i` (see 3.7).

### 0.2 JSON serialization contract
`NextResponse.json` uses `JSON.stringify`, so JS `Date` values become
`YYYY-MM-DDTHH:mm:ss.sssZ` (millisecond precision, `Z`). Several shapes (`MatchRow`) are
returned with raw `Date` fields and so serialize this way; others convert explicitly with
`.toISOString()`. Postgres `numeric` salary columns are cast to `float8` in SQL
(`matches.ts:76-77`) so they arrive as JSON numbers. Server-action return values go through
React Flight, which preserves `Date` objects natively — but every action here returns only
strings/numbers/booleans/plain objects (CompanyRow pre-stringifies dates,
`company-row.ts:38`).

### 0.3 Caching headers
All JSON GETs set `Cache-Control: no-store` (conversations, live, matches, runs, events).
File/HTML responses set `Cache-Control: private, no-store` plus `X-Content-Type-Options:
nosniff`. No route exports `dynamic`, `revalidate`, `runtime` or `maxDuration`; there is no
`revalidateTag`, `unstable_cache`, or `cookies()` call anywhere in `src`.

### 0.4 Server-action mechanics a Django port must replace
- An action is invoked as a `POST` to the current page URL with a `Next-Action: <hash>`
  header; arguments and return value are React-Flight encoded. Next enforces a same-origin
  check (Origin vs Host) — that is the actions' only CSRF protection. Default body limit 1 MB
  (`next.config.ts` does not raise it) — relevant to `saveProfile`, which sends a whole resume.
- `revalidatePath(path)` / `revalidatePath(path, "layout")` purges the RSC cache for those
  routes and, when called inside an action, ships a fresh RSC payload of the current route in
  the same response, so the page re-renders without a client `router.refresh()`. Per-action
  revalidations are listed in section 4. In a Django-backed world the equivalent is: client
  calls `router.refresh()` (or refetches) after a successful mutation, or a thin Next action
  proxies to Django and keeps calling `revalidatePath`.
- Thrown (unexpected) errors in an action reject the client promise; components catch with a
  generic toast ("That request did not go through…", `company-table.tsx:179`,
  `add-company.tsx:72`).

### 0.5 What "enqueue" means
`send(queue, payload, options?)` (`src/lib/queue/send.ts:33-40`) is a pg-boss `send` on a
lazily-started sender instance cached on `globalThis.__foraySender` (`send.ts:14-30`); the
sender runs `ensureQueues()` once on start (`boss.ts:64-70`: `createQueue` + `updateQueue`
for every queue with its policy and `notify: true`). pg-boss schema `pgboss`, sender has
`migrate/supervise/schedule/useListenNotify = false` (`boss.ts:12-28`). Returns the pg-boss
job id string or null. Queue names and payloads: section 6.

---

## 1. `src/proxy.ts` (Next 16 "proxy", formerly middleware)

- `proxy(request)` (`proxy.ts:12-18`): if `getSessionCookie(request)` returns a value, pass
  through (`NextResponse.next()`); otherwise **307 redirect** to
  `/sign-in?next=<request.nextUrl.pathname>` (pathname only — the query string is dropped).
- It is an **optimistic presence check, not authorization** (`proxy.ts:4-11`): it never
  validates the cookie; pages and actions re-check with `requireUser()`.
- `getSessionCookie` (better-auth `dist/cookies/index.mjs:261-270`): parses the `Cookie`
  header and returns the first of `__Secure-better-auth.session_token`,
  `better-auth.session_token`, `__Secure-better-auth-session_token`,
  `better-auth-session_token`.
- `config.matcher` (`proxy.ts:20-33`): `/home/:path*`, `/matches/:path*`, `/chat/:path*`,
  `/onboarding/:path*`, `/pipeline/:path*`, `/jobs/:path*`, `/activity/:path*`,
  `/profile/:path*`, `/companies/:path*`, `/settings/:path*`. (`:path*` also matches the bare
  prefix, e.g. `/chat`.) **Not** matched: `/`, `/sign-in`, `/api/**`, static assets.
  `/pipeline` and `/jobs` have no pages today (legacy/forward paths).
- Django: this is frontend routing. If Next stays the frontend, keep it in Next (it only
  needs the cookie name). If Django serves pages, a middleware that redirects unauthenticated
  requests for those prefixes to `/sign-in?next=<path>`.

Related page-level redirects (server components, not HTTP APIs but auth-relevant):
- `/` (`src/app/page.tsx:6-8`): `redirect(session ? "/home" : "/sign-in")`.
- `/sign-in` (`src/app/(auth)/sign-in/page.tsx:12-24`): `safeNext(next)` accepts only values
  starting with `/` and not `//`, else `/home`; if already signed in, `redirect(next)`.
  Passes `googleEnabled = Boolean(GOOGLE_CLIENT_ID && GOOGLE_CLIENT_SECRET)` to the form.
- `(app)/layout.tsx:11-20`: `requireUser()` then renders `LiveProvider` + shell with
  `{ name, email }`.
- `/onboarding` page: `redirect("/home")`.

---

## 2. Auth: `GET|POST /api/auth/[...all]` (Better Auth)

`src/app/api/auth/[...all]/route.ts:1-5`: `export const { GET, POST } = toNextJsHandler(auth)`.
The whole Better Auth router is mounted at `/api/auth/*`.

### 2.1 Configuration (`src/lib/auth/index.ts:25-45`)
- `baseURL: env.BETTER_AUTH_URL` (default `http://localhost:3000`, `src/env.ts:34`);
  `secret: env.BETTER_AUTH_SECRET` (optional, min 32 chars, `env.ts:33`; when unset Better Auth
  falls back to its library default `"better-auth-secret-12345678901234567890"` and refuses it
  only in production — `better-auth/dist/utils/constants.mjs:2`,
  `context/create-context.mjs:36-40`).
- Drizzle adapter on tables `user`, `session`, `account`, `verification`
  (`src/lib/db/schema/auth.ts:23-95`; columns snake_case, ids are `text`).
- `emailAndPassword: { enabled: true, requireEmailVerification: false }` (no mail transport).
- `socialProviders.google` only when both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
  are set (`auth/index.ts:15-23`); wired early so Phase 3 Gmail scopes can be added via
  `linkSocial` on the same account.
- `plugins: [nextCookies()]` — lets `auth.api.*` write Set-Cookie from actions/route handlers.
- Defaults in effect (better-auth `context/create-context.mjs:146-175`): session `expiresIn`
  7 days, `updateAge` 1 day (sliding refresh on `getSession`), `freshAge` 1 day, no cookie
  cache; rate limit **enabled only in production**, 100 req / 10 s per IP+path, with special
  rules: `/sign-in*`, `/sign-up*`, `/change-password*`, `/change-email*` max 3 per 10 s
  (`api/rate-limiter/index.mjs:302-316`). Password length 8-128.
- Cookie (`cookies/index.mjs:22-49`): name `better-auth.session_token`, prefixed
  `__Secure-` when `BETTER_AUTH_URL` is https; attributes `HttpOnly; SameSite=Lax; Path=/`,
  `Secure` with the prefix, `Max-Age` 7 d. **Value = URL-encoded `${token}.${base64(HMAC-SHA256(secret, token))}`**
  (standard base64 with padding), where `token` is `session.token` in the DB
  (`scripts/dev-session.ts:43-45` reproduces this exactly).
- Password hash format (`@better-auth/utils/dist/password.node.mjs`): scrypt, `N=16384,
  r=16, p=1, dkLen=64`, password NFKC-normalized, salt = 16 random bytes hex-encoded and used
  **as the hex string** (not decoded) for scrypt; stored as `"<saltHex>:<keyHex>"` in
  `account.password` where `account.provider_id = 'credential'` and `account.account_id =
  user.id`.

### 2.2 Endpoints the app actually calls (client: `src/lib/auth/client.ts:9-11`,
`createAuthClient()` same-origin)

| Call site | HTTP | Body | Success | Errors shown |
|---|---|---|---|---|
| `signUp.email` — `sign-in-form.tsx:35` | `POST /api/auth/sign-up/email` | `{ name: name \|\| email.split("@")[0], email, password }` | 200 `{ token, user }`, sets session cookie (auto sign-in). Email lowercased. Creates `user` + `account(credential)` + `session`. | 400 `Invalid email` / `Password too short` / `Password too long`; 422 `User already exists. Use another email.` |
| `signIn.email` — `sign-in-form.tsx:36` | `POST /api/auth/sign-in/email` | `{ email, password }` | 200 `{ redirect: false, token, url: undefined, user }` + cookie | 401 `Invalid email or password` (timing-equalized hash on unknown user) |
| `signIn.social` — `sign-in-form.tsx:129` | `POST /api/auth/sign-in/social` | `{ provider: "google", callbackURL: next }` | `{ url, redirect: true }`; client navigates to Google; Google returns to `GET /api/auth/callback/google`, which creates/links user+account, sets cookie, redirects to `callbackURL` | — |
| `signOut` — `sign-out-button.tsx:21`, `user-menu.tsx:83` | `POST /api/auth/sign-out` | none | deletes the `session` row, clears cookies, `{ success: true }` | — |

Client behaviour after auth calls: sign-up → `router.push("/home")`; sign-in →
`router.push(next)`; both then `router.refresh()` (`sign-in-form.tsx:49-51`). Error text is
`result.error.message` or a fallback (`sign-in-form.tsx:38-46`). Sign-out → push
`/sign-in` + refresh.

Server-side use: `auth.api.getSession` (via `getSession`/`requireUser`) on every page,
action and route handler; validates signed cookie, loads `session` by token, checks
`expires_at`, and (when older than `updateAge`) extends `expires_at` and re-sets the cookie.
Better Auth also performs an Origin check (trusted origins = `baseURL`) on mutating
endpoints and `formCsrfMiddleware` on `/sign-in/email` (`api/middlewares/origin-check.mjs`).

Other mounted-but-unused Better Auth endpoints exist (`/get-session`, `/list-sessions`,
`/update-user`, `/change-password`, `/forget-password`, `/reset-password`, `/verify-email`,
`/link-social`, `/list-accounts`, `/revoke-session(s)`, `/ok`, `/error`, …); nothing in `src`
calls them.

### 2.3 Scripts that depend on the auth format
- `scripts/dev-session.ts:22-49`: refuses in production; finds user by email (or first
  user); inserts `session { id: hex(16 bytes), token: base64url(24 bytes), user_id,
  expires_at: now+7d, user_agent: "dev-session script" }`; prints
  `cookie: better-auth.session_token=<encodeURIComponent(token + "." + base64 HMAC)>`. Used by
  the CLAUDE.md browser-testing workflow.
- `scripts/set-password.ts:21-48`: requires a `credential` account; prompts twice at a hidden
  TTY prompt; enforces min/max length; `internalAdapter.updatePassword(userId,
  password.hash(pw))`; keeps existing sessions.

---

## 3. Route handlers (`src/app/api/**`)

### 3.1 `POST /api/chat` — one chat turn, streamed
File `src/app/api/chat/route.ts:21-87`.

- **Auth:** `requireUser()`.
- **Request:** JSON `{ id?: string, message?: ForayUIMessage, messages?: ForayUIMessage[] }`
  (`route.ts:24-28`). The real client (`DefaultChatTransport` with
  `prepareSendMessagesRequest`, `src/components/chat/chat.tsx:71-76`) sends only
  `{ id: chatId, message: <last UIMessage> }`. `messages` (SDK default) is accepted for plain
  fetches: `incoming = body.message ?? body.messages.at(-1)` (`route.ts:32`).
- `ForayUIMessage` (`src/lib/agents/chat-agent.ts:114-116`) = AI SDK `UIMessage`:
  `{ id: string, role: "user"|"assistant"|"system", parts: UIPart[], metadata?: unknown }`
  with tool parts typed by the agent's tools (`tool-<name>` parts with `toolCallId`, `state`,
  `input`, `output`, `errorText`). The client generates user-message ids with the SDK's
  `generateId` (16-char alphanumeric).
- **Flow:**
  1. No `incoming` → **400** `text/plain` body `No message provided.` (`route.ts:33-35`).
  2. Title = first `text` part's `.text.slice(0, 80)` (`route.ts:37-44`).
  3. `ensureConversation(user.id, body.id, title)` (`conversations.ts:64-81`): if `id` is a
     UUID and the user owns it → that row. Else if `id` is a UUID, try `INSERT INTO
     conversations (id, user_id, title) … ON CONFLICT DO NOTHING RETURNING *` ("claim" the
     client-chosen id). Else (bad id, or id held by another user) → new conversation with a
     random id. The client pre-picks the id (`chat/[[...id]]/page.tsx:29`,
     `crypto.randomUUID()`) so it can `history.replaceState` to `/chat/<id>` after the first
     reply (`chat.tsx:81-87`). The title is only set on creation, never updated.
  4. `history = loadMessages(user.id, conversation.id)` (`conversations.ts:86-107`):
     `SELECT * FROM messages WHERE conversation_id=$1 AND user_id=$2 ORDER BY created_at ASC`,
     mapped to `{ id, role, parts, metadata ?? undefined }`. Trusted, not revalidated
     (`route.ts:46-47`).
  5. `uiMessages = [...history, incoming]`; `saveMessages(user.id, conversation.id,
     [incoming])` persists the user message **before** the model runs (`route.ts:52-54`).
  6. `agent = await chatAgent(user.id, { conversationId, userName: user.name })` — built
     **outside** the LLM context so its profile read is not attributed (`route.ts:56-64`).
     Agent: `ToolLoopAgent` with `model("chat")`, instructions from `systemPrompt(...)`, 16
     tools, `stopWhen: isStepCount(8)` (`chat-agent.ts:43-97`). (The agent itself belongs to
     the agents area.)
  7. Returns `withLlmContext({ userId }, () => createAgentUIStreamResponse({...}))`
     (`route.ts:66-86`), options:
     - `agent`, `uiMessages`, `originalMessages: uiMessages`;
     - `headers: { "x-conversation-id": conversation.id }` (no client reads it);
     - `generateMessageId: generateId` — **required**: without it the assistant message id
       is `""` and every reply upserts onto one orphan `messages` row (`route.ts:72-78`);
     - `onEnd: async ({ responseMessage }) => saveMessages(user.id, conversation.id,
       [responseMessage])` — persists the final assistant message once the stream ends
       (`route.ts:79-84`). In ai 7.0.106 `onEnd` also fires on stream `cancel()` (client
       abort / Stop button), so a partial reply is saved (`ai/dist/index.js:7946-8003`).
- **Response:** `200`, Server-Sent Events, AI SDK **UI Message Stream protocol v1**. Headers
  (`ai/dist/index.js:6870-6876`): `content-type: text/event-stream`, `cache-control:
  no-cache`, `connection: keep-alive`, `x-vercel-ai-ui-message-stream: v1`,
  `x-accel-buffering: no`, plus `x-conversation-id`. Body: `data: <JSON chunk>\n\n` per chunk
  (`start` with `messageId`, `start-step`, `text-start/delta/end`, `reasoning-*`,
  `tool-input-start/delta/available`, `tool-output-available`/`tool-output-error`,
  `finish-step`, `finish`, `error`, `abort`), terminated by `data: [DONE]\n\n`.
  `createAgentUIStream` first runs `validateUIMessagesForAgent` against the tool schemas
  and `convertToModelMessages` (`ai/dist/index.js:12795-12835`).
- **Side effects:** upsert of `messages` rows (user then assistant), `conversations.updated_at`
  bumped on each save; LLM calls recorded in `llm_calls` by ledger middleware attributed to
  `userId` via AsyncLocalStorage (`src/lib/llm/context.ts:17-25`); tools may create runs and
  enqueue jobs (agents area).
- `saveMessages` (`conversations.ts:116-148`): `INSERT INTO messages (id, conversation_id,
  user_id, role, parts, metadata) VALUES … ON CONFLICT (id) DO UPDATE SET parts =
  excluded.parts, metadata = excluded.metadata`; then `UPDATE conversations SET updated_at =
  now() WHERE id = $conversationId`. `created_at` is never updated.
- **Client:** `src/components/chat/chat.tsx:67-89` (`useChat` + `DefaultChatTransport`,
  `onFinish` → `requestLiveRefresh()`, address-bar rewrite, `announceConversationsChanged()`);
  `regenerate()` on error (`chat.tsx:~247`) re-posts the last user message (same id); `stop()`
  aborts the fetch. Non-2xx → `useChat` error whose message is the response text.
- **Not done here:** no request timeout or `maxDuration` despite DESIGN.md invariant 12
  ("capped with isStepCount(8) and a timeout") — only the step cap exists.

### 3.2 `GET /api/conversations` — sidebar recent chats
`src/app/api/conversations/route.ts:7-21`.
- Auth `requireUser()`. No params.
- `listConversations(user.id, 30)` (`conversations.ts:14-21`): `SELECT * FROM conversations
  WHERE user_id=$1 ORDER BY updated_at DESC LIMIT 30`.
- 200 `{ conversations: [{ id, title: title ?? "Untitled chat", updatedAt: ISO }] }`,
  `Cache-Control: no-store`.
- Client: `src/components/shell/recent-chats.tsx:35-40` (on mount and on the window event
  `foray:conversations-changed`); shows first 20.

### 3.3 `GET /api/conversations/[id]` — one conversation's messages
`src/app/api/conversations/[id]/route.ts:16-22`.
- Auth. `getConversation(user.id, id)` (UUID-checked, owner-filtered,
  `conversations.ts:24-32`); missing → **404** `{ error: "Not found" }`.
- 200 `{ messages: UIMessage[] }` (from `loadMessages`), `Cache-Control: no-store`.
- Client: `chat.tsx:105-110` — reloads after a background follow-up (`chat.followup` live
  event with this `conversationId` and a `messageId`), merging server messages with local
  ones not yet persisted (`mergeMessages`, `chat.tsx:~293-296`).

### 3.4 `DELETE /api/conversations/[id]`
`src/app/api/conversations/[id]/route.ts:25-30`.
- Auth. `deleteConversation(user.id, id)` (`conversations.ts:35-42`): UUID check, `DELETE
  FROM conversations WHERE id=$1 AND user_id=$2 RETURNING id` (messages cascade via FK).
- 200 `{ ok: true }`; not deleted → **404** `{ error: "Not found" }`.
- Note: runs keep their `conversation_id` (no FK on `runs.conversation_id`,
  `schema/runs.ts:54`), so a follow-up can later target a deleted conversation (follow-up
  area handles it).
- Client: `recent-chats.tsx:53-57` (optimistic removal; navigates to `/chat` if viewing it;
  ignores the response).

### 3.5 `GET /api/documents/[id]` — stored file download/inline
`src/app/api/documents/[id]/route.ts:17-50`.
- Auth. Query `?download=1` → attachment, otherwise inline.
- `id` must match UUID regex (`route.ts:7-8, 25`), then `getDocument(user.id, id)`
  (`profile.ts:30-37`: `SELECT * FROM documents WHERE id=$1 AND user_id=$2 LIMIT 1`).
  Missing → **404** `{ error: "Not found" }`.
- `storage.get(document.storagePath)` (`src/lib/storage/index.ts:64-66`: `readFile` under
  `resolve(STORAGE_DIR)` with a path-traversal guard, `index.ts:47-54`). Read failure → logs
  and **404** `{ error: "The file is missing from storage." }`.
- 200 raw bytes with headers: `Content-Type: <documents.mime>`, `Content-Length`,
  `Content-Disposition: <inline|attachment>; filename="<ascii>"; filename*=UTF-8''<encoded>`,
  `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`.
- `contentDisposition` (`route.ts:56-63`, RFC 6266): ascii = filename with every char outside
  `\x20-\x7e` replaced by `_`, then `"` and `\` replaced by `_`; encoded =
  `encodeURIComponent(filename)` with `'()*` additionally percent-encoded uppercase.
- Serves uploaded resumes (`resume_upload`, `linkedin_pdf`) and worker-rendered PDFs
  (`resume_pdf`).
- Client: plain `<a href>` links — `tailor-panel.tsx:146` (inline, new tab),
  `tailor-panel.tsx:155` and `profile-header.tsx:53` (`?download=1`), `run-card.tsx:352,363`.

### 3.6 `GET /api/live?since=<cursor>` — the app shell's single poll
`src/app/api/live/route.ts:12-26`.
- Auth. `since = Number(searchParams.since ?? 0)`; if not finite or `<= 0` → 0
  (`route.ts:15-16`). (JS `Number` accepts `"1e3"`, `"0x10"`, `" 5 "`, decimals.)
- **Side effect on first poll** (`since === 0`): `expireStaleApprovals(user.id)`
  (`src/lib/runs/approvals.ts:553-583`): `UPDATE approvals SET status='expired' WHERE
  user_id=$1 AND status='pending' AND expires_at <= now() RETURNING *`; for each: `emit({
  kind: "approval.expired", level: "warn", message: "No answer within a week, so this
  lapsed: <title>.", approvalId, runId })` and enqueue its stored `continuation` (an expiry
  is a "no" that resumes the run so it can finish); enqueue errors are logged, not thrown.
  So this GET writes rows and enqueues jobs.
- Response: `liveSnapshot(user.id, since)` (`src/lib/runs/live.ts:96-172`), 200 JSON,
  `Cache-Control: no-store`:
  ```
  { cursor: number,            // id of the last returned event, else `since`
    events: LiveEvent[],
    runs: LiveRun[],
    approvals: LiveApproval[],
    worker: { online: boolean, lastSeenAt: string|null, pid: number|null } }
  ```
  Four parallel queries:
  - events, `since > 0`: `SELECT * FROM agent_events WHERE user_id=$1 AND id > $since ORDER
    BY id ASC LIMIT 200`; `since = 0`: latest 60 (`ORDER BY id DESC LIMIT 60`, reversed to
    ascending) (`live.ts:103-117`).
  - runs: `WHERE user_id=$1 AND (status IN ('queued','running','awaiting_approval') OR
    updated_at > now()-10min) ORDER BY created_at DESC LIMIT 20` (`RECENT_MS = 10 min`,
    `live.ts:69, 118-131`).
  - approvals: `WHERE user_id=$1 AND status='pending' AND (expires_at IS NULL OR expires_at
    > now()) ORDER BY created_at DESC LIMIT 20` (`live.ts:132-143`) — note: no join on run
    status here (unlike `listPendingApprovals`).
  - worker: `workerStatus()` (`src/lib/system/heartbeat.ts:42-57`): row `system_state` key
    `'worker'`; `online = now - updated_at < 45_000 ms` (`WORKER_STALE_MS`, `heartbeat.ts:18`),
    `lastSeenAt = updated_at ISO`, `pid = value.pid ?? null`; no row → `{online:false,
    lastSeenAt:null, pid:null}`.
  - `LiveEvent` (`live.ts:18-27`): `{ id: number, runId: string|null, level:
    "debug"|"info"|"warn"|"error", kind: string, message: string, data: unknown,
    approvalId: string|null, createdAt: ISO }`.
  - `LiveRun` = `toLiveRun(run)` (`live.ts:71-94`): `{ id, kind, status, mode, prompt:
    input.prompt ?? null, summary: kind==="search" ? describeBrief(parseBrief(input.brief ??
    input.hints)) : null, stats: run.stats ?? {}, output: run.output ?? null, error,
    createdAt: ISO, startedAt: ISO|null, finishedAt: ISO|null }`. `describeBrief` /
    `parseBrief` live in `src/lib/ranking/brief.ts:56-59, 151-169` (ranking area).
  - `LiveApproval` (`live.ts:51-58`): `{ id, runId, kind: "deep_eval_batch"|
    "shortlist_proposal"|"tailor"|"company_add", title, payload: object, createdAt: ISO }`.
- Client: `src/components/live/live-provider.tsx:86-143` — one provider in the app layout
  polls with adaptive cadence: 1.5 s while any run is active or an approval is pending, 6 s
  idle, 30 s hidden-tab idle (6 s hidden-tab busy) (`FAST_MS/SLOW_MS/HIDDEN_MS`,
  `live-provider.tsx:55-58`); keeps last 300 events; merges runs by id; first snapshot is
  history (no toasts); later events fan out to listeners (`useLiveEvent`) and toasts
  (`announce`, `live-provider.tsx:211-247`); 401/redirected → `router.refresh()`.
  `requestLiveRefresh()` dispatches `foray:live-refresh` to poll immediately.

### 3.7 `GET /api/matches?ids=a,b,c` — match rows by id
`src/app/api/matches/route.ts:10-20`.
- Auth. `ids` = split on `,`, trim, keep those matching `/^[0-9a-f-]{36}$/i`, first 20.
- `getMatchRows(user.id, ids)` (`src/lib/db/queries/matches.ts:144-156`): empty → `[]`;
  else `SELECT <columns> FROM job_matches JOIN jobs ON jobs.id = job_matches.job_id WHERE
  job_matches.user_id=$1 AND job_matches.id IN (…) ORDER BY coalesce(deep_score, fast_score)
  DESC NULLS LAST`.
- 200 `{ matches: MatchRow[] }`, `Cache-Control: no-store`. `MatchRow`
  (`matches.ts:31-58`, columns `matches.ts:62-87`):
  `matchId, jobId, stage, stageChangedAt (Date→ISO), fastScore, deepScore, score
  (= coalesce(deep, fast)), recommendation (= deep_eval->>'recommendation'), headline (=
  coalesce(deep_eval->>'summary', fast_eval->'fit_reasons'->>0)), title (jobs.title), company
  (jobs.company_name), locations (text[]), remote (enum), salaryMin/salaryMax (numeric::float8),
  salaryCurrency, salaryPeriod, postedAt (Date|null), url, applyUrl, isActive, tailoredResumeId,
  userRating, createdAt (job_matches.created_at)`.
- Client: `src/components/runs/run-card.tsx:392-406` (`ResultList`: a finished run's
  `output.topMatchIds` or `output.matchIds`); on non-OK treats as `{ matches: [] }`.

### 3.8 `GET /api/resumes/[id]/preview` — resume HTML for an iframe
`src/app/api/resumes/[id]/preview/route.ts:26-53`.
- Auth. `getResume(user.id, id)` (`src/lib/db/queries/resumes.ts:25-36`, UUID-checked,
  owner-filtered; base or tailored). Missing → **404** `{ error: "Not found" }`.
- `ResumeSchema.safeParse(row.data)` (`src/lib/resume/schema.ts:86-93`); failure → **422**
  `{ error: "This resume could not be read." }`.
- 200 `renderResumeHtml(parsed.data)` (`src/lib/resume/render/template.ts:276`; the same
  template the worker prints to PDF; escapes every value) with headers:
  `Content-Type: text/html; charset=utf-8`,
  `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src data:;
  base-uri 'none'; form-action 'none'; frame-ancestors 'self'` (`route.ts:17-24`),
  `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`. Rationale: user/model-written text served from the app
  origin; CSP is the second lock after escaping (`route.ts:8-16`).
- Client: `tailor-panel.tsx:133-138` `<iframe sandbox="" src=…>`. `frame-ancestors 'self'`
  means the frontend and this endpoint must share an origin (or the CSP must name the
  frontend origin) after the split.

### 3.9 `GET /api/runs/[id]` — one run as `LiveRun`
`src/app/api/runs/[id]/route.ts:8-21`.
- Auth. `getRun(user.id, id)` (`src/lib/runs/runs.ts:24-33`; UUID check; `WHERE id=$1 AND
  user_id=$2`). Missing → **404** `{ error: "Not found" }`.
- 200 `toLiveRun(run)` (shape in 3.6), `Cache-Control: no-store`.
- Client: `useRun()` in `live-provider.tsx:287-309` — fetched once for a run that is not in
  the live snapshot (old run card in a chat), then `rememberRun`.

### 3.10 `GET /api/runs/[id]/events` — every event of one run
`src/app/api/runs/[id]/events/route.ts:18-47`.
- Auth; `getRun` ownership check → **404** `{ error: "Not found" }`.
- `SELECT * FROM agent_events WHERE user_id=$1 AND run_id=$2 ORDER BY id ASC LIMIT 1000`
  (`LIMIT = 1000` "a backstop, not a page size", `route.ts:10-11`). Query inline with
  Drizzle, not via a query module.
- 200 `{ events: LiveEvent[] }` (same mapping as 3.6), `Cache-Control: no-store`.
- Client: `src/components/activity/run-list.tsx:176-187` (expanding a run on /activity;
  merges with live events by id).

### 3.11 `POST /api/uploads` — resume upload + inline extraction
`src/app/api/uploads/route.ts:24-116`.
- Auth. `multipart/form-data`: `file` (required, a `File`), `kind` (optional; only the value
  `"linkedin_pdf"` is honoured). The real client sends only `file`
  (`src/components/profile/resume-dropzone.tsx:47-51`).
- Validation, in order:
  - not a File → **400** `{ error: "No file uploaded." }`;
  - `file.size > 10 MB` (`MAX_BYTES = 10*1024*1024`, `route.ts:9`) → **413** `{ error:
    "That file is <size MB, 1 decimal> MB; the limit is 10 MB." }`;
  - `mime = file.type || "application/octet-stream"` must be `application/pdf` or
    `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
    (`ACCEPTED`, `route.ts:11-15`) else **415** `{ error: "Upload a PDF or a Word (.docx)
    file." }`.
- `kind = kindHint === "linkedin_pdf" ? "linkedin_pdf" : "resume_upload"`.
- Steps: read bytes; `digest = sha256 hex`; `path = storage.put(storageKey(user.id, kind,
  file.name), data, mime)` — key `"<userId>/<kind>/<Date.now()>-<safe>"` where `safe =
  filename.replace(/[^a-zA-Z0-9._-]/g, "_").slice(-100)` (`storage/index.ts:33-40`). The file
  is written **before** extraction.
- Then (try):
  - `extractResume({ data, mime, source: kind==="linkedin_pdf" ? "linkedin_pdf" : "resume",
    context: { userId } })` (`src/lib/resume/extract.ts:67-110`) — **one LLM call inline in
    the request** (10-30 s; purpose `extract`; vision fallback renders PDF pages to PNG when
    the text layer is garbled). Comment justifies inline execution (`route.ts:17-23`).
  - `recordDocument({ userId, kind, filename: file.name, mime, sizeBytes: file.size,
    storagePath: path, sha256: digest, textExtracted: extraction.text })`
    (`profile.ts:43-62`): `INSERT … ON CONFLICT (user_id, sha256) DO UPDATE SET
    text_extracted = excluded value RETURNING *` (unique index
    `documents_user_sha256_idx`). A re-upload of identical bytes returns the existing row,
    whose `storage_path` still points at the first copy (the new copy is orphaned).
  - If `kind === "resume_upload"`: `saveBaseResume({ userId, resume, documentId })`
    (`profile.ts:72-101`): `INSERT INTO resumes (user_id, kind='base', version = previous+1,
    data)`; upsert `profiles (user_id, data)` `ON CONFLICT (user_id) DO UPDATE SET data,
    updated_at` — this is what creates the `profiles` row other actions require. (The
    `documentId` argument is accepted but not stored.) `linkedin_pdf` is stored and parsed but
    does not touch the base resume ("Step 9" merge path pending, `route.ts:79-80`).
  - 200 `{ documentId, resumeId: string|null, usedVision: boolean, garble: GarbleReport,
    counts: { work, education, skills, projects } }`. `GarbleReport`
    (`src/lib/resume/parse/garble.ts:17-29`): `{ garbled, reasons: string[], metrics: { chars,
    charsPerPage, spaceRatio, alphaRatio, replacementRatio, singleCharWordRatio } }`.
- catch → log, **422** `{ error: "Could not read that resume: <error.message>" }` (or without
  the suffix for non-Error throws). The stored file is left in place, no document row.
- Client: `resume-dropzone.tsx:40-75` — checks extension `.pdf|.docx` client-side, posts
  FormData, reads `json.error` / `json.counts` / `json.usedVision`, toasts "Read N roles, N
  schools and N skills.", then `router.refresh()`. Used on /profile and onboarding.

---

## 4. Server actions (`src/app/actions/**`, all `"use server"`)

Every action starts with `requireUser()` (redirect to `/sign-in` when signed out). Expected
failures are returned as `{ ok: false, message }` objects, never thrown; unexpected errors
throw.

### 4.1 `decideApprovalAction(approvalId: string, decision: { approved: boolean; itemIds?: string[] })`
`src/app/actions/approvals.ts:26-91`.
- Validation: `Decision = z.object({ approved: z.boolean(), itemIds:
  z.array(z.string()).nullish().transform(ids => ids ?? undefined) })` (`approvals.ts:17-24`;
  null means "all items"). `typeof approvalId !== "string"` or parse failure → `{ ok:false,
  message:"That answer could not be read." }`.
- `decideApproval(user.id, approvalId, parsed, "ui")` (`src/lib/runs/approvals.ts:255-359`):
  1. non-UUID → `not_found`; load row (`WHERE id AND user_id`) → `not_found` if none;
     `status='expired'` → `expired`; status not `pending` → `already_decided`.
  2. Past `expires_at` → `expireStaleApprovals(user)` then `expired`.
  3. Run must be in `queued|running|awaiting_approval`; else mark this approval `expired`
     (conditional on still pending) → `expired`.
  4. `stored = resolveDecision(payload, decision)` (`approvals.ts:365-379`): payload without
     `items[]` → `{approved}`; rejection → `{approved:false, itemIds:[]}`; approval →
     `itemIds` = payload ids (in payload order) filtered to the requested subset (unknown ids
     ignored); an empty result is a rejection. Item id = `item.matchId ?? item.id`, deduped
     (`payloadItemIds`, `approvals.ts:401-410`).
  5. Conditional write: `UPDATE approvals SET status = approved?'approved':'rejected',
     decision=$stored, decided_via='ui', decided_at=now() WHERE id AND user_id AND
     status='pending' AND (expires_at IS NULL OR expires_at > now()) RETURNING *`. No row →
     re-read: `expired` if expired/lapsed else `already_decided` (exactly-once under races
     with chat or double clicks).
  6. `emit({ kind:"approval.decided", message: describeDecision(...), approvalId, runId,
     data:{ kind, via:"ui", approved, itemIds: stored.itemIds ?? null } })` **before**
     enqueueing (so the feed reads "approved" before "evaluating", `approvals.ts:321-323`).
  7. Enqueue the stored `continuation` `{queue, payload}` (validated: queue must be a known
     name and payload an object, `approvals.ts:602-616`) — **for rejections too**, because
     the continuation is what finishes the run. If enqueue throws: revert the approval to
     `pending` (clear decision/decided_via/decided_at), emit `approval.reopened` (warn,
     "Could not resume the run, so the request is open again."), rethrow.
- Action error handling: a throw from `decideApproval` → `{ ok:false, message:"Could not
  resume the run just now. Try again in a moment." }` (`approvals.ts:39-47`).
- **Revalidate (always after a non-throwing decide, success or not):** `/home`, `/activity`,
  `/matches` (layout) (`approvals.ts:49-51`).
- Returns:
  - not ok: `already_decided` → "That was already answered."; `expired` → "That request has
    expired."; else "That request no longer exists."
  - declined: `deep_eval_batch` → "Skipped the deep evaluation."; `shortlist_proposal` → "Left
    the shortlist as it was."; else "Declined."
  - approved (chosen = `decision.itemIds.length` or total): `deep_eval_batch` → "Approved.
    Deep-evaluating N role(s) in the background."; `shortlist_proposal` → "Approved.
    Shortlisting N role(s)."; else "Approved."
- Enqueues: the approval's continuation — in practice `search.evaluate` or
  `search.shortlist` with `{ userId, runId, approvalId }`.
- Client: `src/components/runs/approval-card.tsx:84-98` (sends `itemIds: approved ?
  [...selected] : undefined`; on ok → local "Approved/Skipped" state + `requestLiveRefresh()`).

### 4.2 `addCompanyAction(nameOrUrl: string): AddCompanyResult`
`src/app/actions/companies.ts:32-97`. Shared registry — requires a user, does not scope.
- `query = nameOrUrl.trim()`; empty → `{ok:false, message:"Type a company name, its website
  or a job board link.", trail:[]}`; `> 300` chars → `{ok:false, message:"That is too long to
  be a company name or a link.", trail:[]}`.
- `startedAt = Date.now()`; `discoverCompany({ nameOrUrl: query, addedByUserId: user.id,
  discoveredVia: "user" })` (`src/lib/companies/discovery.ts:189`, companies area) — **runs
  inline**: outbound HTTP to ATS APIs, optional Brave search (`SEARCH_API_KEY`, 12 s timeout,
  `discovery.ts:154-181`), board verification, then upsert into `companies`. Throw →
  `{ok:false, message:"Looking for “<q>” stopped with an error: <msg>", trail:[]}`.
- `!result.registered` → `{ok:false, trail: result.trail, message}` where message depends on
  `looksLikeUrl = /^(?:https?:\/\/|www\.)|\.[a-z]{2,}(?:\/|$)/i`: URL → "No job board found at
  “<q>”. Try a direct link to its jobs page, or the company's name."; name → "No job board
  found for “<q>”. Try its website or a link to its jobs page." `DiscoveryStep = { step,
  detail }` (`discovery.ts:135`).
- `existed = company.createdAt < startedAt`.
- If `sourceType ∉ {careers_page, none}` and `status === "active"`: `refreshCompany(company)`
  **inline** (fetch the whole ATS board, ingest jobs, record fetch;
  `src/lib/companies/refresh.ts:38-94`), then re-read by slug.
- `row = toCompanyRow(company, activeJobCount(company.id))`; `activeJobCount` =
  `SELECT count(jobs.id) FROM jobs WHERE company_id=$1 AND is_active` (`companies.ts:180-186`).
- **Revalidate** `/companies`.
- Returns `{ ok:true, existed, company: CompanyRow, message }`: detail = API board → "Its
  <BOARD_LABEL> board has <plural(activeJobs, "open role")>." else "Its careers page is read at
  the next scheduled refresh."; message = existed ? "<name> was already on the list. <detail>"
  : "Added <name>. <detail>". `plural` = `count.toLocaleString() + " " + (1 ? one : one+"s")`
  (`src/lib/format.ts:178-180`).
- `CompanyRow` (`src/components/companies/company-row.ts:8-25`): `{ slug, name, domain,
  sourceType, atsSlug, careersUrl: careersUrls[0] ?? null, status, needsBrowser, tags,
  lastFetchedAt: ISO|null, lastFetchStatus, lastFetchJobCount, discoveredVia, activeJobs }`.
  `BOARD_LABEL` (`company-row.ts:46-52`): greenhouse "Greenhouse", lever "Lever", ashby
  "Ashby", careers_page "Careers page", none "No board".
- Client: `src/components/companies/add-company.tsx:50-76` (shows `result.trail` under the
  field on a miss; `onAdded(result.company)`).

### 4.3 `setStatusAction(slug: string, status: "active" | "paused")`
`companies.ts:99-117`.
- `getCompanyBySlug(slug)` (`queries/companies.ts:31-38`) → none: `{ok:false, message:"That
  company is no longer on the list."}`.
- `setCompanyStatus(id, status)` (`queries/companies.ts:129-139`: `UPDATE companies SET
  status, updated_at WHERE id RETURNING *`). `status` is **not** runtime-validated (any string
  reaches the enum column).
- Revalidate `/companies`. Returns `{ok:true, company: CompanyRow, message}`: paused →
  "Paused <name>. Its board is skipped until you resume it."; active → "<name> is active again
  and will be fetched with the rest."
- Client: `company-table.tsx:168-189` (menu actions pause/resume).

### 4.4 `refreshCompanyAction(slug: string)`
`companies.ts:120-151`. "Re-fetches one board now, rather than waiting for the six-hourly
refresh."
- Unknown slug → `{ok:false, message:"That company is no longer on the list."}`.
- `outcome = refreshCompany(company)` **inline** — for `careers_page` companies this is an
  **LLM extraction inside the request** (`refresh.ts:44-47, 125-173`). 404 from an ATS marks
  the company `invalid`; transient errors only record the failure.
- Re-read, `row = toCompanyRow(...)`, revalidate `/companies`.
- `outcome.status === "ok"` → `{ok:true, company, message: "<name>: <plural(jobs,"open
  role")><inserted||closed ? ", <inserted> new and <closed> closed since the last fetch" : ",
  no changes">."}` (from `outcome.ingest.inserted` / `.deactivated`).
- else `{ok:false, company, message: "<name>: <lowercased FETCH_PROBLEM[status] ?? 'the fetch
  failed'><status==='not-found' ? ', so it is marked invalid' : ''>.<status==='error' &&
  error ? ' <error>' : ''>"}`; `FETCH_PROBLEM` (`company-row.ts:55-60`): not-found "Board not
  found", error "Last fetch failed", unsupported "Cannot be fetched", needs-browser "Needs a
  browser".
- Client: `company-table.tsx:168-189` (menu "fetch").

### 4.5 `refreshAllAction()`
`companies.ts:154-178`.
- `stale = staleCompanies(staleCutoff())` (`queries/companies.ts:94-109`: `status='active'
  AND (last_fetched_at IS NULL OR last_fetched_at < now-2h) ORDER BY last_fetched_at ASC NULLS
  FIRST LIMIT 200`; `BOARD_STALE_AFTER_MS = 2h`, `refresh.ts:24-28`).
- None → `{ok:true, queued:0, message:"Every active board was fetched in the last two hours, so
  none are stale."}`.
- `send("company.refresh", { userId: user.id, all: false })`; throw → `{ok:false,
  message:"The refresh could not be queued. Check that the database is running."}`.
- `{ok:true, queued: stale.length, message:"Refreshing <plural(n,"stale board")> in the
  background. The table updates when they are done."}`. No revalidate.
- Client: `src/components/companies/refresh-stale-button.tsx:36-52`.

### 4.6 `moveMatchStageAction(matchId: string, stage: Stage)`
`src/app/actions/matches.ts:46-68`.
- Validation: `z.uuid()` on matchId and `ALL_STAGES.includes(stage)` (`found, shortlisted,
  applied, interviewing, offer, rejected, archived`, `queries/matches.ts:17-25`) else
  `{ok:false, message:"That move could not be read."}`.
- `setMatchStage(user.id, matchId, stage)` (`matches.ts:274-289`): `UPDATE job_matches SET
  stage=$s, stage_changed_at = CASE WHEN stage=$s THEN stage_changed_at ELSE now() END,
  updated_at=now() WHERE user_id AND id RETURNING *` (same-stage move is a successful no-op
  that keeps the timestamp). None → `{ok:false, message:"That match no longer exists."}`.
- Revalidate `/matches` (layout) and `/home`.
- Returns `{ok:true, message: row ? "Moved <title> at <company> to <Label>." : "Moved to
  <Label>."}` (labels `actions/matches.ts:34-42`).
- Clients: `board.tsx:104` (and undo at `board.tsx:116`), `match-table.tsx:84`,
  `match-controls.tsx:42`; all optimistic + `router.refresh()`.

### 4.7 `updateMatchAction(matchId: string, patch: { notes?: string|null; userRating?: number|null })`
`matches.ts:79-98`.
- `MatchPatch = z.object({ notes: z.string().max(20_000).nullable().optional(), userRating:
  z.number().int().min(-10).max(10).nullable().optional() })` (`matches.ts:70-77`; UI picks the
  rating scale). Bad id/patch → `{ok:false, message:"That change could not be read."}`.
- Both undefined → `{ok:true, message:"Nothing to change."}`.
- `updateMatch(user.id, matchId, data)` (`queries/matches.ts:291-306`): sets only provided
  keys + `updated_at`; none → `{ok:false, message:"That match no longer exists."}`.
- Revalidate `/matches` layout + `/home`. `{ok:true, message:"Saved."}`.
- Client: `match-controls.tsx:96-111` (notes autosave after 800 ms debounce; sends `notes:
  value.trim() ? value : null`). `userRating` has no UI caller today.

### 4.8 `evaluateMatchAction(matchId: string)`
`matches.ts:104-145`. Returns `{ok:true, runId, message}` at once.
- `z.uuid()` failure → `{ok:false, message:"That match does not exist."}`.
- `startMatchEvaluation(user.id, [matchId])` (`src/lib/runs/evaluate-run.ts:515-563`):
  `getMatchRows` for the deduped UUID ids → none ⇒ `UnknownMatchError`; no parseable base
  resume ⇒ `NoResumeError` ("There is no resume on file to evaluate against. Upload one on the
  profile page first.", `src/lib/ranking/deep-eval.ts:305-312`); `ids` sorted; reuse an
  existing run: `SELECT * FROM runs WHERE user_id AND kind='rank_deep' AND status IN
  ('queued','running') AND input->'matchIds' = $ids::jsonb LIMIT 1`; else `INSERT runs (kind
  'rank_deep', status 'queued', input {matchIds}, conversation_id null)` and `send("match.evaluate",
  { userId, runId, matchIds })`; on send failure `finishActiveRun(failed, "Could not queue the
  evaluation.")` and rethrow.
- Revalidate `/matches` layout + `/home`.
- Message: reused → "Already evaluating <title> at <company>."; else "Evaluating <title> at
  <company><cost ? ` (${cost})` : ''>. This takes about half a minute." where cost =
  `costPhrase(estimateEvalCost(1).perItemUsd)` (`deep-eval.ts:453-479, 505-509`: null → none,
  < $0.01 → "under a cent", else "about $X.XX").
- Errors: `NoResumeError` → its message; `UnknownMatchError` → "That match does not exist.";
  other → log + "Could not start the evaluation just now. Try again in a moment."
- Client: `match-controls.tsx:68-93` (`EvaluateButton` → shows `<RunCard runId>`).

### 4.9 `pingWorker()`
`src/app/actions/ping.ts:8-15`.
- `send("ping", { userId, note: "The worker answered a test job from Settings." })` → returns
  `{ jobId: string|null }`. Throws on queue failure (client catches). No revalidate.
- Client: `src/components/settings/worker-panel.tsx:55-67` (then nudges live refresh every
  1.5 s until the worker's event arrives or patience runs out).

### 4.10 `saveProfile(next: unknown)`
`src/app/actions/profile.ts:29-55`. Profile (resume) editor save.
- `ResumeSchema.safeParse(next)` (full schema: `basics{name, headline?, email?, phone?,
  location?, links[{id, source, label, url}]}`, `summary?`, `work[]`, `education[]`,
  `projects[]`, `skills[]`, every item with `id: string` and `source:
  "resume"|"linkedin_pdf"|"user"` defaulting to `"resume"`; dates are strings 1-40 chars;
  `src/lib/resume/schema.ts:12-93`). Failure → `{ok:false, error: firstIssue.message ??
  "Invalid profile."}` — note the key is **`error`**, not `message`.
- `getProfile(user.id)` none → `{ok:false, error:"Upload a resume before editing your
  profile."}`.
- `stamped = stampUserEdits(parsed, ResumeSchema.safeParse(existing.data).data ?? null)`
  (`src/lib/resume/edits.ts:67-81`): for links/work/education/projects/skills, an item whose
  id is new or whose content (ignoring `id`/`source`, key-order-independent, `undefined` keys
  dropped) differs from the stored item becomes `source:"user"`; untouched items keep the
  stored `source`.
- `saveEditedResume(user.id, stamped)` (`queries/profile.ts:129-133`): if the latest base
  resume's data equals it (`sameJson`: key-sorted, null/undefined keys dropped) → no write;
  else `saveBaseResume` (new `resumes(base)` version + mirror into `profiles.data`). DESIGN.md
  invariant 13.
- Revalidate `/profile`. Returns `{ ok: true }`.
- Client: `src/components/profile/profile-editor.tsx:83-98` (`prune(resume)` first; toast
  "Profile saved").

### 4.11 `savePreferencesAction(input: Preferences)`
`profile.ts:65-110`.
- `PreferencesSchema.safeParse(input)` (`src/lib/profile/interview.ts:46-56`): every field
  `nullable().default(null)`: `target_roles: string[]`, `seniority: "intern"|"new_grad"|
  "entry"|"mid"|"senior"|"staff_plus"`, `locations: string[]`, `remote_pref:
  "remote"|"hybrid"|"onsite"|"any"`, `salary_floor: { amount: number, currency: string,
  period: "year"|"hour" }`, `work_auth: string`, `start_date: string`, `must_haves: string[]`,
  `dealbreakers: string[]`. Failure → find the slot whose `fields` contains `issue.path[0]`
  (`SLOTS`, `interview.ts:99-163`) → `{ok:false, message:"<slot.label> could not be saved:
  <issue.message ?? 'the value is not valid'>."}` else "Those preferences could not be saved.
  Check the fields and try again."
- No profile → `{ok:false, message:"Upload your resume first. Your preferences are saved on
  the profile it creates."}`.
- `normalize` (`profile.ts:113-145`): lists → each `trim().slice(0,200)`, drop empty, dedupe
  case-insensitively (first wins), max 40, empty ⇒ `null`; text fields (`work_auth`,
  `start_date`) → `trim().slice(0,500) || null`; `salary_floor` kept only if `amount` finite
  and `> 0`, with `currency.trim() || "USD"`; enums pass through. Written **whole**, not
  merged (an emptied field means "clear").
- Interview state: `InterviewStateSchema` (`interview.ts:165-169`: `{version: number = 1,
  skipped: string[] = []}`) parsed or `EMPTY_INTERVIEW_STATE`; `skipped` keeps ids that are
  unknown slots or not now filled (`isFilled`, `interview.ts:179-186`, true if **any** of the
  slot's fields is non-null and, for arrays, non-empty — the doc comment says "every").
- `UPDATE profiles SET preferences=$p, interview_state={...state, skipped}, updated_at=now()
  WHERE user_id=$1` (inline Drizzle, `profile.ts:98-105`).
- Revalidate `/profile`, `/home`. Returns `{ok:true, message:"Preferences saved"}`.
- Client: `src/components/profile/preferences-form.tsx:151-162`.

### 4.12 `tailorResumeAction(matchId: string)`
`src/app/actions/resumes.ts:28-40`.
- `typeof matchId !== "string" || !matchId.trim()` → `{ok:false, message:"No job was given to
  tailor for."}`.
- `startTailorRun(user.id, matchId)` (`src/lib/runs/tailor-run.ts:289-356`):
  - no base resume → `{ok:false, message:"There is no resume on file to tailor. Upload one on
    your profile first."}`;
  - resolve `id` as match id (UUID) or job id (`resolveMatch`, `queries/matches.ts:197-212`);
    if it is a job id with no match, `ensureMatch` creates one (`stage 'found'`); still none →
    `{ok:false, message:"I couldn't find that job."}`;
  - dedupe: a `tailor` run for this match in `queued|running` created within the last 15 min
    (`input->>'matchId' = $id`, `tailor-run.ts:265-283`) → `{ok:true, runId: active.id,
    matchId, message:"Already tailoring your resume for <title> at <company>. It will be ready
    in about a minute."}`;
  - `INSERT runs (kind 'tailor', mode 'approve_each', status 'queued', input { matchId, prompt:
    "Tailor your resume for <title> at <company>" }, conversation_id null)`;
    `send("resume.tailor", { userId, runId, matchId })`; send failure → `finishRun(failed,
    "Could not queue the job.")` + `{ok:false, message:"Could not start tailoring right now.
    Try again in a moment."}`;
  - ok message: "Tailoring your resume for <label>. It takes about a minute, and every change
    is checked against your original resume before it is saved."
- On ok: revalidate `/profile`, `/matches` layout, `/home`; return `{ ok:true, runId, message }`
  (drops `matchId`).
- Client: `src/components/matches/tailor-panel.tsx:71-83` (shows `<RunCard runId>` until the
  page re-renders with a new tailored resume).

### 4.13 `renderBaseResumeAction()`
`resumes.ts:43-57`.
- `getBaseResume(user.id)` (`queries/profile.ts:20-28`, latest `resumes(kind='base')` by
  version) none → `{ok:false, message:"There is no resume on file yet. Upload one first."}`.
- `startResumeRender(user.id, base.id)` (`tailor-run.ts:363-399`): `getResume` none →
  `{ok:false, message:"That resume no longer exists."}`; `INSERT runs (kind 'resume_render',
  mode 'approve_each', status 'queued', input { resumeId, prompt: base ? "Render your resume to
  PDF" : "Render your tailored resume to PDF" })`; `send("resume.render", { userId, runId,
  resumeId })`; send failure → run failed + "Could not start rendering right now. Try again in
  a moment."; ok → `{ok:true, runId, message:"Rendering your resume to PDF. It takes a few
  seconds."}`.
- On ok revalidate `/profile`, `/matches` layout, `/home`.
- Client: `src/components/profile/profile-header.tsx:36-46`.

### 4.14 `startSearchAction(input: { prompt?: string; mode?: AutonomyMode; againFrom?: string } = {})`
`src/app/actions/runs.ts:22-87`. Button-started searches (Home, run-again).
- `assertCanSearch(user.id)` (`src/lib/runs/search-run.ts:122-138`): parses stored
  preferences/interview state; if any **required** slot (target_roles, seniority, locations,
  remote_pref, work_auth) is not filled → `PreferencesIncompleteError(missing labels)` →
  `{ok:false, message:"Foray still needs your <labels joined ', ', lowercased> before it can
  search. Add them on your profile or tell Foray in chat."}`. Other errors rethrow.
- `activeSearch(user.id)` (`runs.ts:40-54`: newest `kind='search'` run in
  `queued|running|awaiting_approval`) → `{ok:false, message: status==='awaiting_approval' ? "A
  search is already waiting on your go-ahead. Answer it on Home first." : "A search is already
  running. It will finish in a minute or two."}` (one search in flight per user).
- `settings = getSettings(user.id)` (`src/lib/profile/settings.ts:38-45`); `earlier = againFrom
  ? getRun(user.id, againFrom) : null`; `previous = earlier.kind==='search' ? earlier.input :
  null`; `mode = input.mode ?? earlier?.mode ?? settings.defaultMode` (mode is not
  runtime-validated).
- `createRun({ userId, kind:'search', mode, status:'queued', input: { prompt:
  input.prompt?.trim() || previous?.prompt || "Search with my saved preferences"
  (SAVED_PREFERENCES_PROMPT, brief.ts:47), hints: previous?.hints ?? null } })` — brief is
  re-derived by the worker, not copied (`runs.ts:70-72`). `conversation_id` null, so no chat
  follow-up for button-started runs.
- `send("search", { userId, runId })` (a send failure throws — the run stays `queued`).
- Revalidate `/home`. Returns `{ok:true, runId, message: mode==='autopilot' ? "Search started
  on autopilot." : "Search started. Foray will check with you before the deep evaluation."}`.
- Clients: `src/components/home/live-panels.tsx:120-133` (no args), `run-card.tsx:251-265`
  (`{ againFrom: runId }` on failed/cancelled searches).

### 4.15 `cancelRunAction(runId: string)`
`runs.ts:89-99`.
- `requestCancel(user.id, runId)` (`src/lib/runs/runs.ts:113-141`): UUID check; `UPDATE runs
  SET status='cancelled', finished_at=now(), updated_at=now() WHERE id AND user_id AND status
  IN ('queued','running','awaiting_approval') RETURNING *`; if updated,
  `expireRunApprovals(runId)` (all `pending` → `expired`, no continuation); if any expired,
  `emit({kind:"run.cancelled", level:"warn", message:"Cancelled while waiting for your answer.
  Everything found so far was kept."})`. Cancellation is cooperative: the running handler
  polls `isCancelled` between batches; the pg-boss job is not cancelled.
- Revalidate `/home`, `/activity` (always). Returns `{ok:true, message:"Stopped. Everything
  found so far was kept."}` or `{ok:false, message:"That run had already finished."}`.
- Client: `run-card.tsx:209-223` (Stop button; result ignored, `requestLiveRefresh()`).

### 4.16 `saveSettingsAction(patch: Partial<Settings>)`
`src/app/actions/settings.ts:12-38`.
- `SettingsSchema.partial().strict()` (`settings.ts:18-28`: `defaultMode:
  "approve_each"|"autopilot"` default approve_each; `scoreThreshold: int 0-100` default 55;
  `deepEvalTopN: int 0-60` default 15); unknown keys rejected. Failure → `{ok:false,
  message:"Those settings are out of range. Check the values and try again."}`.
- Keep only keys present in `patch` (so schema defaults never overwrite unsent fields,
  `settings.ts:22-26`).
- `updateSettings(user.id, sent)` (`settings.ts:52-66`): read current (parsed, defaults
  filled), merge, full-parse, `UPDATE profiles SET settings, updated_at WHERE user_id RETURNING
  id`; no row → `{ok:false, message:"Upload your resume first. Settings are saved on the profile
  it creates."}`.
- Revalidate `/settings`. Returns `{ok:true, message:"Settings saved", settings: Settings}`.
- Client: `src/components/settings/behaviour-form.tsx:61-75` (sends all three fields).

---

## 5. Things emitted (agent_events) directly by this surface

`emit()` (`src/lib/runs/events.ts:28-50`) inserts into `agent_events` and, when the event
has a `runId` and its kind is in `FOLLOW_UP_KINDS` (`approval.requested`, `run.completed`,
`run.failed`, `tailor.done`, `tailor.failed`; `src/lib/chat/follow-up-kinds.ts:10-16`) and
the run has a `conversation_id`, enqueues `chat.follow_up` `{userId, runId, eventId}` with
`singletonKey: "follow-up-<eventId>"` (`events.ts:57-74`, errors swallowed).
From HTTP entry points: `approval.decided`, `approval.reopened` (4.1), `approval.expired`
(3.6 first poll, and inside 4.1), `run.cancelled` (4.15). None of these are follow-up kinds.

---

## 6. Queues enqueued from the HTTP surface

Names and payloads `src/lib/queue/queues.ts:8-110`; policies `src/lib/queue/boss.ts:43-56`.

| Queue | Sent by | Payload | retryLimit / expireIn |
|---|---|---|---|
| `ping` | `pingWorker` | `{ userId, note }` | 0 / 60 s |
| `company.refresh` | `refreshAllAction` | `{ userId, all: false }` | 0 / 60 min |
| `search` | `startSearchAction` | `{ userId, runId }` | 0 / 60 min |
| `search.evaluate`, `search.shortlist` | `decideApprovalAction`, `/api/live` first poll (approval continuations) | `{ userId, runId, approvalId }` | 0 / 60 min; 0 / 5 min |
| `match.evaluate` | `evaluateMatchAction` | `{ userId, runId, matchIds }` | 0 / 30 min |
| `resume.tailor` | `tailorResumeAction` | `{ userId, runId, matchId }` | 0 / 30 min |
| `resume.render` | `renderBaseResumeAction` | `{ userId, runId, resumeId }` | 1 / 10 min |
| any of the above | `POST /api/chat` via agent tools | (agents area) | |

---

## 7. Client call-site index

| Client file:line | Entry point |
|---|---|
| `components/chat/chat.tsx:67-89` | `POST /api/chat` (SSE) |
| `components/chat/chat.tsx:105-110` | `GET /api/conversations/[id]` |
| `components/shell/recent-chats.tsx:35-40` | `GET /api/conversations` |
| `components/shell/recent-chats.tsx:53-57` | `DELETE /api/conversations/[id]` |
| `components/live/live-provider.tsx:91` | `GET /api/live?since=` |
| `components/live/live-provider.tsx:295` | `GET /api/runs/[id]` |
| `components/activity/run-list.tsx:176` | `GET /api/runs/[id]/events` |
| `components/runs/run-card.tsx:397` | `GET /api/matches?ids=` |
| `components/runs/run-card.tsx:352,363`; `matches/tailor-panel.tsx:146,155`; `profile/profile-header.tsx:53` | `GET /api/documents/[id]` (`?download=1`) |
| `components/matches/tailor-panel.tsx:136` | `GET /api/resumes/[id]/preview` (iframe) |
| `components/profile/resume-dropzone.tsx:51` | `POST /api/uploads` |
| `components/auth/sign-in-form.tsx:35,36,129` | `/api/auth/sign-up/email`, `/sign-in/email`, `/sign-in/social` |
| `components/settings/sign-out-button.tsx:21`; `shell/user-menu.tsx:83` | `/api/auth/sign-out` |
| `components/runs/approval-card.tsx:86` | `decideApprovalAction` |
| `components/companies/add-company.tsx:59` | `addCompanyAction` |
| `components/companies/company-table.tsx:173-174` | `refreshCompanyAction`, `setStatusAction` |
| `components/companies/refresh-stale-button.tsx:39` | `refreshAllAction` |
| `components/matches/board.tsx:104,116`; `match-table.tsx:84`; `match-controls.tsx:42` | `moveMatchStageAction` |
| `components/matches/match-controls.tsx:103` | `updateMatchAction` |
| `components/matches/match-controls.tsx:76` | `evaluateMatchAction` |
| `components/settings/worker-panel.tsx:59` | `pingWorker` |
| `components/profile/profile-editor.tsx:87` | `saveProfile` |
| `components/profile/preferences-form.tsx:154` | `savePreferencesAction` |
| `components/matches/tailor-panel.tsx:73` | `tailorResumeAction` |
| `components/profile/profile-header.tsx:38` | `renderBaseResumeAction` |
| `components/home/live-panels.tsx:125`; `runs/run-card.tsx:258` | `startSearchAction` |
| `components/runs/run-card.tsx:216` | `cancelRunAction` |
| `components/settings/behaviour-form.tsx:64` | `saveSettingsAction` |

Server pages (`src/app/(app)/**/page.tsx`) read the database directly through `src/lib`
(no HTTP): activity (runs, llm_calls, resumes, match rows), companies (companies + job
counts), home (stage counts, matches, spend, approvals, runs, onboarding), matches (list,
counts, onboarding), matches/[id] (match detail, base resume, tailored resumes), profile
(base resume, latest render, onboarding), settings (profile, models, pricing, jsearch
budget), chat (conversation + messages, suggestions). After the split each of these becomes
a read endpoint (pages area).

---

## 8. Next-specific behaviour a Django backend must replace

1. **`redirect("/sign-in")` inside `requireUser`** — 307 for route handlers, navigation for
   actions. Replace with 401 JSON for API calls (the live poller already handles 401) and a
   frontend-side redirect.
2. **`headers()` / Better Auth `getSession`** — replace with Django session middleware that
   reads `better-auth.session_token` (or a Django session) and loads `user`. To keep existing
   sessions valid during cut-over: verify `token.base64(HMAC_SHA256(BETTER_AUTH_SECRET, token))`,
   look up `session.token`, check `expires_at`, slide expiry when older than 1 day.
3. **Better Auth endpoints** — sign-up/sign-in/sign-out/social (2.2) including scrypt hash
   compatibility, email lowercasing, error messages, rate limits, Origin checks.
4. **Server actions → JSON endpoints** (16 of them). Keep the `{ ok, message, … }` result
   shapes verbatim so the components only change their transport. CSRF: actions relied on
   Next's Origin check; Django needs CSRF tokens or an Origin/`SameSite=Lax` policy.
5. **`revalidatePath`** (11 actions) — after the split the frontend must refresh RSC data
   itself (`router.refresh()`), or Next keeps thin actions that call Django and then
   `revalidatePath` the same paths (section 4 lists them).
6. **Streaming** — `/api/chat` is an SSE response produced by the AI SDK's agent loop; Django
   needs ASGI + `StreamingHttpResponse` over an async generator emitting the UI Message
   Stream v1 chunk protocol so `@ai-sdk/react` `useChat` keeps working, plus an "on end,
   including client disconnect" hook to persist the assistant message.
7. **AsyncLocalStorage LLM attribution** (`withLlmContext`) → Python `contextvars`, which
   must survive into the streaming generator.
8. **`formData()` + `File`** in `/api/uploads` → `request.FILES["file"]`
   (`UploadedFile.content_type`, `.size`, `.name`).
9. **`notFound()`** in pages; `NextResponse.json`; `new Response(Uint8Array)` →
   `HttpResponse/FileResponse` with the exact headers listed.
10. **Global singletons on `globalThis`** (pg-boss sender, DB pool) — Django app-level
    singletons or per-process init; pg-boss itself is Node-only (see hazards).

---

## 9. Hazards, quirks and latent bugs to decide on

- **Cross-tenant message overwrite (security):** `saveMessages` upserts `ON CONFLICT (id) DO
  UPDATE SET parts, metadata` with no `user_id`/`conversation_id` guard
  (`conversations.ts:123-142`), and `/api/chat` persists the client-supplied `message.id`
  verbatim. A client that sends an existing message id (its own older one, or in principle
  another user's) overwrites that row's parts. The route's comment that the client "cannot
  rewrite what was already said" (`route.ts:17-19`) does not hold for parts. The port should
  scope the upsert (`WHERE messages.user_id = excluded.user_id AND conversation_id = …`) or
  mint user-message ids server-side.
- **Unvalidated role:** the incoming message's `role` is not checked; a client can persist a
  `system` or `assistant` message into history (AI SDK validation accepts all three roles).
- **User message saved before validation:** `saveMessages([incoming])` runs before
  `validateUIMessagesForAgent`; a malformed message is persisted and then the request 500s.
- **Regenerate leaves the failed reply:** `regenerate()` re-sends the same user message id
  (upsert, fine) but the earlier partial/failed assistant message stays in the DB, so a reload
  shows both.
- **Conversation id fallback mismatch:** if the client's chosen id is not a UUID or is held by
  another user, the server creates a different conversation but the client still rewrites its
  URL to `/chat/<its id>` (404 on reload). `x-conversation-id` is sent but unread.
- **No chat timeout:** DESIGN.md invariant 12 mentions a timeout; only `isStepCount(8)` exists.
- **Inline heavy work in requests** (contradicts invariant 12 / "heavy work is a pg-boss
  job"): `/api/uploads` (LLM extraction, 10-30 s, plus vision fallback), `addCompanyAction`
  (discovery HTTP + a full board fetch + ingest), `refreshCompanyAction` (board fetch; for
  `careers_page` an LLM extraction). A sync Django worker with a 30 s timeout will kill these;
  either run them under ASGI with generous timeouts or move them to jobs with a polled result.
- **GET with side effects:** `/api/live?since=0` expires approvals, emits events and enqueues
  continuations. Must be preserved (or moved to a scheduler) or week-old paused runs never
  finish.
- **Redirect instead of 401** for unauthenticated `fetch` calls (all route handlers): only the
  live poller copes. Chat's `useChat` would try to parse HTML as SSE.
- **`/api/matches` id filter is looser than UUID** (`/^[0-9a-f-]{36}$/i`): e.g. 36 dashes
  passes and reaches Postgres as an invalid uuid → 500. Validate strictly in the port.
- **Runtime-unvalidated action args:** `setStatusAction(status)` and `startSearchAction({mode,
  prompt})` trust the TypeScript types; bad values hit enum columns.
- **`saveProfile` returns `error`, every other action returns `message`.**
- **Error message texts come from zod** (`saveProfile` first issue, `savePreferencesAction`
  issue text) — pydantic's wording will differ; the UI shows them verbatim.
- **Upload orphans:** the file is written before extraction; on extraction failure, or on a
  duplicate (same sha256) upload, the new copy is never referenced.
- **`linkedin_pdf` accepts DOCX too** (`kind` hint is honoured for either mime).
- **`decideApprovalAction` revalidates even on failure results**; harmless but part of
  current behaviour.
- **`isFilled` uses `some`, doc says "every"** (`interview.ts:178-186`) — affects which
  skipped slots are cleared and which searches are blocked.
- **`frame-ancestors 'self'`** on the preview: if Django serves `/api/**` on a different origin
  than the Next frontend, the iframe will be blocked; keep a same-origin reverse proxy (Next
  rewrites or a front proxy) or list the frontend origin.
- **pg-boss is a Node library**: the Django side must either enqueue by writing pg-boss's
  `pgboss.job` table directly (schema-version-specific, notify channel) or the queue moves to
  a Python system (Celery/Procrastinate/Django-Q) together with the worker.
- **Better Auth cookie/secret coupling:** the cookie signature uses `BETTER_AUTH_SECRET` (or
  the library default when unset in dev); sessions minted by `scripts/dev-session.ts` depend
  on the same format.
- **Server-action body limit 1 MB** (default) currently caps `saveProfile` payloads; a Django
  endpoint would need its own limit (`DATA_UPLOAD_MAX_MEMORY_SIZE` defaults to 2.5 MB).
- **Date format:** clients parse with `new Date(...)`; Python `isoformat()` (`+00:00`,
  microseconds) parses fine, but `cursor` must stay an integer and `agent_events.id` must stay
  a monotonic bigserial for `since` polling.

---

## 10. Suggested Django shape (for the design step)

- Keep the URL space: `/api/chat`, `/api/conversations[/<uuid>]`, `/api/documents/<uuid>`,
  `/api/live`, `/api/matches`, `/api/resumes/<uuid>/preview`, `/api/runs/<uuid>[/events]`,
  `/api/uploads`, `/api/auth/...`. Add one JSON endpoint per server action, e.g.
  `POST /api/approvals/<id>/decide`, `POST /api/companies`, `POST
  /api/companies/<slug>/status`, `POST /api/companies/<slug>/refresh`, `POST
  /api/companies/refresh-stale`, `POST /api/matches/<id>/stage`, `PATCH /api/matches/<id>`,
  `POST /api/matches/<id>/evaluate`, `POST /api/worker/ping`, `PUT /api/profile/resume`, `PUT
  /api/profile/preferences`, `POST /api/matches/<id>/tailor`, `POST /api/resume/render`, `POST
  /api/searches`, `POST /api/runs/<id>/cancel`, `PATCH /api/settings` — returning the same
  `{ ok, message, ... }` bodies with HTTP 200 (the components branch on `ok`, not on status).
- Run under ASGI (uvicorn/daphne) for the SSE chat stream and long inline calls.
