import { api, call } from "./client";
import type { components } from "./schema";

/**
 * Signing in, up and out, from the browser (design §6.2).
 *
 * Each call goes through Next's rewrite to Django's `/api/auth/*`, so the browser receives
 * Django's `Set-Cookie` itself; the caller follows a success with `router.refresh()` (or a push).
 * Wrong credentials are a 400 with Django's sentence, never a 401, and `call()` never redirects
 * for `/api/auth/*` anyway, so the form always shows the server's message.
 */

type AuthUser = components["schemas"]["AuthUser"];

/** What every auth call answers: success, or Django's (or `call()`'s) sentence. */
export type AuthResult<T extends object = object> =
  | ({ ok: true } & T)
  | { ok: false; message: string };

type Envelope = { ok: boolean; message?: string };

const NOT_DONE = "That did not work. Try again.";

function settle<T extends object>(body: Envelope, success: T): AuthResult<T> {
  return body.ok ? { ok: true, ...success } : { ok: false, message: body.message || NOT_DONE };
}

export async function signIn(body: { email: string; password: string }): Promise<AuthResult<{ user: AuthUser }>> {
  const r = await call<Envelope & { user?: AuthUser }>(api.POST("/api/auth/sign-in", { body }));
  return settle(r, { user: r.user as AuthUser });
}

export async function signUp(body: {
  name?: string;
  email: string;
  password: string;
}): Promise<AuthResult<{ user: AuthUser }>> {
  const r = await call<Envelope & { user?: AuthUser }>(api.POST("/api/auth/sign-up", { body }));
  return settle(r, { user: r.user as AuthUser });
}

/** A session that is already gone (401 "Not signed in.") counts as signed out. */
export async function signOut(): Promise<AuthResult> {
  const r = await call<Envelope>(
    api.POST("/api/auth/sign-out").then((res) =>
      res.response.status === 401 ? { data: { ok: true }, response: res.response } : res,
    ),
  );
  return settle(r, {});
}
