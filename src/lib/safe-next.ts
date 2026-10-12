/**
 * Where sign-in sends a visitor next: a path on this site, never another site.
 *
 * `next` comes from the URL, so anyone can craft it. Checking that it starts with "/" but not
 * "//" is not enough: browsers read a backslash as a slash and drop tabs and newlines, so
 * "/\evil.example" and "/<tab>/evil.example" are scheme-relative links to evil.example. Such
 * characters are refused outright, and what remains is resolved against a fixed origin and kept
 * only if it stays there. Resolving collapses dot segments, which can itself produce a leading
 * "//" ("/.//evil.example" and "/a/..//evil.example" both become "//evil.example"), so the
 * resolved path must pass the same check as the input. Pure: the sign-in page (server) and the
 * form (client) both use it.
 */

export const DEFAULT_NEXT = "/home";

const BASE = "http://foray.invalid";
// A backslash, an ASCII control character, a space or DEL.
const UNSAFE = /[\\\u0000- \u007f]/;

/** One leading slash and nothing a browser would bend: a link that cannot leave the page's
 *  origin, whatever that origin is. */
function isLocalPath(path: string): boolean {
  return path.startsWith("/") && !path.startsWith("//") && !UNSAFE.test(path);
}

export function safeNext(next: unknown): string {
  // A repeated `?next=` arrives as an array: not a path either.
  if (typeof next !== "string" || !isLocalPath(next)) return DEFAULT_NEXT;
  let url: URL;
  try {
    url = new URL(next, BASE);
  } catch {
    return DEFAULT_NEXT;
  }
  if (url.origin !== BASE) return DEFAULT_NEXT;
  const out = url.pathname + url.search + url.hash;
  return isLocalPath(out) ? out : DEFAULT_NEXT;
}
