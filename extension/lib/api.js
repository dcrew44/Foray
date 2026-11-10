/*
 * Foray's API, as the extension uses it. Only the service worker imports this, so the token
 * is read in exactly one place and never crosses into a page.
 *
 * Every request carries the token as `Authorization: Bearer` and no cookie
 * (`credentials: "omit"`): Foray refuses a request to these routes that carries its session
 * cookie, because that is a browser tab, not the extension.
 */
import { parseAddress } from "./origin.js";

export const CONNECTION = "connection"; // {origin, token} in chrome.storage.local

const fail = (kind, message, status = 0) => ({ ok: false, error: { kind, message, status } });

/** The saved address and token, or null until both are set. */
export async function connection() {
  const saved = (await chrome.storage.local.get(CONNECTION))[CONNECTION];
  return saved?.origin && saved?.token ? saved : null;
}

/**
 * One request. Answers `{ok: true, data}` (parsed JSON, or `{bytes, type, filename}` for
 * `binary`) or `{ok: false, error: {kind, message, status}}`; it never throws.
 */
export async function request(path, { method = "GET", body, binary = false, timeout = 20000 } = {}) {
  const saved = await connection();
  if (!saved) {
    return fail("unconfigured", "Foray is not connected yet. Open the extension's settings.");
  }
  const address = parseAddress(saved.origin);
  if (!address.ok) return fail("unconfigured", "The saved Foray address is not valid.");
  if (!(await chrome.permissions.contains({ origins: [address.pattern] }))) {
    return fail(
      "permission",
      `The extension is not allowed to reach ${saved.origin}. Open its settings and press Save.`,
    );
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  let response;
  try {
    response = await fetch(`${address.origin}${path}`, {
      method,
      credentials: "omit",
      cache: "no-store",
      redirect: "error", // the token goes to the address the user typed and nowhere else
      referrerPolicy: "no-referrer",
      headers: {
        Authorization: `Bearer ${saved.token}`,
        Accept: binary ? "application/pdf" : "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch {
    return fail("network", `Foray could not be reached at ${saved.origin}. Check that it is running.`);
  } finally {
    clearTimeout(timer);
  }
  if (response.ok && binary) {
    const named = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(
      response.headers.get("Content-Disposition") ?? "",
    );
    return {
      ok: true,
      data: {
        bytes: new Uint8Array(await response.arrayBuffer()),
        type: (response.headers.get("Content-Type") ?? "application/pdf").split(";")[0],
        filename: named ? decodeURIComponent(named[1]) : "",
      },
    };
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (response.ok && data !== null) return { ok: true, data };
  if (response.status === 401) {
    return fail("unauthorized", "Foray did not accept this token. Make a new one in Foray's Settings.", 401);
  }
  const said = typeof data?.message === "string" && data.message ? data.message : "";
  return fail(
    "refused",
    said || `Foray answered with an error (${response.status}).`,
    response.status,
  );
}
