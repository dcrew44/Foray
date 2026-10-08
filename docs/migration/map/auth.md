# Area map: auth (Better Auth 1.7.5 on Next 16) for the Django port

Repo root: `/Users/haydenbarolette/Projects/JobSearchAgent/.claude/worktrees/middleware-django-migration-389b1a` (all repo paths below are relative to it).

Abbreviations for library paths (all under `node_modules/`, pnpm store):

| Short | Real path |
|---|---|
| `BA/` | `better-auth/dist/` (better-auth 1.7.5) |
| `CORE/` | `.pnpm/@better-auth+core@1.7.5_@better-auth+utils@0.4.2_@better-fetch+fetch@1.3.2_better-call@_d0861dc1c5e560036ddf567d6e572604/node_modules/@better-auth/core/dist/` |
| `UTILS/` | `.pnpm/@better-auth+utils@0.4.2/node_modules/@better-auth/utils/dist/` |
| `BC/` | `.pnpm/better-call@1.4.0_zod@4.6.5/node_modules/better-call/dist/` |
| `DRZ/` | `.pnpm/@better-auth+drizzle-adapter@1.7.5_*/node_modules/@better-auth/drizzle-adapter/dist/index.mjs` |

Everything marked **VERIFIED** was executed: a Node script drove the real better-auth 1.7.5 handler (in-memory adapter, same config as the app) and a Python 3.14 script re-derived hashes/signatures. Scripts: `scratchpad/authcheck/{gen.mjs,verify.py,flow.mjs,flow2.mjs}`.

---

## 0. TL;DR for the Django engineer

1. **Password hash** (`account.password`, `provider_id='credential'`): `"<salt>:<key>"`, where `salt` = 32 lowercase hex chars (16 random bytes hex-encoded) and `key` = 128 lowercase hex chars = `scrypt(N=16384, r=16, p=1, dkLen=64)` of `NFKC(password)` UTF-8, **salted with the ASCII bytes of the 32-char hex salt string itself (not the 16 decoded bytes)**. Python: `hashlib.scrypt(unicodedata.normalize("NFKC", pw).encode(), salt=salt_hex.encode(), n=16384, r=16, p=1, dklen=64, maxmem=64*1024*1024)`. `maxmem` is mandatory: the OpenSSL default of 32 MiB raises "memory limit exceeded". VERIFIED.
2. **Session cookie**: name `better-auth.session_token` (`__Secure-better-auth.session_token` plus `Secure` when `BETTER_AUTH_URL` starts with `https://`). Value on the wire = `encodeURIComponent(token + "." + base64std(HMAC_SHA256(key=utf8(BETTER_AUTH_SECRET), msg=utf8(token))))`. The signature is 44 chars and ends in `=`. Attributes: `Max-Age=604800; Path=/; HttpOnly; SameSite=Lax`. VERIFIED.
3. **Session row lookup**: `SELECT ... FROM session JOIN "user" ON user.id=session.user_id WHERE session.token = <token>`. Valid if `expires_at >= now (UTC)`. Expiry is 7 days, refreshed when a day has passed since the last refresh.
4. **IDs**: `user.id`, `session.id`, `account.id`, `verification.id` and `session.token` are 32-char random strings over `[a-zA-Z0-9]`. Treat `user.id` as opaque `text`: test users exist with ids like `test-follow-up-<uuid>`, and dev-session rows have 32-hex ids.
5. **Secret**: the HMAC key is the literal UTF-8 bytes of the `BETTER_AUTH_SECRET` string. It is not base64-decoded, even though `.env.example` says to generate it with `openssl rand -base64 32`.
6. **Live DB facts** (checked read-only, no secrets printed, 2026-10-04): 1 user (32-char alnum id, lowercase email), 1 account (`credential`, `account_id = user_id`, password matches `^[0-9a-f]{32}:[0-9a-f]{128}$`), 16 session rows (15 with 32-alnum tokens; 5 from the dev-session script with 32-hex ids), 0 verification rows, DB `timezone = Etc/UTC`, 0 currently-live sessions. Google is **not** configured in the main `.env` (`GOOGLE_CLIENT_ID=` and `GOOGLE_CLIENT_SECRET=` are empty), so there are no Google accounts.

---

## 1. Files in this area (all read in full)

| File | Lines | Role |
|---|---|---|
| `src/lib/auth/index.ts` | 1-47 | `betterAuth({...})` server config (`auth` singleton) and the `Session` type |
| `src/lib/auth/session.ts` | 1-25 | `getSession()` and `requireUser()`, used by every page, action and API route |
| `src/lib/auth/client.ts` | 1-11 | browser client `createAuthClient()`, which exports `signIn`, `signUp`, `signOut` and `useSession` |
| `src/app/api/auth/[...all]/route.ts` | 1-5 | `export const { GET, POST } = toNextJsHandler(auth)`: every `/api/auth/*` endpoint |
| `src/lib/db/schema/auth.ts` | 1-95 | Drizzle tables `user`, `session`, `account`, `verification` |
| `src/proxy.ts` | 1-33 | Next 16 "proxy" (middleware): an optimistic cookie-presence redirect |
| `src/components/auth/sign-in-form.tsx` | 1-152 | sign-in / sign-up UI and the Google button |
| `src/components/settings/sign-out-button.tsx` | 1-36 | sign-out button (Settings) |
| `src/components/shell/user-menu.tsx` | 81-90 | second sign-out entry point (user menu) |
| `src/app/(auth)/sign-in/page.tsx` | 1-43 | sign-in page: `safeNext`, redirect if signed in, `googleEnabled` flag |
| `src/app/page.tsx` | 1-8 | `/` goes to `/home` or `/sign-in` |
| `src/app/(app)/layout.tsx` | 1-20 | `requireUser()` and passes `{name,email}` to the shell |
| `scripts/set-password.ts` | 1-91 | `pnpm auth:set-password <email>` |
| `scripts/dev-session.ts` | 1-57 | mints a dev session cookie |
| `src/env.ts` | 33-36 | auth env vars |
| `drizzle/0000_calm_bloodscream.sql` | 18-64, 256-280 | DDL for the auth tables, FKs and indexes |
| `drizzle/0001_wealthy_human_cannonball.sql` | 1 | `DROP INDEX "account_provider_account_idx"` |
| `package.json` | scripts `auth:generate`, `auth:set-password` | `better-auth generate --config src/lib/auth/index.ts` (dev CLI `@better-auth/cli ^1.4.21`) |

---

## 2. Configuration (`src/lib/auth/index.ts`)

```ts
betterAuth({
  baseURL: env.BETTER_AUTH_URL,          // default "http://localhost:3000" (src/env.ts:34)
  secret: env.BETTER_AUTH_SECRET,        // optional, min 32 chars (src/env.ts:33)
  database: drizzleAdapter(db, { provider: "pg", schema: { user, session, account, verification } }),
  emailAndPassword: { enabled: true, requireEmailVerification: false },
  socialProviders,                       // { google: { clientId, clientSecret } } only if both env vars set (:15-23)
  plugins: [nextCookies()],
})
```

Why-comments to keep:
- `index.ts:9-14`: Google is configured only when credentials exist, "so local development never blocks on a Google Cloud project". It is wired now because **Phase 3 Gmail scopes are added to this same account via `linkSocial`**, not through a second provider.
- `index.ts:35-38`: email+password exists "so a single developer can sign in without any third-party setup". There is no mail transport in Phase 1, so email verification is off.
- `index.ts:43`: `nextCookies()` exists so server actions and route handlers can write the session cookie.

### Effective defaults (from the library, given this config)

| Setting | Value | Source |
|---|---|---|
| basePath | `/api/auth` | `BA/context/create-context.mjs:87` |
| baseURL used for callbacks | `withPath(BETTER_AUTH_URL, "/api/auth")`, e.g. `http://localhost:3000/api/auth` | `BA/utils/url.mjs` `withPath`/`getBaseURL` |
| secret | `options.secret || env.BETTER_AUTH_SECRET || env.AUTH_SECRET || "better-auth-secret-12345678901234567890"` (the default throws in production) | `create-context.mjs:70-82`, `:39-46`; `BA/utils/constants.mjs:2` |
| `BETTER_AUTH_SECRETS` (versioned, `"v:secret,..."`) | not set; if set, the first entry's secret becomes the signing secret | `create-context.mjs:70-77`, `BA/context/secret-utils.mjs:13-51` |
| session.expiresIn | 604800 s (7 d) | `create-context.mjs:148` |
| session.updateAge | 86400 s (1 d) | `create-context.mjs:147` |
| session.freshAge | 86400 s (only used by unused endpoints) | `create-context.mjs:149` |
| cookieCache | **disabled** (stateful because a DB exists) | `create-context.mjs:48-54`, `BA/context/store-capabilities.mjs:2-4` |
| password length | min 8, max 128 (JS string `.length`, i.e. UTF-16 code units) | `create-context.mjs:185-188` |
| password hash/verify | scrypt (section 4) | `create-context.mjs:183-184` |
| rate limit | enabled only if `NODE_ENV==="production"`; window 10 s, max 100, memory store | `create-context.mjs:170-176` |
| OAuth state storage | `"database"` (verification table plus signed `state` cookie) | `create-context.mjs:137` |
| skipOriginCheck | `true` only when `NODE_ENV==="test"` or env `TEST` is truthy | `create-context.mjs:211`, `CORE/env/env-impl.mjs:32-36` |
| trustedOrigins | `[origin(BETTER_AUTH_URL)]` + `BETTER_AUTH_TRUSTED_ORIGINS.split(",")` | `BA/context/helpers.mjs:60-86` |
| id generation | `generateId()` = 32 chars over `a-zA-Z0-9` | `CORE/db/adapter/get-id-field.mjs:23-31`, `CORE/utils/id.mjs:3-5` |
| DB transactions | **off** (drizzle adapter `transaction: config.transaction ?? false`), so sign-up's `runWithTransaction` is not atomic | `DRZ:557` |
| encryptOAuthTokens | off, so OAuth tokens are stored in plaintext | `BA/oauth2/utils.mjs:21-27` |
| telemetry | off (only on if `BETTER_AUTH_TELEMETRY` is set) | `@better-auth/telemetry/dist/index.mjs:360-361` |
| NODE_ENV flags | `isProduction = NODE_ENV==="production"`, `isDevelopment = "dev"|"development"`, `isTest = "test"|TEST` | `CORE/env/env-impl.mjs:32-36` |

### Env vars (`src/env.ts`)

| Var | Schema | Notes |
|---|---|---|
| `BETTER_AUTH_SECRET` | `z.string().min(32).optional()` (`:33`) | HMAC key for cookie signatures. Set in the main `.env` |
| `BETTER_AUTH_URL` | `z.url().default("http://localhost:3000")` (`:34`) | Base origin. Decides the `__Secure-` prefix, trusted origin, and OAuth redirect URI |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | optional strings (`:35-36`) | Google is enabled only if both are set; empty in the main `.env` |
| `NODE_ENV` | enum, default `development` (`:27-29`) | dev-session refuses `production`; also drives the rate-limit, dev IP fallback and default-secret checks |
| `emptyStringAsUndefined: true` (`:95`) | | `FOO=` means unset |
| (library-read) `BETTER_AUTH_TRUSTED_ORIGINS`, `BETTER_AUTH_SECRETS`, `AUTH_SECRET`, `BETTER_AUTH_TELEMETRY` | | read straight from `process.env` by better-auth; none are set |

---

## 3. Database tables (`src/lib/db/schema/auth.ts`, DDL in `drizzle/0000_calm_bloodscream.sql:18-64`)

Why-comment (`auth.ts:9-21`): Better Auth owns this shape and `pnpm auth:generate` regenerates it. "Nothing here is ours to redesign — our own columns go on `profiles`, not on `user`." Index names are snake_case, and `defaultNow()` on `session`/`account.updated_at` is deliberate ("a default there costs nothing and turns a missed write into a correct row rather than a not-null violation").

All timestamps are **`timestamp` WITHOUT time zone**. Drizzle writes `Date.toISOString()` (`drizzle-orm/pg-core/columns/timestamp.js:34-35`), and Postgres silently drops the `Z` for this type, so values are **UTC wall-clock**. Reads append `+0000` (`timestamp.js:30-31`). `DEFAULT now()` is also UTC because the DB timezone is `Etc/UTC`.

### `"user"` (quoted, reserved word) — `auth.ts:23-34`, SQL `:46-55`
| Column | Type | Constraints |
|---|---|---|
| `id` | text | PK. 32-char `[a-zA-Z0-9]` when created by Better Auth; treat as opaque |
| `name` | text | NOT NULL (can be `""`) |
| `email` | text | NOT NULL, `UNIQUE` (`user_email_unique`). **Always stored lowercased** (`BA/db/internal-adapter.mjs:140-146`) |
| `email_verified` | boolean | NOT NULL DEFAULT false. Always false for email/password users (no verification flow) |
| `image` | text | nullable |
| `created_at` | timestamp | NOT NULL DEFAULT now() |
| `updated_at` | timestamp | NOT NULL DEFAULT now(). Drizzle `$onUpdate`; Better Auth sets it on every update (`CORE/db/get-tables.mjs` `onUpdate`) |

### `session` — `auth.ts:36-54`, SQL `:34-44`
| Column | Type | Constraints / semantics |
|---|---|---|
| `id` | text | PK, 32 alnum (dev-session rows use 32 hex) |
| `expires_at` | timestamp | NOT NULL. now+7d at creation and at each refresh; now+1d if `rememberMe:false` |
| `token` | text | NOT NULL `UNIQUE` (`session_token_unique`). 32 alnum (`generateId(32)`, `internal-adapter.mjs:273`); the dev-session script writes base64url(24 bytes) |
| `created_at` | timestamp | NOT NULL DEFAULT now() |
| `updated_at` | timestamp | NOT NULL DEFAULT now(); set to now on refresh |
| `ip_address` | text | nullable. Better Auth writes `getIP(headers) || ""` (`internal-adapter.mjs:263`); dev returns `"127.0.0.1"` when there is no XFF (`CORE/utils/ip.mjs:217`); dev-session leaves it NULL |
| `user_agent` | text | nullable. `headers.get("user-agent") || ""` (`:264`); dev-session writes `"dev-session script"` |
| `user_id` | text | NOT NULL FK `"user"(id)` ON DELETE CASCADE (`session_user_id_user_id_fk`, SQL `:257`) |
| index | | `session_user_id_idx` on `user_id` (SQL `:279`) |

### `account` — `auth.ts:56-79`, SQL `:18-32`
| Column | Type | Semantics |
|---|---|---|
| `id` | text | PK, 32 alnum |
| `account_id` | text | NOT NULL. For `credential`: **equals `user_id`** (`BA/api/routes/sign-up.mjs:242-247`). For google: the OIDC `sub` |
| `provider_id` | text | NOT NULL. `"credential"` or `"google"` |
| `user_id` | text | NOT NULL FK `"user"(id)` ON DELETE CASCADE (SQL `:256`) |
| `access_token`, `refresh_token`, `id_token` | text | nullable, **plaintext** |
| `access_token_expires_at`, `refresh_token_expires_at` | timestamp | nullable |
| `scope` | text | nullable, comma-joined scopes (accumulated grant, `CORE/db/schema/account.mjs`) |
| `password` | text | nullable. Only on `credential` rows: `"<32hex salt>:<128hex key>"` |
| `created_at`, `updated_at` | timestamp | NOT NULL DEFAULT now() |
| index | | `account_user_id_idx` on `user_id` (SQL `:277`). The unique `(provider_id, account_id)` index from 0000 (SQL `:278`) was **dropped** in `drizzle/0001_wealthy_human_cannonball.sql:1`; uniqueness is enforced only in app code (`internal-adapter.mjs:542-556`, `:671-685` throw on more than one match) |

### `verification` — `auth.ts:81-95`, SQL `:57-64`
| Column | Type | Semantics |
|---|---|---|
| `id` | text | PK, 32 alnum |
| `identifier` | text | NOT NULL, indexed (`verification_identifier_idx`). Stored **plain** (no `storeIdentifier` hashing configured, `BA/db/verification-token-storage.mjs:8-23`) |
| `value` | text | NOT NULL (JSON for OAuth state) |
| `expires_at` | timestamp | NOT NULL |
| `created_at`, `updated_at` | timestamp | NOT NULL DEFAULT now() |

Used only for OAuth state today (Google, not configured). Every `findVerificationValue` call also runs `DELETE FROM verification WHERE expires_at < now()` (`internal-adapter.mjs:753-757`).

### Cross-area FK contract
Every per-user table references `"user"(id)` as text: ON DELETE CASCADE for `job_matches, documents, profiles, resumes, agent_events, approvals, runs, conversations, messages`; ON DELETE SET NULL for `companies.added_by_user_id, llm_calls.user_id` (SQL `:256-276`). Other code reads `user.name` directly (`src/lib/chat/follow-up.ts:122-126`). Tests insert `user` rows with arbitrary ids (`src/test/unit/follow-up-db.test.ts:22-28`).

---

## 4. Password hashing (exact)

Source: `UTILS/password.node.mjs:1-44` (chosen through the `"node"` export condition, `UTILS/../package.json` `"./password"`). `BA/crypto/password.mjs:9-12` wraps it, and `create-context.mjs:183-184` installs it as `ctx.password.hash/verify`. The pure-JS fallback `UTILS/password.mjs:4-31` is byte-identical in behaviour (noble scrypt, same params).

```js
config = { N: 16384, r: 16, p: 1, dkLen: 64 }
generateKey(password, salt) = scrypt(password.normalize("NFKC"), salt /* JS string */, 64,
                                     { N, r, p, maxmem: 128*N*r*2 /* 64 MiB */ })
hashPassword(pw)  -> salt = randomBytes(16).toString("hex")       // 32 lowercase hex chars
                  -> `${salt}:${generateKey(pw, salt).toString("hex")}`  // 128 lowercase hex
verifyPassword(hash, pw):
  [salt, key] = hash.split(":")  ; if !salt || !key -> throw Error("Invalid password hash")
  return generateKey(pw, salt).toString("hex") === key           // plain ===, not constant-time
```

- Node `scrypt` with a **string** salt uses the UTF-8 bytes of that string. The salt input is the 32 ASCII hex characters, **not** the 16 decoded bytes.
- The password is normalised to **NFKC** before UTF-8 encoding. VERIFIED: skipping NFKC gives a different key for a non-ASCII password.
- Format: `^[0-9a-f]{32}:[0-9a-f]{128}$` (matches the live DB row).

**Python reference (VERIFIED against a hash minted by the real library):**
```python
import hashlib, hmac, unicodedata, secrets

def ba_verify(stored: str, password: str) -> bool:
    salt, key = stored.split(":")          # raise/False if malformed
    dk = hashlib.scrypt(unicodedata.normalize("NFKC", password).encode("utf-8"),
                        salt=salt.encode("utf-8"), n=16384, r=16, p=1, dklen=64,
                        maxmem=64 * 1024 * 1024)   # REQUIRED; default fails
    return hmac.compare_digest(dk.hex(), key)

def ba_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.scrypt(unicodedata.normalize("NFKC", password).encode("utf-8"),
                        salt=salt.encode("utf-8"), n=16384, r=16, p=1, dklen=64,
                        maxmem=64 * 1024 * 1024)
    return f"{salt}:{dk.hex()}"
```
A Django `PasswordHasher` can wrap this (e.g. `algorithm = "better_auth_scrypt"`, encoded `better_auth_scrypt$<salt>$<key>`). The stored value must stay in `account.password` in the bare `salt:key` form for as long as any Better Auth code (or a rollback to it) may read it.

**Timing-equalisation behaviour to keep:** when sign-in finds no user, no credential account, or a null password, it still runs `hash(password)` before returning 401 (`BA/api/routes/sign-in.mjs:317-328`). `checkPassword` does the same (`BA/utils/password.mjs:16-18`).

---

## 5. Session cookie and signing (exact)

### Names and attributes — `BA/cookies/index.mjs:20-75`
- Prefix: `__Secure-` if `advanced.useSecureCookies` is set, else if `BETTER_AUTH_URL` starts with `https://`, else if `isProduction`… With a string baseURL, **only the URL scheme decides** (`:23`). Dev (`http://localhost:3000`) has no prefix.
- `createCookie(name)` produces the name `${secure?"__Secure-":""}better-auth.${name}` and the attributes `{ secure: !!prefix, sameSite: "lax", path: "/", httpOnly: true, ...overrides }` (`:27-44`).
- Cookies:
  | Logical | Name (dev) | Max-Age |
  |---|---|---|
  | sessionToken | `better-auth.session_token` | `expiresIn` = 604800 (`:49`) |
  | sessionData (cookie cache; unused, but always expired on sign-out) | `better-auth.session_data` | 300 |
  | dontRememberToken | `better-auth.dont_remember` | none (session cookie) |
  | accountData | `better-auth.account_data` (unused: `storeAccountCookie` is false) | 300 |
  | OAuth state (Google) | `better-auth.state` | 300 (`BA/state.mjs:72`) |

### Signing — `BC/crypto.mjs:21-31`, `BC/cookies.mjs:42-79`
```
sig   = base64_standard( HMAC_SHA256(key = utf8(secret), msg = utf8(value)) )   // 44 chars, '=' padded
value = encodeURIComponent(`${value}.${sig}`)                                    // '+'->%2B '/'->%2F '='->%3D
Set-Cookie: <name>=<value>; Max-Age=<int>; [Domain=..;] Path=/; [Expires=..;] HttpOnly; [Secure;] SameSite=Lax
```
The attribute order is fixed by `_serialize` (`BC/cookies.mjs:42-71`). `Max-Age` above 34560000 (400 days) throws.

### Verification (`ctx.getSignedCookie`) — `BC/context.mjs:38-49`
1. Parse the `Cookie` header (`BC/cookies.mjs:19-41`): split on `;`, trim, **first occurrence of a name wins**, strip surrounding `"`, and `decodeURIComponent` if the value contains `%` (bad encoding leaves it raw, `BC/utils.mjs:58-60`).
2. `i = value.lastIndexOf(".")`; if `i < 1` return null.
3. `signedValue = value[:i]`, `signature = value[i+1:]`; if `len(signature) != 44` or it does not end with `"="`, return null.
4. Verify HMAC-SHA256 via WebCrypto `verify` (constant-time); on mismatch return `false`. Callers treat null and false the same (no session).

The script `scripts/dev-session.ts:43-45` re-implements this: `createHmac("sha256", secret).update(token).digest("base64")` → `encodeURIComponent(`${token}.${sig}`)`.

Python (VERIFIED):
```python
import base64, hashlib, hmac
from urllib.parse import unquote
def sign(token, secret): return token + "." + base64.b64encode(hmac.new(secret.encode(), token.encode(), hashlib.sha256).digest()).decode()
def unsign(raw_cookie_value, secret):
    v = unquote(raw_cookie_value) if "%" in raw_cookie_value else raw_cookie_value
    i = v.rfind(".")
    if i < 1: return None
    tok, sig = v[:i], v[i+1:]
    if len(sig) != 44 or not sig.endswith("="): return None
    return tok if hmac.compare_digest(sig, sign(tok, secret).rsplit(".",1)[1]) else None
# Set-Cookie value: urllib.parse.quote(sign(token, secret), safe="")  (matches encodeURIComponent for this alphabet)
```

### Presence check used by the Next proxy — `BA/cookies/index.mjs:261-270`
`getSessionCookie(request)` returns the raw value of the first of `__Secure-better-auth.session_token`, `better-auth.session_token`, `__Secure-better-auth-session_token`, `better-auth-session_token`. It does **not** check the signature. Its parser (`BA/cookies/cookie-utils.mjs:157-168`) drops names and values that fail RFC regexes.

---

## 6. Session lifecycle

### Create — `BA/db/internal-adapter.mjs:247-320`, `BA/cookies/index.mjs:167-180`
`createSession(userId, dontRememberMe)` runs
`INSERT session (id=gen32, token=gen32, user_id, expires_at = now + (dontRememberMe ? 1d : 7d), ip_address = getIP||"", user_agent = UA||"", created_at=now, updated_at=now)`.
`setSessionCookie` then:
- sets the signed `session_token` (Max-Age 604800, or no Max-Age when `dontRememberMe`);
- if `dontRememberMe`, also sets the signed `dont_remember` cookie to `"true"` with no Max-Age (VERIFIED: `better-auth.dont_remember=true.<sig>; Path=/; HttpOnly; SameSite=Lax`);
- skips the cookie cache (disabled).

`dontRememberMe` is also inherited from an existing valid `dont_remember` cookie when no explicit value is passed (`:168-169`). The UI never sends `rememberMe`, so every UI session is "remembered".

### Read / validate / refresh — `GET /api/auth/get-session`, `BA/api/routes/session.mjs:12-234`
Headers on every response: `cache-control: no-store`, `pragma: no-cache` (`:33-34`). POST returns 405 `METHOD_NOT_ALLOWED_DEFER_SESSION_REQUIRED` (`:37`), because `deferSessionRefresh` is off.

1. `token = getSignedCookie(session_token)`. Any stale `session_data` cookie is cleaned (expired) when the cookie cache is off (`:42-46`). No or invalid token returns `200 null` without clearing the cookie (VERIFIED with a tampered signature).
2. `findSession(token)` runs `SELECT session.*, user.* FROM session JOIN "user" ON ... WHERE token=?` (`internal-adapter.mjs:343-357`). A session whose user is missing counts as none.
3. If not found **or `expires_at < now`**: expire the `session_token`, `session_data` and `dont_remember` cookies (`Max-Age=0`, same attributes) (`BA/cookies/index.mjs:241-255`). If a row existed, `DELETE FROM session WHERE token=?`. Return `200 null` (`:155-165`). VERIFIED.
4. If the `dont_remember` cookie is valid, or the query has `disableRefresh`, return `{session, user}` without refreshing (`:170-177`).
5. `shouldBeUpdated = expires_at - expiresIn + updateAge <= now`, i.e. `expires_at - 6 days <= now`, i.e. at least 1 day since the last issue or refresh (`:178-180`). Refresh is also skipped when `getShouldSkipSessionRefresh()` is true: set by `nextCookies` for RSC requests (`RSC: 1` without `next-action`, `BA/integrations/next-js.mjs:67-69`), because cookies cannot be written there.
6. On refresh: `UPDATE session SET expires_at = now+7d, updated_at = now WHERE token=?`. If no row came back (a concurrent delete), expire the cookies and return **401** `FAILED_TO_GET_SESSION`. Otherwise re-set the same signed cookie with `Max-Age=604800` and return the updated session (`:198-221`). VERIFIED: a row issued 25 h ago was pushed back to 7 d and the cookie re-sent.
7. Any non-APIError exception returns 500 `{"message":"Failed to get session","code":"FAILED_TO_GET_SESSION"}` (`:229-233`).

**Response JSON** (VERIFIED, dates as ISO-8601 UTC with ms and `Z`):
```json
{"session":{"expiresAt":"...Z","token":"<32>","createdAt":"...Z","updatedAt":"...Z","ipAddress":"127.0.0.1","userAgent":"...","userId":"<id>","id":"<32>"},
 "user":{"name":"Ada","email":"ada@example.com","emailVerified":false,"image":null,"createdAt":"...Z","updatedAt":"...Z","id":"<id>"}}
```
or the literal `null`. The user output hides nothing (`account` hides tokens and password: `CORE/db/get-tables.mjs:226-262`, `BA/db/schema.mjs:52-58`). Key order is not significant.

### Effective behaviour in the Next app
- Server components call `auth.api.getSession({headers})` directly (`src/lib/auth/session.ts:10-12`). A full document load has no `RSC` header, so the DB row is refreshed, but `cookies().set` throws inside a Server Component and `nextCookies` swallows it (`BA/integrations/next-js.mjs:91-95`). The browser cookie is **not** extended there.
- Route handlers and server actions (e.g. `/api/live`, polled every 6-30 s by the shell) do write the refreshed cookie. The result is a rolling 7-day session, refreshed roughly daily.
- A Django port should refresh in middleware and attach `Set-Cookie` to the response. That is simpler and fixes the RSC quirk.

### Sign-out — `POST /api/auth/sign-out`, `BA/api/routes/sign-out.mjs:15-94`
Body (optional): `{ callbackURL?: string, disableRedirect?: boolean, state?: string }`. The client sends `{}` as JSON.
1. `token = getSignedCookie(...)`. If present: `findSession` (errors logged), then `DELETE FROM session WHERE token=?` (errors logged, not thrown).
2. Always expire `session_token`, `session_data`, `dont_remember` (`Max-Age=0`). `oauth_state` is not touched (DB state strategy).
3. Provider logout URL: only for providers with `createEndSessionURL`. Google has none, so it never applies.
4. Returns `200 {"success":true}` even with no cookie. VERIFIED.
5. The origin check applies (it is a POST): a cookie with a bad `Origin` gives 403 `INVALID_ORIGIN`; a cookie with no Origin and no Referer gives 403 `MISSING_OR_NULL_ORIGIN`. VERIFIED.

---

## 7. HTTP endpoints under `/api/auth` (Next catch-all)

`src/app/api/auth/[...all]/route.ts:5` exports only `GET` and `POST`, so other methods get Next's 405. The router is `BA/api/index.mjs:151-218`: basePath `/api/auth`, default allowed body type `application/json`, and the `originCheckMiddleware` on `/**`. `onRequest` runs the rate limiter (`:165-183`).

**Error body shape (all APIErrors):** `{"message": string, "code": string}` with the HTTP status from the error. `statusText` is the status name (e.g. `UNAUTHORIZED`), or `"400"` for validation errors. Validation errors (zod) are `400 {"message":"[body.<path>] <zod message>; ...","code":"VALIDATION_ERROR"}` (`BC/validator.mjs:46-53`, `BC/error.mjs` `ValidationError`). Error codes and messages: `CORE/error/codes.mjs:3-53`. **The UI shows `error.message` verbatim** (`sign-in-form.tsx:40-45`), so these strings are user-visible.

Endpoints actually used by the frontend: **sign-in/email, sign-up/email, sign-out, sign-in/social (only if Google is on), callback/google (Google redirect target), error (OAuth error page)**. `get-session` is not called from the browser (no `useSession` consumer), but its logic is the session read path for every page and API.

| Method + path | Used by UI | Body / query | Success | Notable errors |
|---|---|---|---|---|
| `POST /sign-up/email` | yes | `{name: string (required, may be ""), email: z.email(), password: nonempty string, image?: string, callbackURL?: string, rememberMe?: boolean}` + any extra keys (ignored; no additionalFields). Media types JSON or form-urlencoded | `200 {"token": "<32>", "user": {...}}` + session cookie | 400 `VALIDATION_ERROR` (`[body.email] Invalid email address`, `[body.name] Invalid input: expected string, received undefined`); 400 `PASSWORD_TOO_SHORT` (<8), `PASSWORD_TOO_LONG` (>128); **422 `USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL`** "User already exists. Use another email."; 422 `FAILED_TO_CREATE_USER`; 400 `FAILED_TO_CREATE_SESSION` |
| `POST /sign-in/email` | yes | `{email: string, password: string, callbackURL?: string, rememberMe?: boolean (default true)}` | `200 {"redirect": !!callbackURL, "token": "<32>", "url": callbackURL|undefined, "user": {...}}` + cookie (plus `Location` header if callbackURL) | 400 `INVALID_EMAIL` "Invalid email" (fails `z.email()`); **401 `INVALID_EMAIL_OR_PASSWORD`** "Invalid email or password" (unknown user, no credential account, null password, wrong password; hashing still runs); 401 `FAILED_TO_CREATE_SESSION`; 400 `EMAIL_PASSWORD_DISABLED` |
| `POST /sign-out` | yes | optional `{callbackURL?, disableRedirect?, state?}` | `200 {"success":true}` + three expiring cookies | 403 origin errors |
| `GET /get-session` | server-side only | query `disableCookieCache?: coerce bool`, `disableRefresh?: coerce bool` (`BA/cookies/session-store.mjs:174-181`) | `200 {session,user}` or `200 null` | 401/500 `FAILED_TO_GET_SESSION`; POST gives 405 |
| `POST /sign-in/social` | if Google | `{provider: enum, callbackURL?, newUserCallbackURL?, errorCallbackURL?, disableRedirect?, idToken?: {token, nonce?, accessToken?, refreshToken?, expiresAt?, user?}, scopes?: string[], requestSignUp?, loginHint?, additionalParams?, additionalData?: record}` (`BA/api/routes/sign-in.mjs:18-115`) | `200 {"url": "<google authorize url>", "redirect": true}` + `Location` header + signed `better-auth.state` cookie (Max-Age 300). The client navigates with `window.location.href = url` (`BA/client/fetch-plugins.mjs:3-15`) | 404 `PROVIDER_NOT_FOUND` (Google off, VERIFIED); 403 `INVALID_CALLBACK_URL` for a non-trusted absolute URL (VERIFIED) |
| `GET /callback/google` | Google redirect | query `code?, error?, error_description?, state?, device_id?, user?, iss?`. POST re-redirects as GET (`BA/api/routes/callback.mjs:38-49`) | `302 Location: callbackURL` (or newUserURL on first sign-up) + session cookie | `302` to `<baseURL>/error?error=<code>[&error_description=]` (section 8) |
| `GET /error` | OAuth failures | `?error=` | 200 HTML page (`BA/api/routes/error.mjs`) | |
| `GET /ok` | no | | `200 {"ok":true}` | |
| Others mounted (unused by UI): `/verify-email`, `/send-verification-email`, `/change-email`, `/change-password`, `/set-password`, `/update-user`, `/update-session`, `/delete-user` (+`/callback`), `/request-password-reset`, `/reset-password` (+`/:token`), `/verify-password`, `/list-sessions`, `/revoke-session(s)`, `/revoke-other-sessions`, `/link-social`, `/list-accounts`, `/unlink-account`, `/refresh-token`, `/get-access-token`, `/account-info` | no | see `BA/api/index.mjs:114-147` | | `request-password-reset` gives 400 because there is no `sendResetPassword`. These need not be ported now; `link-social` and `get-access-token` are the **Phase 3 Gmail** path (`DESIGN.md:203`) |

### Sign-up algorithm — `BA/api/routes/sign-up.mjs:142-272`
1. Check `email` against `z.email()` and the password length (min/max).
2. `normalizedEmail = email.toLowerCase()`; `SELECT ... FROM "user" WHERE email = normalizedEmail`; if it exists, return 422 (the generic-duplicate branch only applies with `requireEmailVerification` or `autoSignIn:false`, both off here).
3. `hash = scrypt(password)` (before the user insert, "so any plugin that may break the hashing should break before the user is created").
4. `INSERT "user"(id gen32, name, email lower, image, email_verified=false, created_at, updated_at)`.
5. `INSERT account(id gen32, user_id, provider_id='credential', account_id=user.id, password=hash, created_at, updated_at)`.
6. No verification email (`sendOnSignUp ?? requireEmailVerification` is false).
7. `createSession(user.id, rememberMe===false)` and set the cookie; return `{token, user}`.
Steps 4-5 are **not** in a DB transaction (adapter transactions are off), so a crash between them leaves a user without a credential account.

### Sign-in algorithm — `BA/api/routes/sign-in.mjs:305-366`
1. `z.email()` check, else 400 `INVALID_EMAIL`.
2. `findUserByEmail(email.toLowerCase(), includeAccounts)` joins accounts (`internal-adapter.mjs:568-583`).
3. `credentialAccount = accounts.find(provider_id=='credential' && account_id == user.id)`.
4. Missing user, missing credential account, or null password: hash anyway, then 401. Wrong password: 401.
5. Email-verification gate skipped (off).
6. `createSession(user.id, rememberMe===false)`, set the cookie, `Location: callbackURL` if given, return JSON.

### CSRF / origin protection (must be reproduced) — `BA/api/middlewares/origin-check.mjs`
- `originCheckMiddleware` (all non-GET/HEAD/OPTIONS router requests, `:43-70`):
  a. `validateOrigin(ctx)` (`:96-118`): skipped if no `Cookie` header (unless forced). Otherwise `origin = Origin || Referer`. The special case `Origin: null` + `Sec-Fetch-Site: same-origin` infers the base URL. Missing or `"null"` gives 403 `MISSING_OR_NULL_ORIGIN`. Not matching any trusted origin (exact `scheme://host[:port]` equality, `BA/auth/trusted-origins.mjs:88-104`) gives 403 `INVALID_ORIGIN`.
  b. Validates `body.callbackURL`/`query.callbackURL`, `body.redirectTo`, `body.errorCallbackURL` and `body.newUserCallbackURL` against trusted origins. **Relative paths are allowed** if `isSafeRelativeURL` (`:68-78`): starts with `/`, not `//`, no `\`, no control chars, no `%2f`/`%5c` in the path. Failure gives 403 `INVALID_CALLBACK_URL` etc.
- `formCsrfMiddleware` (sign-in/email and sign-up/email, `:123-153`). With a cookie: `validateOrigin`. Without a cookie: if any of `Sec-Fetch-Site/Mode/Dest` is present, `site=="cross-site" && mode=="navigate"` gives 403 `CROSS_SITE_NAVIGATION_LOGIN_BLOCKED`; otherwise force-validate the origin. If only `Origin`/`Referer` is present, force-validate. No headers at all means allowed (non-browser clients). All VERIFIED.
- All of this is skipped when `NODE_ENV=test`.

### Rate limiting (production only) — `BA/api/rate-limiter/index.mjs`
- Key `"<ip>|<path>"`. The IP comes from `x-forwarded-for` **only if it holds exactly one valid IP** (no trusted proxies configured), else the shared key `no-trusted-ip` (`CORE/utils/ip.mjs:174-219`, `rate-limiter:234-245`). In the Docker prod profile there is usually no XFF, so **all clients share one bucket per path**.
- Rules: default 100 per 10 s. `/sign-in*`, `/sign-up*`, `/change-password*`, `/change-email*`: **3 per 10 s**. `/request-password-reset`, `/send-verification-email`, `/forget-password*`: 3 per 60 s (`:302-316`).
- Fixed-window-ish semantics (`decideConsume`, `:25-63`); in-memory and per-process.
- Response: `429 {"message":"Too many requests. Please try again later."}`, header `X-Retry-After: <seconds>`.

---

## 8. Google OAuth (configured only when both env vars are set; currently off)

Provider: `CORE/social-providers/google.mjs:52-137`.

**Start** (`POST /sign-in/social {provider:"google", callbackURL: next}` from `sign-in-form.tsx:129`):
1. `state = generateRandomString(32)` over `a-z0-9A-Z-_` (`BA/crypto/random.mjs:3`). `codeVerifier = generateRandomString(128)` (`BA/oauth2/state.mjs:20`). Google does not require a nonce.
2. `INSERT verification(identifier=state, value=JSON{callbackURL, codeVerifier, errorURL?, newUserURL?, expiresAt: Date.now()+600000, requestSignUp?, oauthState: state}, expires_at = now+10min)`. Set the signed cookie `better-auth.state=<state>.<sig>` with Max-Age 300 (`BA/state.mjs:72-87`). VERIFIED.
3. Authorization URL (`CORE/oauth2/create-authorization-url.mjs:20-59`), VERIFIED:
   `https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id=<id>&state=<state>&scope=email+profile+openid&redirect_uri=<BETTER_AUTH_URL>/api/auth/callback/google&code_challenge_method=S256&code_challenge=<b64url(sha256(verifier)) no pad>&include_granted_scopes=true`.
   There is **no** `access_type=offline` or `prompt`, so Google returns no refresh token. Phase 3 will need offline access.
4. Response `200 {"url": ..., "redirect": true}`.

**Callback** `GET /api/auth/callback/google?code&state` (`BA/api/routes/callback.mjs:26-259`):
1. No `state` gives a redirect to `<baseURL>/error?error=state_not_found` (VERIFIED). `parseGenericState` (`BA/state.mjs:89-147`): look up the verification row by identifier (latest by `created_at`; also purges expired rows); none means `state_mismatch`. Then require `value.oauthState == state` and the signed `state` cookie == state (else `state_mismatch`), expire the state cookie, `DELETE verification WHERE identifier=state`, and check `expiresAt` against now.
2. `error` param redirects to the error URL with `error` (and `error_description`). Missing code gives `no_code`.
3. Token exchange (`CORE/oauth2/validate-authorization-code.mjs:29-71`, `token-endpoint-auth.mjs:5-9`): `POST https://oauth2.googleapis.com/token` form-urlencoded `grant_type=authorization_code, code, code_verifier, redirect_uri, client_id, client_secret` (client_secret_post), `accept: application/json`; redirects refused. Tokens: `access_token`, `refresh_token`, `id_token`, `expires_in` → `accessTokenExpiresAt = now+expires_in`, `scope` split on whitespace (`CORE/oauth2/utils.mjs:17-31`). Failure gives `invalid_code`.
4. User info: **`decodeJwt(id_token)` without signature verification** (the token came from the TLS token endpoint) → `{name, email, image: picture, emailVerified: email_verified}`. `accountId = profile.sub` (`google.mjs:56`, `:116-135`).
5. `handleOAuthUserInfo` (`BA/oauth2/link-account.mjs:12-281`):
   - Account `(provider_id='google', account_id=sub)` exists: sign that user in. Update `id_token`, `access_token`, `refresh_token` and expiries on the account (not `scope`; `updateAccountOnSignIn` defaults to on). If Google says verified and the local user is not, with the same email, set `email_verified=true`.
   - Else a user with the same (lowercased) email exists: **link only if (Google `email_verified` OR provider trusted) AND the local user's `email_verified` is true** (`requireLocalEmailVerified` defaults to true, `:78-85`). Email/password users always have `email_verified=false`, so a Google sign-in for an existing password user **fails** with `?error=account_not_linked`.
   - Else create the user (`name, image, email lower, email_verified` from Google) and the account (`scope` = comma-joined). This is "transactional" but adapter transactions are off.
   - `createSession(user.id)`, set the cookie, `302 Location: callbackURL` (a relative `/home` is fine), or `newUserURL || callbackURL` when newly registered.
6. Error codes (`BA/oauth2/errors.mjs:9-23`): `no_code, oauth_provider_not_found, issuer_mismatch, invalid_code, unable_to_get_user_info, no_callback_url, unable_to_link_account, email_not_found, email_not_verified, state_not_found, state_mismatch, account_not_linked, signup_disabled, unable_to_create_user, unable_to_create_session` (underscored from `result.error`).
- Google console redirect URI to register: `${BETTER_AUTH_URL}/api/auth/callback/google`.

---

## 9. App-side auth surfaces (Next)

### `getSession()` — `src/lib/auth/session.ts:10-12`
`auth.api.getSession({ headers: await headers() })` returns `{session, user} | null` (section 6). Used by `src/app/page.tsx:7` and `src/app/(auth)/sign-in/page.tsx:22`.

### `requireUser()` — `src/lib/auth/session.ts:21-25`
`session?.user` or `redirect("/sign-in")`, which throws `NEXT_REDIRECT`. Status: **307** from Server Components and Route Handlers; for Server Actions, a client navigation (or 303 for no-JS forms) (`node_modules/next/dist/docs/01-app/03-api-reference/04-functions/redirect.md:12,213-215`). It returns the **user object** `{id, name, email, emailVerified, image, createdAt, updatedAt}`.
- Invariant comment (`:14-20`): "Every page and action behind auth starts here, and every query module takes the returned id as its first argument — there is no unscoped read path to per-user data (DESIGN.md invariant 8)" (`DESIGN.md:51-52`).
- 40 call sites (`grep requireUser()`): pages `(app)/{layout,home,matches,matches/[id],chat/[[...id]],activity,profile,companies,settings}`; actions `src/app/actions/{ping,runs,settings,profile,approvals,companies,matches,resumes}.ts`; API routes `src/app/api/{chat,live,matches,uploads,conversations,conversations/[id],documents/[id],runs/[id],runs/[id]/events,resumes/[id]/preview}/route.ts`.
- Fields used: `user.id` everywhere, `user.name` (`home/page.tsx:40`, `api/chat/route.ts:63`, `settings/page.tsx:274-278`), `user.email` (`layout.tsx:16`, `settings/page.tsx`). `src/lib/chat/follow-up.ts:122-126` reads `user.name` from the table directly.
- `(app)/layout.tsx:6-10`: "Pages still call requireUser() themselves — a layout does not re-run on every navigation, so it cannot be the only guard."
- **API contract today:** an unauthenticated `fetch` to e.g. `/api/live` gets a **307 to `/sign-in`**, not a 401. A Django API should return 401 JSON, and the frontend must handle it (cross-area).

### `proxy.ts` (Next 16 middleware) — `src/proxy.ts:12-33`
- "An optimistic redirect, not an authorization check" (`:4-11`): if `getSessionCookie(request)` (presence only) is set, continue; else **307** to `/sign-in?next=<pathname>` (pathname only, query dropped).
- Matcher: `/home/:path*, /matches/:path*, /chat/:path*, /onboarding/:path*, /pipeline/:path*, /jobs/:path*, /activity/:path*, /profile/:path*, /companies/:path*, /settings/:path*`. **Not** `/api/*` or `/sign-in`.
- With Next kept as the frontend, this stays in Next but must look for whatever cookie name Django issues. Keeping `better-auth.session_token` needs no change.

### Sign-in page — `src/app/(auth)/sign-in/page.tsx:11-31`
- `safeNext(next)`: accept only `next.startsWith("/") && !next.startsWith("//")`, else `/home`.
- If already signed in, `redirect(next)`. `googleEnabled = Boolean(GOOGLE_CLIENT_ID && GOOGLE_CLIENT_SECRET)` is passed to the form. A Django port must expose this flag, e.g. in a config endpoint.
- `/` (`src/app/page.tsx:6-8`) redirects to `/home` if a session exists, else `/sign-in`.

### `SignInForm` — `src/components/auth/sign-in-form.tsx`
- Modes `sign-in` | `sign-up` (`:11,21`). Inputs: `name` (sign-up only), `email` (`type=email required`), `password` (`minLength=8 required`; hint "At least 8 characters.").
- Submit (`:28-52`): sign-up calls `signUp.email({ name: name || email.split("@")[0], email, password })`; sign-in calls `signIn.email({ email, password })`. These become `POST /api/auth/sign-up/email` or `/sign-in/email` with a JSON body, `credentials: "include"`.
- On error, show `result.error.message`; fallbacks "That account could not be created." / "That email and password do not match an account." (`:38-46`).
- On success: `router.push(mode === "sign-up" ? "/home" : next)` then `router.refresh()` (comment: "A brand-new account starts at onboarding, not an empty dashboard").
- Google button (`:126-135`): `signIn.social({ provider: "google", callbackURL: next })`.
- Copy strings ("Welcome back", "Start your search", button states "Creating your account…" / "Signing in…") are frontend-only.

### Sign-out — `src/components/settings/sign-out-button.tsx:19-29`, `src/components/shell/user-menu.tsx:81-87`
`signOut()` sends `POST /api/auth/sign-out` with body `{}` and `credentials: "include"`. On `result.error`, toast "Signing out did not work. Reload the page and try again." (settings only). Then `router.push("/sign-in"); router.refresh()`. The user-menu variant ignores errors.

### Browser client — `src/lib/auth/client.ts:9-11`, `BA/client/config.mjs:22-111`
- `createAuthClient()` with no baseURL uses the same origin `/api/auth`, `credentials: "include"`, `method` per path (`/sign-out` is forced to POST, `:67-72`). Path is the kebab-cased property path (`signIn.email` becomes `/sign-in/email`, `BA/client/proxy.mjs:29-46`).
- Response handling (better-fetch): `{data, error}`, where `error = {status, statusText, message, code}` comes from the JSON error body.
- After `/sign-in/email`, `/sign-up/email` or `/sign-out` it toggles the session signal (`:73-82`). Nothing subscribes (`useSession` is exported but unused), so no `get-session` calls are made from the browser.
- Redirect plugin: if the response JSON has `url` and `redirect:true`, set `window.location.href = url` (social sign-in).

---

## 10. Scripts

### `scripts/set-password.ts` — `pnpm auth:set-password <email>`
- Purpose (`:9-19`): no mail transport, so there is no reset link. The password is typed at a hidden prompt "so it stays out of shell history". "Hashing goes through Better Auth's own context … so sign-in accepts the result. Existing sessions are kept."
- Flow: `SELECT * FROM "user" WHERE email = argv[2] LIMIT 1` (**exact-case match**, no lowercasing, `:25`). Else error "No user with email X." Then `findAccounts(user.id)`; it needs a row with `provider_id='credential'`, else "<email> does not sign in with a password." (`:28-32`).
- Prompt `New password for <email>: ` with raw-mode TTY input (Backspace `\x7f`/`\b` deletes, Ctrl-C cancels with "Cancelled.", non-TTY rejects with "Run this in a terminal…", `:51-83`). Length must be 8 to 128, else "Passwords must be 8 to 128 characters." Then "Type it again: "; a mismatch gives "Those did not match. Nothing was changed."
- `updatePassword(userId, scrypt(pw))` runs `UPDATE account SET password=?, updated_at=now WHERE user_id=? AND provider_id='credential' AND account_id=?` (`BA/db/internal-adapter.mjs:627-642`). Prints `Password updated. Sign in at <BETTER_AUTH_URL>/sign-in`. Sessions are untouched. Errors print the message and exit 1.
- Django equivalent: a management command (`getpass` twice, same checks and messages, same UPDATE with the section 4 hasher).

### `scripts/dev-session.ts` — `pnpm tsx scripts/dev-session.ts [email]`
- Refuses `NODE_ENV=production` ("dev-session is for local development only.") and a missing `BETTER_AUTH_SECRET`.
- Picks the user by exact email, or **any first user** (`SELECT * FROM "user" LIMIT 1`) if no arg. Errors: "No user with email X." / "No users yet."
- `token = randomBytes(24).base64url` (32 chars, may include `-`/`_`). `INSERT session(id = randomBytes(16).hex, token, user_id, expires_at = now+7d, user_agent = 'dev-session script')`; `created_at`/`updated_at` take the DB default; `ip_address` NULL.
- Prints `user: <email>` and `cookie: better-auth.session_token=<encodeURIComponent(token + "." + base64(HMAC-SHA256(secret, token)))>`. CLAUDE.md's pane-testing workflow depends on this output format (set via `document.cookie`, dropping the `cookie: ` prefix).
- Django equivalent: a management command with an identical output format. If the cookie name or format changes, CLAUDE.md "Driving the app in a browser" must change too.

---

## 11. Invariants the port must keep

1. Existing `account.password` hashes verify with scrypt(N=16384,r=16,p=1,dkLen=64), NFKC, salt = the hex string's bytes; format `salt:key` (`UTILS/password.node.mjs:3-42`).
2. Credential account identity: `provider_id='credential' AND account_id = user_id` (`sign-up.mjs:242-247`, `sign-in.mjs:316`, `internal-adapter.mjs:652-670`).
3. Emails are lowercased on write and on lookup (`internal-adapter.mjs:140-146,568-575`; `sign-up.mjs:165`). Note: `set-password`/`dev-session` look up by exact email.
4. Sessions: 7-day expiry, refreshed once at least a day has passed since the last issue (`session.mjs:178-180`). An expired session is deleted and its cookies cleared on read (`:155-165`).
5. Cookie is `better-auth.session_token` (or the `__Secure-` variant over https) with value `token.base64(HMAC-SHA256(secret, token))`, URL-encoded, HttpOnly, SameSite=Lax, Path=/, Max-Age=604800 (`BA/cookies/index.mjs:20-75,167-180`; `BC/crypto.mjs:21-31`).
6. A signature must be exactly 44 chars ending in `=` (`BC/context.mjs:47`).
7. Sign-in errors do not reveal whether the email exists (same 401 and message; hash computed on every path) (`sign-in.mjs:317-335`).
8. Password length is 8 to 128 (`create-context.mjs:186-187`, `sign-up.mjs:152-161`, `set-password.ts:34-41`).
9. CSRF: a state-changing auth POST carrying cookies needs a trusted Origin/Referer; a cookie-less login blocks `Sec-Fetch-Site: cross-site` + `Sec-Fetch-Mode: navigate` (`origin-check.mjs:96-153`).
10. Callback URLs: same-origin absolute or a safe relative path only (`trusted-origins.mjs:68-104`). The sign-in page's `next` follows the same idea (`sign-in/page.tsx:12-14`).
11. Every per-user query is scoped by the authenticated `user.id` (`session.ts:14-20`, `DESIGN.md:51-52`).
12. `/sign-in?next=<path>` redirect for protected page prefixes (`proxy.ts:12-32`). Signed-in visitors to `/sign-in` or `/` go to `next` or `/home`.
13. Error JSON `{message, code}` with the statuses in section 7; message strings are shown to users.
14. Rate limit in production: 3 requests per 10 s on `/sign-in*` and `/sign-up*` (`rate-limiter/index.mjs:302-316`).
15. No email verification and no reset mail. `set-password` is the recovery path and keeps sessions.

---

## 12. Port hazards

- **scrypt `maxmem`**: Python's `hashlib.scrypt` needs `maxmem` of at least ~33 MiB (use 64 MiB) or it raises "memory limit exceeded". The parameters cost about 32 MiB of RAM and around 50-100 ms of CPU per hash. Use a thread pool if served async, and keep the hash-on-miss timing equalisation.
- **Salt quirk**: the salt is the hex string, not its bytes. Getting this wrong fails every login silently.
- **Django's user model**: the password is not on `"user"`; it lives in `account`. `"user"` has no `password`/`last_login`/`is_active`/`is_staff`. Using `AbstractBaseUser` against this table needs either new columns (schema change shared with any remaining TS code) or a custom auth backend plus a custom middleware that sets `request.user` from the Better Auth cookie, skipping `django.contrib.sessions`. Django's own `sessionid` cookie and `django_session` table would log everyone out and break `dev-session`.
- **Naive timestamps**: the columns are `timestamp without time zone` holding UTC. With `USE_TZ=True`, Django on Postgres returns naive datetimes for these columns and warns or errors on aware/naive comparisons. Options: an `ALTER ... TYPE timestamptz USING col AT TIME ZONE 'UTC'` migration (safe because the DB tz is UTC and values are UTC), or explicit naive-UTC handling. Every `expires_at < now` comparison must use UTC.
- **Reserved table name** `"user"` needs quoting. Use `db_table = 'user'` (Django quotes it). The tables are owned by Drizzle migrations today; decide whether Django models are `managed=False` or Django takes over migrations (the `account_provider_account_idx` drop history matters).
- **Cookie encoding**: Django's cookie parser (`django.http.cookie.parse_cookie`) only strips RFC quoted-string quoting. It does **not** percent-decode, so `%2B`, `%2F` and `%3D` stay encoded in `request.COOKIES`. Call `urllib.parse.unquote` before splitting on the last `.`. Django's dict is also last-occurrence-wins, while better-call is first-occurrence-wins (`BC/cookies.mjs:33`). When setting, pre-encode with `urllib.parse.quote(signed, safe="")`, which is equivalent to `encodeURIComponent` for this alphabet (checked), so the value is `[A-Za-z0-9._%-]` and `SimpleCookie` will not re-quote it. Django was not installed locally, so this was not executed against Django itself.
- **Secret handling**: use the `BETTER_AUTH_SECRET` string as-is (UTF-8 bytes) for HMAC. Do not base64-decode it, and do not let Django's `SECRET_KEY` or signing framework replace it.
- **`__Secure-` prefix** depends on `BETTER_AUTH_URL`'s scheme. Behind TLS in production, the cookie name changes and `Secure` is set.
- **The RSC refresh quirk disappears** in Django. Make sure the refreshed `Set-Cookie` is emitted on API responses (the Next frontend's server-side fetches to Django must forward `Set-Cookie` back to the browser, or the browser must call Django directly).
- **Frontend server-side calls**: Next server components currently read the session in-process. As a frontend only, Next must forward the incoming `Cookie` header to Django (e.g. a `/api/auth/get-session`-style endpoint) for SSR, and relay any `Set-Cookie`. The `proxy.ts` presence check can stay.
- **307 vs 401**: today's API routes answer unauthenticated calls with a 307 to `/sign-in`. A Django API returning 401 changes client behaviour (`useLive` and fetch callers).
- **CSRF**: Django's CSRF middleware (token-based) differs from Better Auth's Origin/Fetch-Metadata check. The existing browser client sends no CSRF token, so either exempt the auth endpoints and re-implement the origin check, or change the client.
- **Not transactional today**: sign-up's user and account inserts. Wrapping them in `transaction.atomic()` in Django is strictly better.
- **Google id_token is not signature-verified** in the callback flow (`google.mjs:116-119`). A Python port using `authlib`/`google-auth` would normally verify it; that is stricter and fine.
- **Account linking rule**: Google cannot attach to an existing password user, because `email_verified=false` and `requireLocalEmailVerified` defaults to true. Keep or consciously change this.
- **Rate limiter IP**: keyed on single-value `X-Forwarded-For`; without a proxy, all users share a bucket. Django should use a trusted-proxy-aware IP and a shared store (cache) if it runs multiple workers.
- **`better-auth generate`** (`pnpm auth:generate`) and the Drizzle schema-check in `DRZ` stop mattering once Better Auth is gone. While both stacks run, Better Auth's schema expectations still constrain changes to these four tables.
- **Mixed id formats**: dev-session ids are hex, test user ids are `test-*`. Never validate ids against the 32-alnum pattern.

---

## 13. External dependencies and Python candidates

| Dependency | Use here | Python candidate |
|---|---|---|
| `better-auth` 1.7.5 (+ `@better-auth/core`, `@better-auth/drizzle-adapter`, `better-call`) | auth server, routing, cookies, sessions, OAuth | custom Django app (views + middleware); `django-allauth` or `authlib` for Google |
| `@better-auth/utils/password` (node:crypto scrypt) | password hash | `hashlib.scrypt` (stdlib) |
| WebCrypto HMAC-SHA256 | cookie signatures | `hmac` + `hashlib` (stdlib) |
| `better-auth/react` `createAuthClient` (+ `@better-fetch/fetch`, `nanostores`) | browser auth calls | stays in Next, or is replaced by a small `fetch` wrapper against Django endpoints |
| `better-auth/next-js` (`toNextJsHandler`, `nextCookies`) | mount and cookie bridge | Django URLconf; not needed |
| `better-auth/cookies` `getSessionCookie` | proxy presence check | stays in Next (or `request.cookies.has(...)`) |
| `zod` 4 (`z.email()`) | body validation and email check | `pydantic` / DRF serializers; `email-validator` (zod's regex is stricter than RFC) |
| `jose` (`decodeJwt`, JWKS for Google id tokens) | Google | `PyJWT[crypto]` / `authlib.jose` / `google-auth` |
| `drizzle-orm` / `pg` | tables | Django ORM (psycopg 3) |
| `@better-auth/cli` | schema generation | none |
| Next `redirect()` / `headers()` | requireUser | Django `login_required`-style decorator / DRF `IsAuthenticated` |
