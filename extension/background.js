/*
 * The service worker: the only part of the extension that holds the token and talks to Foray.
 *
 * Content scripts run inside employers' pages, so they are told as little as possible: they
 * ask by message for an application to fill and report what they did, and what comes back is
 * the application and nothing about the connection. Messages are checked for who sent them:
 * a page's script may only ask for the few things a fill needs, and only the extension's own
 * pages (the popup, the options page) may ask about the connection or start a fill.
 *
 * What a tab is in the middle of is kept in `chrome.storage.session` (memory only, gone when
 * the browser closes), because a worker is stopped whenever it is idle:
 *   tab:<id>       {id, origin, company, title, filled, submitSeen, auto, at}: a form filled
 *                  in that tab, waiting for the board's page to confirm it; `filled` is what
 *                  the form held when it was last heard from
 *   invoked:<id>   the origin a tab was on when the user clicked Foray in the toolbar, which
 *                  is when Chrome lets the extension take a picture of that tab
 *   reported:<id>  an application whose confirmation has been sent to Foray
 */
import { connection, request } from "./lib/api.js";

const CONTENT_FILES = [
  "content/text.js",
  "content/dom.js",
  "content/controls.js",
  "content/reader.js",
  "content/boards.js",
  "content/handoff.js",
  "content/panel.js",
  "content/fill.js",
  "content/main.js",
];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PENDING_FOR_MS = 12 * 60 * 60 * 1000;
// Foray keeps a picture of at most 3 MB; base64 makes four characters of every three bytes.
const PICTURE_MAX_CHARS = 4_000_000;
const HOME = chrome.runtime.getURL("");

// The token is for this worker and the options page only: content scripts cannot read
// chrome.storage at all.
chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });

chrome.runtime.onInstalled.addListener(({ reason }) => {
  if (reason === "install") chrome.runtime.openOptionsPage();
});

chrome.tabs.onRemoved.addListener((tabId) => {
  chrome.storage.session.remove([`tab:${tabId}`, `invoked:${tabId}`]);
});

// --- what a tab is in the middle of ---------------------------------------------------------

const session = {
  async get(key) {
    return (await chrome.storage.session.get(key))[key] ?? null;
  },
  set(key, value) {
    return chrome.storage.session.set({ [key]: value });
  },
  remove(key) {
    return chrome.storage.session.remove(key);
  },
};

function originOf(url) {
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}

/** The fill a tab is waiting on, when the page asking is still on the site it was done on. */
async function pendingFor(sender) {
  const tabId = sender.tab?.id;
  if (tabId === undefined) return null;
  const pending = await session.get(`tab:${tabId}`);
  if (!pending) return null;
  if (pending.origin !== originOf(sender.url) || Date.now() - pending.at > PENDING_FOR_MS) {
    return null;
  }
  return pending;
}

// Two messages about one tab must not read the record, then both write it: the second
// would undo the first. Changes wait their turn.
let changes = Promise.resolve();

/** Applies `change` to the fill a tab is waiting on. Answers the new record, or null. */
function changePending(sender, change) {
  const run = changes.then(async () => {
    const pending = await pendingFor(sender);
    if (!pending) return null;
    const next = change(pending);
    if (next) await session.set(`tab:${sender.tab.id}`, next);
    return next;
  });
  changes = run.catch(() => null);
  return run;
}

/** Whether Chrome will let the extension take a picture of this tab (see `invoked`). */
async function canPicture(sender) {
  const tabId = sender.tab?.id;
  if (tabId === undefined) return false;
  const invoked = await session.get(`invoked:${tabId}`);
  return !!invoked && invoked === originOf(sender.tab?.url ?? sender.url);
}

// --- proof of arrival -----------------------------------------------------------------------

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * A picture of the tab the confirmation is in: PNG, or JPEG when the PNG is more than Foray
 * keeps. "" when Chrome does not allow it (the user never clicked Foray in the toolbar on
 * that tab) or the tab is not the one in front: a picture of some other tab is worse than
 * none.
 */
async function picture(tab) {
  try {
    const now = await chrome.tabs.get(tab.id);
    if (!now.active) return "";
    let image = await chrome.tabs.captureVisibleTab(now.windowId, { format: "png" });
    for (const quality of [80, 50]) {
      if (image.length <= PICTURE_MAX_CHARS) break;
      await wait(600); // Chrome allows two captures a second
      image = await chrome.tabs.captureVisibleTab(now.windowId, { format: "jpeg", quality });
    }
    return image.length <= PICTURE_MAX_CHARS ? image : "";
  } catch {
    return "";
  }
}

const sending = new Set();

async function confirmed(message, sender) {
  const pending = await pendingFor(sender);
  if (!pending || pending.id !== message.id || !pending.submitSeen) {
    return { ok: false, ignored: true };
  }
  const key = `reported:${pending.id}`;
  const done = { ok: true, title: pending.title, company: pending.company };
  if (sending.has(pending.id)) return { ok: false, ignored: true };
  if (await session.get(key)) return { ...done, picture: false, already: true };
  sending.add(pending.id);
  try {
    // Marked before the request, so a second report cannot start while the first is out.
    await session.set(key, true);
    const screenshot = sender.tab ? await picture(sender.tab) : "";
    const answer = await request(`/api/extension/applications/${pending.id}/submitted`, {
      method: "POST",
      timeout: 45000,
      body: {
        url: String(message.url ?? "").slice(0, 4000),
        text: String(message.text ?? "").slice(0, 20000),
        screenshot,
        filled: pending.filled,
        // Only when the extension pressed submit itself; Foray decides whether to believe it.
        ...(pending.auto ? { auto: true } : {}),
      },
    });
    if (!answer.ok) {
      await session.remove(key);
      return answer;
    }
    await session.remove(`tab:${sender.tab.id}`);
    return { ...done, picture: !!screenshot };
  } finally {
    sending.delete(pending.id);
  }
}

// --- what a content script may ask ----------------------------------------------------------

const cleanFilled = (filled) =>
  (Array.isArray(filled) ? filled : []).slice(0, 300).map((item) => ({
    key: String(item?.key ?? "").slice(0, 200),
    label: String(item?.label ?? "").slice(0, 500),
    value: String(item?.value ?? "").slice(0, 4000),
  }));

const application = (answer) =>
  answer.ok ? { ok: true, application: answer.data } : answer;

const fromPage = {
  async find(message, sender) {
    const saved = await connection();
    if (!saved) return { configured: false };
    const pending = await pendingFor(sender);
    const found = await request(
      `/api/extension/applications?url=${encodeURIComponent(String(message.url ?? ""))}`,
    );
    return {
      configured: true,
      forayUrl: saved.origin,
      pending: pending && {
        id: pending.id,
        at: pending.at,
        submitSeen: pending.submitSeen,
        title: pending.title,
        company: pending.company,
      },
      ...(found.ok ? { application: found.data.application } : { error: found.error }),
    };
  },

  async get(message) {
    return application(await request(`/api/extension/applications/${message.id}`));
  },

  async plan(message) {
    const fields = (Array.isArray(message.fields) ? message.fields : []).slice(0, 200);
    return application(
      await request(`/api/extension/applications/${message.id}/plan`, {
        method: "POST",
        body: { fields },
      }),
    );
  },

  async resume(message) {
    const [answer, about] = await Promise.all([
      request(`/api/extension/applications/${message.id}/resume`, { binary: true, timeout: 45000 }),
      request(`/api/extension/applications/${message.id}`),
    ]);
    if (!answer.ok) return answer;
    // Messages carry JSON, so the file crosses as base64.
    let binary = "";
    const { bytes } = answer.data;
    for (let at = 0; at < bytes.length; at += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(at, at + 0x8000));
    }
    return {
      ok: true,
      base64: btoa(binary),
      mime: answer.data.type,
      filename:
        (about.ok && about.data.resume?.filename) || answer.data.filename || "resume.pdf",
    };
  },

  async filled(message, sender) {
    const filled = cleanFilled(message.filled);
    const answer = await request(`/api/extension/applications/${message.id}/filled`, {
      method: "POST",
      body: { filled },
    });
    if (!answer.ok) return answer;
    if (sender.tab?.id !== undefined) {
      const fresh = {
        id: message.id,
        origin: originOf(sender.url),
        title: String(message.title ?? "").slice(0, 300),
        company: String(message.company ?? "").slice(0, 300),
        filled,
        submitSeen: false,
        auto: false,
        at: Date.now(),
      };
      const write = changes.then(async () => {
        await session.remove(`reported:${message.id}`);
        await session.set(`tab:${sender.tab.id}`, fresh);
      });
      changes = write.catch(() => null);
      await write;
    }
    return { ok: true, picture: await canPicture(sender) };
  },

  /** What the form holds now, after the user changed something in it. */
  async held(message, sender) {
    const held = cleanFilled(message.filled);
    const next = await changePending(sender, (pending) =>
      pending.id === message.id ? { ...pending, filled: held.length ? held : pending.filled } : null,
    );
    return { ok: !!next };
  },

  async "submit-seen"(message, sender) {
    const held = cleanFilled(message.filled);
    const next = await changePending(sender, (pending) =>
      pending.id === message.id
        ? {
            ...pending,
            submitSeen: true,
            auto: message.auto === true,
            // What the form held when submit was pressed, when the page could still be read.
            filled: held.length ? held : pending.filled,
          }
        : null,
    );
    return { ok: !!next };
  },

  confirmed,
};

// --- what the extension's own pages may ask -------------------------------------------------

async function me() {
  const answer = await request("/api/extension/me");
  return answer.ok
    ? { ok: true, name: answer.data.name, waiting: answer.data.waiting }
    : answer;
}

/** Tells the content script in a tab to fill, putting it there first when it is not. */
async function fillTab(tabId) {
  const ask = (message, options) =>
    chrome.tabs.sendMessage(tabId, message, options).catch(() => null);
  if (!(await ask({ type: "ping" }, { frameId: 0 }))) {
    try {
      // Allowed by `activeTab`: the user has just clicked Foray on this tab.
      await chrome.scripting.executeScript({ target: { tabId }, files: CONTENT_FILES });
    } catch {
      return { ok: false, message: "Foray cannot work on this page." };
    }
  }
  const taken = await ask({ type: "fill-now" });
  if (taken?.ok) return { ok: true };
  return { ok: false, message: "Foray could not reach this page. Reload it and try again." };
}

const fromExtension = {
  /** The popup opened on a tab: the connection, and (for later) that the tab was invoked. */
  async "popup-state"(message) {
    const saved = await connection();
    const tab = Number.isInteger(message.tabId) ? await chrome.tabs.get(message.tabId) : null;
    if (tab?.url) await session.set(`invoked:${tab.id}`, originOf(tab.url));
    if (!saved) return { configured: false };
    return { configured: true, forayUrl: saved.origin, me: await me() };
  },

  async "popup-fill"(message) {
    if (!Number.isInteger(message.tabId)) return { ok: false, message: "No tab to fill." };
    return fillTab(message.tabId);
  },

  async "test-connection"() {
    return me();
  },
};

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || typeof message?.type !== "string") return undefined;
  const own = !!sender.url?.startsWith(HOME);
  const offered = own ? fromExtension : fromPage;
  const handler = Object.hasOwn(offered, message.type) ? offered[message.type] : null;
  if (!handler) return undefined;
  // An id from a page goes into a path: it must be an application's id and nothing else.
  if (!own && message.type !== "find" && !UUID.test(String(message.id ?? ""))) {
    respond({ ok: false });
    return undefined;
  }
  Promise.resolve(handler(message, sender)).then(respond, () =>
    respond({ ok: false, error: { kind: "worker", message: "The extension hit an error." } }),
  );
  return true; // the answer comes later
});
