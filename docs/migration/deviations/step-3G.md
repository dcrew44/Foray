# Step 3G: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. The client/server ESLint boundary is `no-restricted-syntax`, not `no-restricted-imports`

- **What.** `eslint.config.mjs` applies one `no-restricted-syntax` selector to `src/**`: a module
  whose prologue holds `"use client"` must not import, re-export or dynamically import a path
  ending in `lib/api/server` (`@/lib/api/server`, `../../lib/api/server`, `…/server.ts`). Type-only
  imports are refused too; shared types belong in `lib/api/types` (step 8A) or the schema.
- **Why.** §1.5 names `no-restricted-imports`, but that rule is configured per file glob and
  cannot see a directive, and "use client" files are spread across `src/app` and
  `src/components`. The selector checks the directive in the file's own syntax tree, so it is
  exact. No other config block sets `no-restricted-syntax` (`eslint --print-config`), so nothing
  overrides it. `server.ts` also does `import "server-only"`, so `next build` refuses the same
  mistake.
- **Evidence.** Seven scratch files (since deleted): `"use client"` + value import, `'use client'`
  (no semicolon) + relative type import, `export { … } from "…/server.ts"` + `import()`, and
  `export *` were refused (5 errors); a server module importing it, a client module importing
  `lib/api/client`, and a module holding the string `"use client"` below an import were clean.
- **Also.** `backend/**` is a global ignore (otherwise ESLint walks `backend/.venv`). The old
  Playwright and worker boundaries stay until step 11 deletes the code they guard (§1.5: they go
  "with the code").

## 2. `call()`: a body that is not Django's envelope becomes the generic failure

- **What.** `call()` returns `data` when there is one, else `error` **only if** it is an object
  with a boolean `ok`, else `{ok:false, message:"That request did not go through. Try again."}`.
  A thrown fetch still gives "Could not reach Foray. Check that it is running.". The failure
  objects are cast through `unknown`.
- **Why.** openapi-fetch parses an error body as JSON and falls back to the raw text. While
  Django is down, Next's rewrite answers a bare `500 Internal Server Error` (text/plain,
  `research/next-frontend-only.md` §2d), so §6.2's `data ?? error ?? …` returned that string as
  `T`: `r.ok` was `undefined` and `toast.error(r.message)` showed nothing. The cast: §6.2's
  `{ ok: false, … } as T` in the `catch` does not compile under `strict` (TS2352: `T` could be a
  narrower subtype of `{ok: boolean}`).
- **Evidence.** A `tsx` run of `call()` over a mocked fetch (no server): 200/400/409 envelopes
  returned as sent; a 500 `Internal Server Error` and a 502 with an empty body gave the generic
  message; a thrown fetch gave the "Could not reach" message; a 401 from `/api/matches/x` on
  `/matches?view=board` assigned `/sign-in?next=%2Fmatches%3Fview%3Dboard`; a 401 from
  `/api/auth/sign-in`, and a 401 while on `/sign-in`, did not navigate.
- **Also.** The 401 navigation carries an
  `eslint-disable-next-line @next/next/no-location-assign-relative-destination`: it is a full
  page load on purpose (the session is gone) and `call()` runs outside React, with no router.

## 3. `Me` is written by hand in `server.ts`

- **What.** `export type Me = { id: string; name: string; email: string; signupOpen: boolean }`
  (§4.4's `GET /api/me`), next to `getMe`/`getMeOrNull`.
- **Why.** §6.1 names `Me` but the accounts router is still a stub, so the generated schema has
  no `/api/me` yet, and `types.ts` waits for 8A. Step 8A can replace it with the generated
  response type.
- **Evidence.** A scratch type check (deleted) compiled `getMe(): Promise<Me>`,
  `getMeOrNull(): Promise<Me | null>` (assigning it to `Me` is an error), `call(api.GET("/api/health"))`
  as `{ok: boolean}`, and refused `api.GET("/api/nope")`.

## 4. `proxy.ts`: `next=` keeps today's path-only value

- **What.** The signed-out redirect still sets `next` to the pathname, as today; the forwarded
  `x-foray-path` header is pathname plus query, as §6.5 says. The header is overwritten on every
  matched request, so a client-sent `x-foray-path` never reaches `django()`.
- **Why.** §6.5 lists exactly three changes (cookie name, the header, the two dead matchers).
- **Evidence.** `proxy()` called directly with hand-built `NextRequest`s (no server): no cookie
  and a `better-auth.session_token`-only cookie → 307 to `/sign-in?next=%2Fmatches` /
  `%2Fhome`; `sessionid` → `x-middleware-request-x-foray-path: /matches?view=board&stage=applied`
  (and `/home` when the request carried a forged `x-foray-path: //evil.example`).
- **Deferred.** The Done check (a scratch server component logs the header under `next dev`) and
  the §15.4 assumption that the header reaches server components need `:3000`, so they run at
  integration.

## 5. Dependencies and generated files

- `openapi-fetch ^0.17.0` (dependencies) and `openapi-typescript ^7.13.0` (devDependencies);
  `pnpm-lock.yaml` changes with them (new packages, plus `debug` re-resolved with the
  `supports-color` peer `openapi-typescript` brings).
- `server-only` is not installed: Next aliases it in its compilers
  (`next/dist/build/create-compiler-aliases.js`), the Next docs call installing it optional, and
  `tsc` does not resolve side-effect imports.
- `backend/openapi.json` (written by ninja, no trailing newline) and `src/lib/api/schema.d.ts`
  are byte-identical across `pnpm api:types` re-runs. They hold only `/api/health` and
  `HealthOut`: every other router is still a stub.
