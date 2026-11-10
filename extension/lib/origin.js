/*
 * The Foray address the user types on the options page, turned into the two things the
 * extension needs: the origin its requests go to, and the pattern Chrome is asked to allow.
 * Pure, and unit-tested (extension/test/unit/origin.test.js).
 */

const LOCAL = /^(localhost|127(?:\.\d{1,3}){3}|\[::1\]|[^.]+\.localhost)$/i;
const HOST = /^(?:[a-z0-9-]+(?:\.[a-z0-9-]+)*|\[[0-9a-f:.]+\])$/i;
const SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i;
const NOT_AN_ADDRESS = "That does not read as an address. Try http://localhost:3000.";

/**
 * `{ok: true, origin, pattern, plain}` for an address Foray can be reached at, else
 * `{ok: false, message}`. `plain` is true for http to a machine that is not this one: the
 * token would cross the network unencrypted, which the options page says before saving.
 */
export function parseAddress(input) {
  const typed = String(input ?? "").trim();
  if (!typed) return { ok: false, message: "Type the address you open Foray at." };
  // Without a scheme: http for this machine, https for any other.
  const first = typed.startsWith("[") ? typed.split("]")[0] + "]" : typed.split(/[/:?#]/)[0];
  const withScheme = SCHEME.test(typed)
    ? typed
    : `${LOCAL.test(first) ? "http" : "https"}://${typed}`;
  let url;
  try {
    url = new URL(withScheme);
  } catch {
    return { ok: false, message: NOT_AN_ADDRESS };
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return { ok: false, message: "The address must start with http:// or https://." };
  }
  // Chrome's parser lets a space through as %20; a host is letters, digits, dots and hyphens.
  if (!HOST.test(url.hostname)) return { ok: false, message: NOT_AN_ADDRESS };
  if (url.username || url.password) {
    return { ok: false, message: "Leave any user name and password out of the address." };
  }
  return {
    ok: true,
    origin: url.origin,
    // Chrome's patterns have no port: allowing a host allows every port on it.
    pattern: `${url.protocol}//${url.hostname}/*`,
    plain: url.protocol === "http:" && !LOCAL.test(url.hostname),
  };
}
