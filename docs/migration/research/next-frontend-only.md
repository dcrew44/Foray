# Next 16.3 as a frontend in front of Django

Research for the Foray strangler migration (Next keeps pages and client components; Django takes
the HTTP API, auth, chat agent and worker). Date: 2026-10-04.

Evidence labels used throughout:

- **[E]** executed here, output quoted
- **[S]** read in the installed package source (path given)
- **[D]** documentation claim only (bundled Next docs or vendor docs), not executed

## Versions

| Component | Version | How it was used |
|---|---|---|
| next | 16.3.5 (Turbopack) | scratch app `web/` (pnpm install) and the repo's `node_modules/next` for source reading |
| react / react-dom | 19.2.8 | scratch app |
| ai (AI SDK) | 7.0.106 | the repo's copy; header constants and the client-side stream parser |
| playwright-core | 1.63.0 | the repo's copy, driving system Google Chrome 154 headless |
| Django | 6.1.1 | single-file ASGI origin |
| uvicorn | 0.54.0 | ASGI server for the origin |
| granian 2.8.4, httpx2 2.13.1 | installed but not used in the results below |
| Python | 3.14.7 | |
| Node | v26.0.0 (the machine's node; the repo requires >=24) | |
| curl | 8.7.1 | `curl -N` timing |

Scratch workspace: `research/next-frontend-only/` next to this file. It contains `origin/app.py` (the Django
origin), `web/` (the Next app), `sse_time.sh` (curl -N with per-line timestamps), `battery.sh`,
`drive.mjs` (browser scenarios) and `heartbeat.mjs`.

## TL;DR: recommended pattern for each question

1. **Forwarding.** Use `next.config.ts` rewrites with **`fallback`** for `/api/:path*` pointing at Django.
   This is the only rewrite phase where every Next route handler still in the repo, static or dynamic,
   wins over the rewrite [E]. Porting a slice then means: implement it in Django, delete the Next
   `route.ts`, and the path falls through to Django automatically. Keep `src/proxy.ts` as the optimistic
   sign-in redirect only, checking Django's session cookie name, and do not forward from it. It goes
   through the same httpxy code path as a rewrite, so it has the same gzip, timeout and body-size traps,
   and it can't express "only if Next has no route" [E][S].
2. **Streaming and uploads through a rewrite** work without buffering, but only once three defaults are
   overridden. Each one broke in every mode tested (`next dev`, `next start`, standalone `server.js`) [E]:
   - **Gzip buffers SSE.** Next's `compression` middleware wraps proxied responses and never flushes
     them, so a browser (which sends `Accept-Encoding: gzip, br, zstd`) received all events at the end.
     Fix: Django sends **`Cache-Control: no-cache, no-transform`** on every streamed response, and/or
     set `compress: false` in Next and let Caddy compress. The AI SDK's own headers send only
     `cache-control: no-cache`, so a Django port that copies them verbatim will be buffered.
   - **30s idle timeout.** The rewrite proxy destroys the upstream socket after 30s with no bytes
     (`curl: (18)` at 30.05s). Fix: set `experimental.proxyTimeout` (ms), and have Django send SSE comment
     heartbeats (`: ping\n\n`). AI SDK 7's client ignores them [E].
   - **Request bodies over 10MB are truncated, which surfaces as a 500**, on every external rewrite, even
     with no `proxy.ts` at all [E]. The setting is `experimental.proxyClientMaxBodySize`. Contrary to its
     doc, it applies to all external rewrites, not only to proxy. Set it above the largest upload plus
     multipart overhead (Foray's resume limit is exactly 10MB, so use about 12–16mb).
   - Set-Cookie (multiple, with HttpOnly, Secure, SameSite, Max-Age) passes through unchanged. Relative
     `Location` redirects pass through unchanged. The client's `Cookie` header is forwarded [E].
   - A client abort reaches Django: the async generator gets `CancelledError` within milliseconds [E].
3. **Server components** read Django through a `server-only` helper. It calls `cookies()` (which makes the
   route dynamic), forwards the cookie header, `Origin`, `X-Forwarded-Host` and `X-CSRFToken`, and uses
   `cache: 'no-store'`. GET fetches are still memoized within one render, so a layout and a page calling
   `/api/me` cost one Django call [E]. **Never fetch Django from a server component without a
   request-time API.** Next prerenders it at build: the data is frozen with `s-maxage=31536000`, and the
   build fails if Django is down [E]. Don't adopt `cacheComponents` or `'use cache'` for per-user data
   [D]. **Mutations:** keep Foray's existing call sites by turning each server action into a *thin
   forwarder* (Django call plus `revalidatePath`), which refreshes the page in the same round trip [E].
   Auth calls (sign in and out) go **client → `/api/auth/*` (rewrite) → Django** so the browser gets
   Django's Set-Cookie natively, followed by `router.refresh()` [E]. When the worker changes data in
   another process, nothing on the Next server needs invalidating, because every page is dynamic. The
   existing `useLive()` → `router.refresh()` path and `staleTimes.dynamic = 0` already pick it up [E].
4. **Production topology.** Use **Caddy in front of both**: `/api/*` and Django's admin and static files
   go straight to Django, everything else goes to Next standalone. Next's server-side fetches go to Django
   over the internal network. Keep the Next `fallback` rewrite as well, so `pnpm dev` works with no Caddy
   and the transition works before Caddy exists. Caddy flushes `text/event-stream` immediately and
   compresses it while streaming [D]. It sets X-Forwarded-For and X-Forwarded-Proto and passes `Host`
   through, so Django's CSRF Origin check works without extra settings [D]. Next-only (T1) works too
   [E], but every API byte, upload and SSE stream then runs through Node, with the caveats above. Next
   also adds no X-Forwarded-For, so the client IP is lost or spoofable [E].

---

## 1. Forwarding mechanisms

### 1a. Rewrite phases and route precedence

[D] Order, from `node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/proxy.md`
("Execution order") and `.../next-config-js/rewrites.md`: headers → redirects → **proxy** →
`beforeFiles` → filesystem (public, `_next/static`, non-dynamic pages and routes) → `afterFiles` →
dynamic routes → `fallback`.

[E] The scratch app had a static Next route handler `app/api/ping/route.ts` and a dynamic one
`app/api/things/[id]/route.ts`. Neither exists in Django. `/api/echo` and `/api/upload` exist only in
Django. One rewrite rule, `{ source: "/api/:path*", destination: "http://127.0.0.1:8100/api/:path*" }`,
was placed in each phase in turn (`next dev`):

```
## api-before
  GET /api/ping      -> <!DOCTYPE html>...      (Django 404: Next route shadowed)
  GET /api/things/7  -> <!DOCTYPE html>...      (Django 404: Next route shadowed)
  GET /api/echo      -> {"method": "GET", "path": "/api/echo", ...}
## api-after
  GET /api/ping      -> {"from":"next-static-route"}
  GET /api/things/7  -> <!DOCTYPE html>...      (dynamic Next route shadowed!)
  GET /api/echo      -> {"method": "GET", ...}
## api-fallback
  GET /api/ping      -> {"from":"next-static-route"}
  GET /api/things/7  -> {"from":"next-dynamic-route","id":"7"}
  GET /api/echo      -> {"method": "GET", ...}
  POST /api/upload   -> {"received": 1048576, ...}
```

Foray has dynamic API routes (`/api/runs/[id]`, `/api/runs/[id]/events`, `/api/conversations/[id]`,
`/api/documents/[id]`, `/api/resumes/[id]/preview`, `/api/auth/[...all]`), so `afterFiles` would shadow them.
**Use `fallback`.** Limit the source to `/api/:path*` (not `/:path*`), so unknown *pages* still render
Next's `not-found.tsx` instead of Django's 404.

### 1b. Rewrite destinations are baked at build time

[E] In the standalone build, `.next/routes-manifest.json` contains the literal destination:

```
routes-manifest rewrites: {"beforeFiles": [["/dj/:path*", "http://127.0.0.1:8100/:path*"]], "afterFiles": [], "fallback": []}
```

[E] The standalone server was then started with `DJANGO_ORIGIN=http://127.0.0.1:8200` at runtime, and a
second origin ran on :8200:

```
dj  -> Django host seen: 127.0.0.1:8100   <- next.config rewrite: build-time value
px  -> Django host seen: 127.0.0.1:8200   <- proxy.ts NextResponse.rewrite: runtime env
rh  -> Django host seen: 127.0.0.1:8200   <- route handler fetch: runtime env
```

Consequence for the repo's `Dockerfile`: `pnpm build` runs in the `build` stage, so pass
`ARG DJANGO_ORIGIN=http://api:8000` (the compose service name) to that stage. `pnpm dev` evaluates
`next.config.ts` at startup, so local dev just uses `http://127.0.0.1:8000`. If the internal hostname must
vary per deploy without a rebuild, that is the one reason to forward from `proxy.ts` instead (it reads
`process.env` at runtime). It has the same traps as a rewrite.

### 1c. Can `proxy.ts` forward instead?

Yes: `return NextResponse.rewrite(new URL(path + search, DJANGO))`. [S]
`next/dist/server/lib/router-server.js:417` sends *any* external rewrite, whether it came from
`next.config` or from proxy, through the same `proxyRequest()` in
`next/dist/server/lib/router-utils/proxy-request.js`. So the behavior is identical [E]: gzip buffering at
3.05s, 10MB truncation, 30s timeout. Downsides: the matcher must be static constants; proxy runs
*before* the filesystem, so it can't express "Django only if Next has no route"; and the Next docs call
proxy a last resort. **Recommendation: proxy.ts keeps only the optimistic cookie-presence redirect.**
Switch `getSessionCookie` (Better Auth) to a check of Django's session cookie name when auth moves.

### 1d. The third option: a route handler pass-through

`app/<prefix>/[...path]/route.ts` returning `new Response(upstream.body, …)` streamed SSE correctly even
with gzip accepted. Next's `pipeToNodeResponse` calls `res.flush()` per chunk ([S]
`next/dist/server/pipe-readable.js:83,172`). It had no 30s cutoff, and it uploaded 60MB intact with no
config [E]. Costs: hand-written hop-by-hop header handling (undici throws
`NotSupportedError: expect header not supported` if `Expect` is forwarded [E]); it must drop
`content-encoding`/`content-length` after `fetch()` decodes; it misses the `fallback` semantics; and
undici's own `bodyTimeout` default of 300s still applies [D, not tested]. It is a fallback option if a
specific path needs custom logic, not the default.

## 2. Streaming, uploads, cookies through the rewrite

Origin endpoint (Django 6.1.1 ASGI, uvicorn), from `origin/app.py`:

```python
async def sse(request):
    ...
    async def gen():
        for i in range(n):
            if i: await asyncio.sleep(interval)
            yield f"data: {json.dumps({'i': i, 't': ...})}\n\n".encode()
    resp = StreamingHttpResponse(gen(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache, no-transform" if request.GET.get("nt") == "1" else "no-cache"
    resp["X-Accel-Buffering"] = "no"
    resp["x-vercel-ai-ui-message-stream"] = "v1"
    return resp
```

Timing tool (`sse_time.sh`): `curl -sS -N "$@" "$URL"` piped into a Python line timestamper.
"Browser AE" means `--compressed -H 'Accept-Encoding: gzip, deflate, br, zstd'`.

### 2a. SSE results (3 events, 1s apart)

| Path | Mode | No Accept-Encoding | Browser AE, `no-cache` (AI SDK headers) | Browser AE, `no-cache, no-transform` |
|---|---|---|---|---|
| next.config rewrite | `next dev` | 0.02 / 1.01 / 2.02s, streams | **3.01 / 3.01 / 3.01 / 3.01 (4 events), buffered** | 0.02 / 1.01 / 2.01 / 3.01, streams |
| proxy.ts rewrite | `next dev` | – | **3.05s, all at once** | – |
| route handler | `next dev` | – | 0.54 / 1.54 / 2.54 / 3.54, streams (0.5s first compile) | – |
| next.config rewrite | `next start` | streams | **2.03s, all at once** | streams |
| proxy.ts rewrite | `next start` | – | **2.02s, all at once** | – |
| route handler | `next start` | – | 0.02 / 1.02 / 2.03, streams | – |
| next.config rewrite | standalone `server.js` | streams | **2.01s, all at once** | streams |
| `fallback` `/api` rewrite, `compress:false`, `proxyTimeout` set | standalone | – | 0.02 / 1.02 / 2.02, streams | – |

Response headers in the buffered case [E]: `content-type: text/event-stream`, `Vary: Accept-Encoding`,
`Content-Encoding: gzip`.

Cause [S]: `router-server.js:124-126,257-259` installs `next/dist/compiled/compression` on *every*
response before routing. `shouldCompress` treats `text/*` as compressible, and the only opt-out is
`Cache-Control: no-transform` (the regex `/(?:^|,)\s*?no-transform\s*?(?:,|$)/`). The httpxy proxy pipes
`proxyRes` into `res` and never calls `res.flush()`, so zlib holds the bytes until the stream ends.
Corollary: a web claim (open-notebook PR #770) that the standalone rewrite "reads the upstream body to
completion" is a misdiagnosis. The rewrite streams fine without gzip [E].

[S] AI SDK 7.0.106 `UI_MESSAGE_STREAM_HEADERS` (`ai/dist/index.js:6870`): `content-type:
text/event-stream`, `cache-control: no-cache`, `connection: keep-alive`, `x-vercel-ai-ui-message-stream:
v1`, `x-accel-buffering: no`. No `no-transform`. The Django chat endpoint must add it.

### 2b. Idle timeout

[S] `proxy-request.js:37`: `proxyTimeout: proxyTimeout === null ? undefined : proxyTimeout || 30000`.
httpxy calls `proxyReq.setTimeout(proxyTimeout, () => destroy())`, a socket *idle* timeout. The config
schema is `z.number().gte(0).optional()`, so `0` means 30000 and it can't be disabled. Set a large number.

```
## DEV rewrite, 35s silent gap after first event (default proxyTimeout)
  0.02s  data: {"i": 0, "t": 0.0}
curl: (18) transfer closed with outstanding read data remaining
  exit=18 total=30.05s
## 40 events at 1s through the same rewrite: idle, not total
 39.06s  data: {"i": 39, "t": 39.05}   exit=0
## PROXY_TIMEOUT=600000 (experimental.proxyTimeout)
  0.02s  data: {"i": 0, ...}
 35.01s  data: {"i": 1, ...}            exit=0
```

Same results under `next start` (30.05s, exit 18) [E]. A chat turn whose tool call (a search run, a deep
eval) goes quiet for over 30s would be cut mid-reply without this setting.

Heartbeats are safe for `useChat` [E] (`heartbeat.mjs`, using `DefaultChatTransport` plus
`readUIMessageStream` from the repo's ai 7.0.106 over a body that contains `: ping\n\n` comment lines):

```
ai 7.0.106 -> [{"type":"text","text":"Hello world","state":"done"}]
```

### 2c. Client abort propagation

[E] `curl -m 2.5` on a 30-event stream. Django's generator logged:

```
## client aborts after 2.5s via /api/sse        (fallback rewrite, standalone)
[sse] stopped by CancelledError after event 3 at 2.50s
## client aborts after 2.5s via /rh/api/sse     (route handler)
[sse] stopped by CancelledError after event 3 at 2.48s
```

So the chat Stop button cancels the Django side, and therefore the LLM call, through the rewrite.

### 2d. Uploads

[E] With defaults (`next dev`, multipart, browser-like requests with no `Expect`):

```
dj   f15  -> Internal Server Error         (next.config rewrite)
dj   f60  -> Internal Server Error
rh   f15  -> {"received": 15728842, "content_length": "15728842", ...}   (route handler)
rh   f60  -> {"received": 62914762, ...}
next log: Request body exceeded 10MB for /dj/api/upload. Only the first 10MB will be available unless configured.
          Failed to proxy http://127.0.0.1:8100/api/upload Error: socket hang up  code: 'ECONNRESET'
```

The same failure with **no `proxy.ts` file at all** [E]: `f1 -> received 1048777`, `f15 -> Internal
Server Error`, one "exceeded 10MB" log line. The same under `next start` and standalone [E].

Cause [S]: `resolve-routes.js:124-125` attaches `getCloneableBody(req, proxyClientMaxBodySize)` to every
non-upgrade request. For an external rewrite, `router-server.js:417` sends
`clonableBody.cloneBodyStream()` upstream. `body-streams.js:52-110` stops pushing once the limit (default
10MB) is passed, so Django gets a body shorter than its `Content-Length` and the proxy errors out. The
`proxyClientMaxBodySize` doc says it applies only when proxy is used. The source and the runs show
otherwise.

Fix [E]: `experimental: { proxyClientMaxBodySize: '100mb' }`, then `f15`/`f60` arrive intact through both
`/dj/*` and the proxy-matched `/guarded/*`, with or without `Expect: 100-continue`. Type note [E]:
`SizeLimit` is `number | \`${number}${'b'|'kb'|'mb'|'gb'|...}\``, so an env string needs a cast.

The same tee keeps a second copy (`p2`) of the body, up to the limit, that nothing reads for external
rewrites [S]. An RSS sample during throttled 60MB uploads grew on both the rewrite and the route-handler
paths, so the run did not isolate this [E, inconclusive]. Keep the limit modest (Foray: about 16mb) and
let Caddy route uploads straight to Django in prod.

Foray impact: `src/app/api/uploads/route.ts` accepts up to exactly 10MB. A 10MB resume plus multipart
overhead exceeds 10MB, and anything between 10MB and Django's own limit would get a bare 500 instead of
the friendly 413.

### 2e. Cookies, headers, redirects

[E] `GET /dj/api/login` (Django sets three cookies):

```
set-cookie: sessionid=abc123; HttpOnly; Path=/; SameSite=Lax
set-cookie: csrftoken_demo=tok; Path=/; SameSite=Lax
set-cookie: remember=1; expires=...; HttpOnly; Max-Age=3600; Path=/; SameSite=Lax; Secure
```

The rewrite does no cookie rewriting: [S] httpxy's `cookieDomainRewrite`/`cookiePathRewrite` are not set
by Next. In headless Chrome after `fetch('/dj/api/login')` [E]:
`browser cookies: csrftoken, sessionid(HttpOnly), csrftoken_demo, remember(HttpOnly)(Secure)`. Django must
not set `Domain=` on its cookies. Host-only cookies on the public origin are what you want.

[E] What Django receives through the rewrite when the client sends no forwarding headers:

```
{'Host': '127.0.0.1:8100', 'User-Agent': 'curl/8.7.1', 'Accept': '*/*', 'X-Forwarded-Host': 'localhost:3100', 'Connection': 'keep-alive'}
get_host = 127.0.0.1:8100, is_secure = False
```

- `Host` is rewritten to the backend (`changeOrigin: true`, [S] `proxy-request.js`). Only
  `X-Forwarded-Host` is added. **No X-Forwarded-For or X-Forwarded-Proto.** A client-supplied
  `X-Forwarded-For: 203.0.113.9` was passed through verbatim [E], so it is spoofable when Next is
  internet-facing.
- Django's CSRF Origin check through the rewrite [E]:

```
default Django settings                       POST csrf-protected -> 403
  (log: Forbidden (Origin checking failed - http://localhost:3100 does not match any trusted origins.))
USE_X_FORWARDED_HOST=True                     -> 200
CSRF_TRUSTED_ORIGINS=["http://localhost:3100"] -> 200
```

  Recommend `CSRF_TRUSTED_ORIGINS = [PUBLIC_ORIGIN]` (explicit) over `USE_X_FORWARDED_HOST`.
- Redirects [E]: `HttpResponseRedirect("/sign-in?next=/home")` arrives as `302 location:
  /sign-in?next=/home`. Keep Django redirects relative. An absolute `build_absolute_uri()` would point at
  `127.0.0.1:8100` behind the rewrite.
- **Trailing slashes** [E]: `GET /dj/api/echo/` and `POST /dj/api/upload/` returned
  `308 Permanent Redirect, location: /dj/api/echo` *before* the rewrite ran. Django API URL patterns for
  proxied paths must have **no trailing slash** (Foray's current paths, `/api/live` and `/api/chat`, have
  none), or Django's `APPEND_SLASH` 301 and Next's 308 redirect-loop. The alternative is
  `skipTrailingSlashRedirect: true` in Next.

## 3. Server components, server actions, revalidation

### 3a. Reading Django from server components

[E] `next build` route table (Django up):

```
├ ƒ /action          (cookies() + fetch Django)
├ ƒ /dyn             (cookies() + fetch Django)
└ ○ /static-fetch    (fetch Django, no request-time API)
```

[E] `/static-fetch` after the build, two requests: the same build-time payload both times
(`{"hits":1, "at":1791147515.2…}`), with headers `x-nextjs-cache: HIT`, `x-nextjs-prerender: 1`,
`Cache-Control: s-maxage=31536000`. `/dyn` returned `Cache-Control: private, no-cache, no-store,
max-age=0, must-revalidate`.

[E] `next build` with Django **down**:

```
Error occurred prerendering page "/static-fetch".
[TypeError: fetch failed] { [cause]: Error: connect ECONNREFUSED 127.0.0.1:8100 }
⨯ Next.js build worker exited with code: 1
```

[D] (`docs/01-app/03-api-reference/04-functions/fetch.md`): the default is "auto no cache". It fetches
every request in dev, and once at build if the route is prerendered. Any request-time API makes the route
dynamic. `cache: 'force-cache'` is opt-in, even with cookie headers.

So every Django read must go through a helper that touches `cookies()`/`headers()`. Foray's pages already
call `requireUser()`, which reads `headers()`, so they are all dynamic today, and the helper keeps it that
way.

Helper that was run (`web/lib/django.ts`):

```ts
import "server-only";
import { cookies, headers } from "next/headers";

const DJANGO = process.env.DJANGO_ORIGIN ?? "http://127.0.0.1:8100";
const PUBLIC_ORIGIN = process.env.PUBLIC_ORIGIN ?? "http://localhost:3100";

/** Server-side call to Django as the visitor: their cookies, CSRF token, forwarded host/IP. */
export async function django(path: string, init: RequestInit = {}) {
  const jar = await cookies();
  const h = await headers();
  const out = new Headers(init.headers);
  out.set("cookie", jar.toString());
  out.set("origin", PUBLIC_ORIGIN);
  out.set("x-forwarded-host", new URL(PUBLIC_ORIGIN).host);
  const xff = h.get("x-forwarded-for");
  if (xff) out.set("x-forwarded-for", xff);
  const method = (init.method ?? "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") out.set("x-csrftoken", jar.get("csrftoken")?.value ?? "");
  return fetch(new URL(path, DJANGO), { ...init, headers: out, cache: "no-store" });
}
```

[E] Memoization: `app/memo/layout.tsx` and `app/memo/page.tsx` both `await django("/api/me")`. Django's
hit counter read `layout 1 / page 1`, then `layout 2 / page 2` on the next request, so there was one
Django call per render despite `cache: 'no-store'`. Memoization (per render pass) is not caching
[D, fetch.md]. A `requireUser()` rebuilt on `django('/api/auth/session')` in both layout and page
therefore costs one backend call. Wrap non-fetch helpers in React `cache()` for the same effect.

`'use cache'` [D] (`docs/01-app/03-api-reference/01-directives/use-cache.md`, `use-cache-private.md`):
it requires `cacheComponents: true`. Cached scopes can't call `cookies()`/`headers()` (error
`next-request-in-use-cache`, which "can pass `next build` and fail under `next start`").
`'use cache: private'` allows them but does not persist across requests. With per-user data behind a
session cookie, the server-side cache gains nothing. **Recommendation: leave `cacheComponents` off.**
Everything stays dynamic, and Django owns caching.

### 3b. What replaces server actions

Driven in headless Chrome (`drive.mjs`, the repo's playwright-core with `channel: "chrome"`). `#me`
shows Django's hit counter, which increments on every server render of `/action`. Results from the
**standalone production build** (dev gave the same results):

```
server action, no revalidate                 hits 2 -> 2   out={"status":200}
server action + revalidatePath('/action')    hits 2 -> 3   out={"status":200}
server action + refresh()                    hits 3 -> 4   out={"status":200}
client fetch via rewrite, no refresh         hits 4 -> 4   out={"status":200}
client fetch via rewrite + router.refresh()  hits 4 -> 5   out={"status":200}
Link nav away + back (2 out-of-band hits)    hits 5 -> 8
browser back + forward                       hits 8 -> 8
login via rewrite (fetch) + router.refresh() hits 8 -> 9   out={"status":200}
  browser cookies: csrftoken, sessionid(HttpOnly), csrftoken_demo, remember(HttpOnly)(Secure)
login via server action (cookies().set)      hits 9 -> 10  out={"names":["sessionid","csrftoken_demo","remember"]}
  browser cookies: sessionid(HttpOnly), csrftoken_demo, remember(HttpOnly)(Secure)
```

The thin forwarding action that ran (with Django's real `CsrfViewMiddleware` enabled):

```ts
"use server";
export async function postViaAction(mode: "none" | "revalidate" | "refresh") {
  const jar = await cookies();
  const h = await headers();
  const proto = h.get("x-forwarded-proto") ?? "http";
  const res = await fetch(`${DJANGO}/api/csrf-protected`, {
    method: "POST",
    headers: {
      cookie: jar.toString(),
      "x-csrftoken": jar.get("csrftoken")?.value ?? "",
      origin: `${proto}://${h.get("host")}`,
      "x-forwarded-host": h.get("host") ?? "",
    },
  });
  if (mode === "revalidate") revalidatePath("/action");
  if (mode === "refresh") refresh();
  return { status: res.status };
}
```

Takeaways:

- **Thin server actions** (recommended for the strangler). Client call sites and the `Result` shape stay
  the same. Porting an action means swapping its body to `django('/api/…', { method: 'POST', body })`,
  keeping `revalidatePath(...)`, and getting a single round trip. Next's own action CSRF check (Origin vs
  Host or X-Forwarded-Host [D] `serverActions.md`) protects the browser→Next hop. Forwarding the
  `csrftoken` cookie as `X-CSRFToken`, plus `Origin`, satisfies Django's middleware for the Next→Django
  hop [E]. Django must issue the `csrftoken` cookie (an `ensure_csrf_cookie` endpoint, or on login).
  Behind Caddy, the `x-forwarded-host`/`host` values should come from `PUBLIC_ORIGIN` rather than request
  headers.
- **Client fetch to `/api/*` + `router.refresh()`**. It works, but takes two round trips (mutation, then
  an RSC refresh) [E], and every call site changes. Use it where the client already uses fetch
  (`resume-dropzone.tsx` upload, live panels), and for **auth**: the browser receives Django's
  Set-Cookie natively.
- **Auth through a server action** works [E], but requires re-issuing every Set-Cookie through
  `cookies().set()`. That needs a Set-Cookie parser (the run used the internal, unstable
  `next/dist/compiled/@edge-runtime/cookies` `parseSetCookie`; use a real dependency if ever needed).
  Avoid it.
- `revalidatePath` vs `refresh()` [E]: both re-rendered the current page in the action's response.
  [D] (`revalidatePath.md`) also says `revalidatePath` in a Server Function marks previously visited
  pages to refresh on their next visit, whereas `refresh()` only refreshes the client router.
  `revalidatePath` targets the route *file* path (destination for rewrites), never a Django URL.
- **Data changed by another process** (the Python worker, Django itself): there is no Next server cache
  to invalidate, because every page is dynamic. Link navigation re-rendered with fresh data (5 → 8 after
  two out-of-band hits; [D] `staleTimes.dynamic` default 0 since v15). Browser back/forward reused the
  client cache (8 → 8). Django cannot call `revalidatePath`. Foray's existing `useLive()` poll plus
  `<RefreshOn kinds>` → `router.refresh()` is already the right mechanism. `/api/live` just moves to
  Django. If a `'use cache'` tag were ever introduced, Django would need to call a Next route handler
  that calls `revalidateTag` (webhook pattern in `backend-for-frontend.md`) [D]. Another reason not to.

## 4. Production topology

| | T1: Next standalone public, rewrites to Django | T2: Caddy in front of both (recommended end state) |
|---|---|---|
| SSE | Works only with `no-transform` or `compress:false`, plus `proxyTimeout` [E]. Every stream is held by the Node process | Caddy flushes `text/event-stream` immediately and compresses it as written [D: Caddy `reverse_proxy` and `encode` docs] |
| Uploads | Need `proxyClientMaxBodySize`. The tee keeps an in-memory copy [S] | Straight to Django. Caddy `request_body max_size` gives an early 413 [D] |
| Cookies | Same origin, Set-Cookie verbatim [E] | Same origin. Caddy passes headers through unchanged [D] |
| Django `get_host()` / CSRF | Sees `127.0.0.1:8100`; needs `CSRF_TRUSTED_ORIGINS` [E] | Sees the public Host [D]. Next server-side calls still need `CSRF_TRUSTED_ORIGINS` (they go direct) |
| Client IP / HTTPS | No XFF or X-Forwarded-Proto added; XFF spoofable [E] | Caddy sets XFF and Proto [D]. Django: `SECURE_PROXY_SSL_HEADER`; trust only Caddy |
| Moving parts | 2 (web, api) | 3 (caddy, web, api); Caddy also does TLS |
| Dev parity | `pnpm dev` behaves the same [E] | Dev keeps the Next `fallback` rewrite; Caddy only in compose |
| Strangler | One switch: delete the Next `route.ts` | Caddy routes `/api/*` to Django only once `/api` is fully ported. Until then Caddy routes `/api/*` to Next, and the Next `fallback` rewrite forwards the rest |

Sketch, **not executed** (Caddy is not installed on this machine, and I did not install system packages):

```caddyfile
foray.example.com {
  encode zstd gzip
  @django path /api/* /admin/* /django-static/*
  handle @django {
    request_body { max_size 16MB }
    reverse_proxy api:8000      # streams text/event-stream immediately (Caddy docs)
  }
  handle {
    reverse_proxy web:3000
  }
}
```

During the transition, swap `@django` for an explicit list of fully ported prefixes, or route all of
`/api/*` to `web:3000` and let Next's `fallback` rewrite forward it. That costs the T1 caveats until the
switch.

## Concrete config for Foray (proposed; the same knobs were executed in the scratch app)

```ts
// next.config.ts
const DJANGO_ORIGIN = process.env.DJANGO_ORIGIN ?? "http://127.0.0.1:8000"; // baked at `next build`

const nextConfig: NextConfig = {
  output: "standalone",
  serverExternalPackages: ["@napi-rs/canvas"],
  // Next's gzip buffers proxied SSE unless Django sends `Cache-Control: no-transform`
  // (the required fix). Turn Next's gzip off only once Caddy is in front and compresses;
  // today's compose exposes web:3000 directly, so leave it on until then.
  // Like the rewrites, this is captured at build time: standalone reads it from
  // .next/required-server-files.json (verified: compress False there => no Content-Encoding).
  compress: process.env.BEHIND_REVERSE_PROXY !== "1",
  experimental: {
    proxyTimeout: 10 * 60_000,      // rewrite idle timeout (default 30s)
    proxyClientMaxBodySize: "16mb", // rewrite bodies over this are truncated -> 500
  },
  async rewrites() {
    return {
      beforeFiles: [],
      afterFiles: [],
      // Strangler: any /api path Next no longer serves goes to Django.
      fallback: [{ source: "/api/:path*", destination: `${DJANGO_ORIGIN}/api/:path*` }],
    };
  },
};
```

Django-side requirements surfaced here (for the Django researchers):

- Streamed responses: `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`, plus the AI SDK's
  `x-vercel-ai-ui-message-stream: v1`, and `: ping\n\n` heartbeats every ≤15s.
- `CSRF_TRUSTED_ORIGINS = [PUBLIC_ORIGIN]`; `ALLOWED_HOSTS` includes the internal hostname (`api`,
  `127.0.0.1`).
- No trailing slash on proxied API routes; relative redirects; cookies without `Domain`.
- Behind Caddy: `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`.
- Keep the upload limit (10MB) enforced in Django with a JSON 413, below Next's `proxyClientMaxBodySize`.

Foray route mapping:

| Today (Next) | After |
|---|---|
| `/api/chat` (AI SDK stream) | Django SSE via fallback rewrite; `useChat` unchanged |
| `/api/live`, `/api/runs/*`, `/api/conversations/*`, `/api/matches` | Django via fallback (delete each `route.ts` as it is ported) |
| `/api/uploads` | Django via fallback; needs `proxyClientMaxBodySize` |
| `/api/documents/[id]`, `/api/resumes/[id]/preview` | Django via fallback (`Content-Disposition` and `Cache-Control` pass through) |
| `/api/auth/[...all]` (Better Auth) | Django auth endpoints; sign-in form calls them via fetch, then `router.refresh()` |
| `src/app/actions/*.ts` | thin forwarders via `django()` + `revalidatePath` |
| pages (`requireUser()` + Drizzle queries) | `django()` reads in server components |
| `src/proxy.ts` | stays; cookie name changes to Django's session cookie |

## Sources

- Bundled Next 16.3.5 docs: `node_modules/next/dist/docs/01-app/03-api-reference/05-config/01-next-config-js/rewrites.md`,
  `.../proxyClientMaxBodySize.md`, `.../serverActions.md`, `.../staleTimes.md`,
  `.../03-file-conventions/proxy.md`, `.../04-functions/{fetch,cookies,revalidatePath,refresh}.md`,
  `.../01-directives/{use-cache,use-cache-private}.md`, `01-app/02-guides/{backend-for-frontend,self-hosting,data-security,server-actions}.md`
- Next 16.3.5 source: `next/dist/server/lib/router-utils/proxy-request.js`, `next/dist/server/lib/router-server.js`
  (lines 124–126, 257–264, 417), `next/dist/server/lib/router-utils/resolve-routes.js` (124–125),
  `next/dist/server/body-streams.js` (52–110), `next/dist/server/pipe-readable.js` (74–84, 166–173),
  `next/dist/compiled/compression/index.js`, `next/dist/compiled/httpxy/index.js`
- AI SDK 7.0.106: `ai/dist/index.js:6870` (`UI_MESSAGE_STREAM_HEADERS`)
- Caddy docs (documentation only): https://caddyserver.com/docs/caddyfile/directives/reverse_proxy ,
  https://caddyserver.com/docs/caddyfile/directives/encode
- Misdiagnosis example: https://cubic.dev/pr/lfnovo/open-notebook/pull/770

---

## Verification (adversarial pass, 2026-10-04)

Two claims were marked "documentation only". Both were checked by running them. Workspace:
`research/next-frontend-only-verify/` (`origin/app.py` Django origin, `timeclient.py` chunk-timing client,
`Caddyfile`, `probe.sh`, `web/` Next 16.3.5 app, `web-1638/` the same app on Next 16.3.8, `caddy-src*/`).

| Component | Version | How |
|---|---|---|
| Caddy | 2.11.6 (`caddy:2` official image, digest `sha256:3422ce6d…`). Docker Hub has no 2.11.7 tag yet | run in Docker, ports 8280–8282 and 8443, proxying to the host |
| Caddy source | v2.11.6 and v2.11.7 (released 2026-10-03), from raw.githubusercontent.com | `reverseproxy/streaming.go`, `reverseproxy/reverseproxy.go`, `encode/encode.go` |
| Django / uvicorn | 6.1.1 / 0.54.0, Python 3.14.5 (`verify/venv`) | origin on :8210 |
| Next | 16.3.5 (the repo's pin) and 16.3.8 (npm `latest`), Turbopack, `cacheComponents: true` | `next build` + `next start -p 3210` |

### Claim 1: Caddy SSE flushing, encode and forwarded headers. Verdict: confirmed, with caveats

**[S] Source, v2.11.6.** `streaming.go` `flushInterval()` returns `-1` ("flush immediately") when
`Content-Type` is `text/event-stream`, and also whenever `ContentLength == -1`. `encode.go`
`WriteHeader()` calls `init()` and writes the header straight away for SSE (#6293), so the
`minimum_length` sniffing in `Write()` is skipped. `FlushError()` flushes the gzip/zstd encoder and
then the connection. In v2.11.7 the SSE branch is the same. It adds RFC 10036 `Incremental` support
alongside it.

**[E] Timing.** The origin sends 26-byte events (well under the 512-byte `minimum_length`) once a second.
`timeclient.py` timestamps each read off the socket and decompresses incrementally:

```
== B. Caddy :80 plain reverse_proxy, SSE, identity
  0.01s  wire=  26B  decoded='data: {"i": 0, "t": 0.0}\n\n'
  1.01s  wire=  26B  decoded='data: {"i": 1, "t": 1.0}\n\n'
  2.01s  ...  3.01s  (same as direct origin)
== C. Caddy :81 `encode zstd gzip`, SSE, Accept-Encoding: gzip   -> content-encoding='gzip'
  0.01s  wire=  36B  decoded='data: {"i": 0, ...}'   1.01s ... 2.01s ... 3.01s (one compressed chunk per event)
== D. same, Accept-Encoding: zstd                                 -> content-encoding='zstd'
  0.01s  wire=35B ... 1.01s wire=18B ... 2.01s ... 3.01s
== E. Caddy :82 `encode gzip { minimum_length 4096 }`, SSE        -> gzip, still one chunk per event
```

So Caddy flushes SSE immediately, and `encode` compresses SSE per event without waiting for
`minimum_length` bytes. The claim holds.

**[E] Headers.** `/api/echo` on Django, through Caddy :80:

```
Host: localhost:8280            (the client's Host, passed through)
X-Forwarded-For: 172.17.0.1     X-Forwarded-Host: localhost:8280     X-Forwarded-Proto: http
Via: 1.1 Caddy                  Accept-Encoding: gzip   <- added although curl sent none
```

The client then sent forged `X-Forwarded-For: 6.6.6.6`, `X-Forwarded-Proto: https` and
`X-Forwarded-Host: evil.example`. Caddy replaced all three with its own values, because by default no
`trusted_proxies` are configured (source: `addForwardedHeaders`). Through the TLS site
(`tls internal`, :8443), Caddy sent `X-Forwarded-Proto: https` and `Via: 2.0 Caddy`. The claim holds.

Caveats the original text gets wrong or leaves out:

1. **`no-transform` turns off Caddy's compression.** `isEncodeAllowed()` skips any response whose
   `Cache-Control` contains `no-transform`. [E] Case G: SSE with `Cache-Control: no-cache, no-transform`
   through `encode` came back with `content-encoding=None`. Section 2 tells Django to always send
   `no-transform` (the T1 fix), so under T2 Caddy will not compress those streams. "Compresses it as
   written" is true only for streams that leave `no-transform` out. It still streams, so this costs
   bandwidth, not correctness.
2. **Caddy does not pass headers through unchanged** (section 4 table). It adds `Via` in both directions,
   sets `Accept-Encoding: gzip` upstream when the client sent none (Go's transport, which then
   decompresses transparently), and strips hop-by-hop headers.
3. **"Django's CSRF Origin check works without extra settings" (TL;DR item 4) is false once Caddy
   terminates TLS.** [E] With Caddy on HTTPS, uvicorn run with `--no-proxy-headers`, and no
   `SECURE_PROXY_SSL_HEADER`, Django reported `is_secure: False`. A POST carrying
   `Origin: https://localhost:8443` got `403 Origin checking failed - https://localhost:8443 does not match
   any trusted origins.` With `SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO","https")` the Origin
   check passed, and the request failed further on with "CSRF cookie not set", as expected. The section 4
   table does list `SECURE_PROXY_SSL_HEADER`; the TL;DR should say the same. A related trap: uvicorn
   defaults to `proxy_headers=True` with `FORWARDED_ALLOW_IPS="127.0.0.1,::1"`. In the first run Docker
   Desktop's forwarder connected from 127.0.0.1, so uvicorn itself trusted `X-Forwarded-Proto` and
   `is_secure` was True with no Django setting. In compose, Caddy is not 127.0.0.1, so that would not
   happen. Configure one layer (Django `SECURE_PROXY_SSL_HEADER`, or uvicorn/granian forwarded IPs)
   deliberately.

### Claim 2: `'use cache'` vs `cookies()`/`headers()`, `'use cache: private'`, "server-side caching adds nothing". Verdict: the facts hold, the conclusion does not

**[E] `cookies()`/`headers()` inside `'use cache'`** (Next 16.3.5, `cacheComponents: true`):

- `app/uc-dynamic` awaits `connection()` inside `<Suspense>` and then calls a `'use cache'` function that
  awaits `cookies()`. `next build` **succeeded** (route `◐ /uc-dynamic`). Under `next start` the request
  returned **HTTP 200** and the server logged:
  `⨯ Error: Route /uc-dynamic used \`cookies()\` inside "use cache". Accessing Dynamic data sources inside a
  cache scope is not supported. ... https://nextjs.org/docs/messages/next-request-in-use-cache`
  (digest `…@E831`). The same happened with `headers()` (`/uc-dynamic-headers`, `E833`).
- `app/uc-direct` calls the same function with no request-time API first. `next build` **failed**:
  `Error occurred prerendering page "/uc-direct" … Next.js build worker exited with code: 1`.
- Next 16.3.8 behaves the same (build passes; at runtime the same error is logged and the response is 200).

So the error code is right, and so is "can pass build and fail at runtime" for dynamic routes. Note that
the failure arrives as a 200 with the error streamed into the Suspense boundary, so status-code monitoring
will not catch it.

**[E] `'use cache: private'` does not persist across requests.** `app/uc-private` renders two components
that both call a private cached `getMe()`. `getMe()` reads `cookies()`, fetches Django `/api/me` (which
counts hits per `sessionid`) and adds a random token:

```
alice: A hits=1 rnd=6a84he  B hits=1 rnd=6a84he     <- deduped within one request
alice: A hits=2 rnd=516dbx  B hits=2 rnd=516dbx     <- recomputed on the next request
alice: A hits=3 ...                                    (16.3.8: same pattern)
```

This holds.

**[E] Refuted: "with per-user data behind a session cookie, the server-side cache gains nothing."**
The bundled `use-cache.md` itself (line 21) says to read cookies outside the cached scope and pass values
in as arguments, and lines 249–252 say self-hosted in-memory entries persist across requests. `app/uc-arg`
does that: it reads `sessionid` outside, then calls
`getMeFor(sid)` with `'use cache'`, `cacheLife({stale:30,revalidate:60,expire:300})` and `cacheTag(`user:${sid}`)`:

```
alice: hits=5 rnd=zsnx6g   alice: hits=5 rnd=zsnx6g        <- one Django call, then served from Next's cache
POST /api/revalidate?tag=user:alice  -> {"revalidated":"user:alice"}   (route handler: revalidateTag(tag, {expire: 0}))
alice: hits=6 rnd=cvevhj   alice: hits=6 rnd=cvevhj        <- refetched once after invalidation
bob:   hits=3 rnd=flhf68   (revalidate alice again)  bob: hits=3 rnd=flhf68   <- bob's entry untouched
```

16.3.8 gave the same result (`carol hits=3, 3`; `dave hits=1, 1`). So under `next start`/standalone,
per-user server-side caching of Django data works, is isolated per argument, and can be invalidated per
user from Django through a tag webhook.

Recommending against it is still defensible, but for different reasons than "gains nothing":

- data is stale up to `revalidate` unless Django calls the webhook on every write, including worker writes;
- the cache is in-process memory, so it is not shared between instances and is lost on restart or deploy;
- it competes with the `useLive()` → `router.refresh()` freshness model;
- a session id (or user id) becomes a cache key;
- adopting it means turning on `cacheComponents` app-wide.

Rewrite the recommendation as a trade-off, not as "no benefit".

### Verdicts

| Claim | Refuted? | Basis |
|---|---|---|
| Caddy flushes SSE immediately; `encode` streams SSE without waiting for `minimum_length`; passes Host and sets X-Forwarded-For/Proto/Host | **No** (confirmed) | [E] Caddy 2.11.6 timing and echo; [S] v2.11.6 and v2.11.7 source. Caveats: `no-transform` disables compression; Via and Accept-Encoding are added; behind TLS, the CSRF Origin check needs `SECURE_PROXY_SSL_HEADER` |
| `'use cache'` cannot call cookies()/headers() (next-request-in-use-cache, can pass build and fail at runtime); `'use cache: private'` does not persist across requests; **so server-side caching adds nothing for per-user Django data** | **Yes** (partly: the facts hold, the conclusion is false) | [E] Next 16.3.5 and 16.3.8 builds and `next start` |

Sources: Caddy source at tags v2.11.6/v2.11.7 (`modules/caddyhttp/reverseproxy/streaming.go` `flushInterval`,
`reverseproxy.go` `addForwardedHeaders` and Via, `modules/caddyhttp/encode/encode.go` `WriteHeader`/`Write`/`FlushError`/
`isEncodeAllowed`); bundled Next 16.3.5 docs `01-app/03-api-reference/01-directives/use-cache.md` (lines 21, 241,
249–252) and `use-cache-private.md` (lines 14–16); uvicorn 0.54.0 `uvicorn/config.py` (`proxy_headers=True`,
`FORWARDED_ALLOW_IPS` default `127.0.0.1,::1`).
