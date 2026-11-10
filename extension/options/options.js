/*
 * The options page: where Foray is, and the token that signs the extension in to it.
 *
 * The token is written to `chrome.storage.local` from here and read only by the service
 * worker. Once saved it is never put back on this page.
 */
import { CONNECTION } from "../lib/api.js";
import { parseAddress } from "../lib/origin.js";

const form = document.getElementById("connect");
const address = document.getElementById("address");
const token = document.getElementById("token");
const save = document.getElementById("save");
const forget = document.getElementById("forget");
const status = document.getElementById("status");

const SAVED_HINT = "A token is saved. Paste a new one to replace it.";
const TOKEN_PREFIX = "foray_ext_";

let saved = null; // what storage holds, read when the page opens

function say(text, kind = "") {
  status.textContent = text;
  status.className = kind;
}

function draw() {
  token.placeholder = saved?.token ? SAVED_HINT : `${TOKEN_PREFIX}…`;
  forget.hidden = !saved;
}

async function test() {
  say("Checking the connection…", "muted");
  const answer = await chrome.runtime.sendMessage({ type: "test-connection" });
  if (answer?.ok) {
    const waiting =
      answer.waiting === 1
        ? "1 application is waiting."
        : `${answer.waiting} applications are waiting.`;
    say(`Connected as ${answer.name}. ${waiting}`, "ok");
  } else {
    say(answer?.error?.message ?? "The connection could not be checked.", "error");
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const parsed = parseAddress(address.value);
  if (!parsed.ok) return say(parsed.message, "error");
  const pasted = token.value.trim();
  if (pasted && !pasted.startsWith(TOKEN_PREFIX)) {
    return say(`That does not look like a Foray token. They start with ${TOKEN_PREFIX}.`, "error");
  }
  const secret = pasted || saved?.token || "";
  if (!secret) return say("Paste a token from Foray's Settings.", "error");

  save.disabled = true;
  try {
    // Asked while this is still the user's click, which is when Chrome will show its prompt.
    const allowed = await chrome.permissions.request({ origins: [parsed.pattern] }).catch(() => false);
    if (!allowed) {
      return say(`Chrome was not allowed to reach ${parsed.origin}, so nothing was saved.`, "error");
    }
    const before = saved?.origin ? parseAddress(saved.origin) : null;
    saved = { origin: parsed.origin, token: secret };
    await chrome.storage.local.set({ [CONNECTION]: saved });
    if (before?.ok && before.pattern !== parsed.pattern) {
      await chrome.permissions.remove({ origins: [before.pattern] }).catch(() => false);
    }
    address.value = parsed.origin;
    token.value = "";
    draw();
    await test();
    if (parsed.plain) {
      say(
        `${status.textContent} This address is plain http on another machine, so the token crosses your network unencrypted. Use https if you can.`,
        status.className,
      );
    }
  } finally {
    save.disabled = false;
  }
  return undefined;
});

forget.addEventListener("click", async () => {
  const before = saved?.origin ? parseAddress(saved.origin) : null;
  await chrome.storage.local.remove(CONNECTION);
  if (before?.ok) await chrome.permissions.remove({ origins: [before.pattern] }).catch(() => false);
  saved = null;
  address.value = "";
  token.value = "";
  draw();
  say("Disconnected. The token is gone from this browser; remove it in Foray's Settings too.");
});

saved = (await chrome.storage.local.get(CONNECTION))[CONNECTION] ?? null;
address.value = saved?.origin ?? "";
draw();
if (saved?.origin && saved?.token) await test();
