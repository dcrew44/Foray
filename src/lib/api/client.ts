import createClient from "openapi-fetch";

import type { paths } from "./schema";

/**
 * Client components call Django directly through here (design §6.2); there are no server
 * actions. Requests go to `/api/*` on the page's own origin, and Next's rewrite forwards them, so
 * the session cookie and Django's `Set-Cookie` both travel natively.
 *
 * `paths` is generated from Django's OpenAPI schema (`pnpm api:types`), so every path, parameter,
 * body and response is typed.
 */
export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });

const NOT_READ = "That request did not go through. Try again.";
const NOT_REACHED = "Could not reach Foray. Check that it is running.";

/** Django's answer to every mutation, success or failure (§5.2). */
function isEnvelope(body: unknown): body is { ok: boolean; message?: string } {
  return typeof body === "object" && body !== null && typeof (body as { ok?: unknown }).ok === "boolean";
}

/** A failure the caller handles like Django's own `{ok: false, message}`. */
const failure = <T>(message: string) => ({ ok: false, message }) as unknown as T;

/**
 * Await an `api.*` call and return Django's `{ok, message, …}` body whatever the status, so
 * callers keep branching on `ok`:
 *
 * ```ts
 * const r = await call(api.POST("/api/approvals/{id}/decision", { params: { path: { id } }, body }));
 * router.refresh();
 * if (!r.ok) toast.error(r.message);
 * ```
 *
 * A 401 ("Not signed in.") also sends the browser to sign in and back, except for the auth
 * endpoints themselves and on `/sign-in`, so the sign-in form always shows the server's message.
 * A body that is not Django's envelope (the rewrite's bare 500 while Django is down, say) becomes
 * a generic failure, and so does a network error.
 */
export async function call<T extends { ok: boolean }>(
  p: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  try {
    const { data, error, response } = await p;
    if (
      response.status === 401 &&
      !new URL(response.url).pathname.startsWith("/api/auth/") &&
      location.pathname !== "/sign-in"
    ) {
      // A full page load on purpose: the session is gone, so no client state should survive, and
      // this runs outside React, where there is no router to push with.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      location.assign(`/sign-in?next=${encodeURIComponent(location.pathname + location.search)}`);
    }
    if (data !== undefined) return data;
    if (isEnvelope(error)) return error as T;
    return failure<T>(NOT_READ);
  } catch {
    return failure<T>(NOT_REACHED);
  }
}
