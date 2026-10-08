import "server-only";

import { cookies, headers } from "next/headers";
import { notFound, redirect } from "next/navigation";

/**
 * Server components read Django through here (design §6.1): one GET per page, as the visitor.
 *
 * - The visitor's cookies go along, so Django sees their session. `cookies()` is a request-time
 *   API, so every page that calls this stays dynamic; there is no Next-side caching.
 * - `X-Foray-SSR: 1` tells Django's `sliding_session` not to refresh the session here: a
 *   `Set-Cookie` on this response would land on the Next server and be dropped (§4.1).
 * - 401 sends the visitor to sign in, back to the page they asked for (`x-foray-path`, set by
 *   `src/proxy.ts`), unless the caller asked for `null` instead (`/` and `/sign-in` only, so a
 *   signed-out visit there cannot loop). 404 is `notFound()`. Anything else that is not 2xx
 *   throws, and `(app)/error.tsx` shows it.
 *
 * Client components never import this module (ESLint boundary in `eslint.config.mjs`); they
 * call Django through `./client`.
 */

const ORIGIN = process.env.DJANGO_ORIGIN ?? "http://127.0.0.1:8000";

type Opts = { onUnauthorized?: "redirect" | "null" };

/**
 * `GET /api/me` (§4.4). Written by hand until the accounts router has the operation; step 8A can
 * switch it to the generated schema.
 */
export type Me = { id: string; name: string; email: string; signupOpen: boolean };

export async function django<T>(path: string, opts: { onUnauthorized: "null" }): Promise<T | null>;
export async function django<T>(path: string, opts?: Opts): Promise<T>;
export async function django<T>(path: string, { onUnauthorized = "redirect" }: Opts = {}) {
  const jar = await cookies();
  const res = await fetch(new URL(path, ORIGIN), {
    headers: { cookie: jar.toString(), "x-foray-ssr": "1" },
    cache: "no-store",
  });
  if (res.status === 401) {
    if (onUnauthorized === "null") return null;
    const here = (await headers()).get("x-foray-path") ?? "/home";
    redirect(`/sign-in?next=${encodeURIComponent(here)}`);
  }
  if (res.status === 404) notFound();
  if (!res.ok) throw new Error(`Django ${path} -> ${res.status}`);
  return res.json() as Promise<T>;
}

export const getMe = () => django<Me>("/api/me");

/** For `/` and `/sign-in` only: they must render, or redirect elsewhere, when signed out. */
export const getMeOrNull = () => django<Me>("/api/me", { onUnauthorized: "null" });
