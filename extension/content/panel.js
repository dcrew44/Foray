/*
 * The small panel Foray shows on an application form. It lives in a closed shadow root, so the
 * page's styles cannot reach it and the page's scripts cannot read it or press its button: the
 * fill starts only from a real click (`isTrusted`), never from a click a script made up.
 *
 * `show(view)` draws one view:
 *   {title, company, tone, lines: [{text, kind?}], lists: [{heading, items}],
 *    link: {text, href}, action: {label, run, quiet}, busy, footnote}
 * `tone` colours the dot: ready (pine), waiting (marigold: something waits on the user),
 * done, error, or quiet. The colours are Foray's own tokens (src/app/globals.css), dark
 * first.
 */
(() => {
  const Foray = (globalThis.Foray ??= {});
  if (Foray.panel) return;

  const HOST_ID = "foray-fill-panel";
  const CSS_TEXT = `
:host { all: initial; }
* { box-sizing: border-box; }
.panel {
  --bg: #172019; --fg: #e3eae5; --muted: #92a199; --border: #25302a; --accent: #1d2a23;
  --primary: #5dbd91; --primary-fg: #0a1a12; --signal: #f0b84c; --signal-soft: #372b11;
  --signal-fg: #fff3d6; --destructive: #e1735e; --ring: #5dbd91;
  width: 320px; max-width: calc(100vw - 24px); max-height: min(72vh, 600px); overflow: auto;
  background: var(--bg); color: var(--fg); border: 1px solid var(--border);
  border-radius: 12px; box-shadow: 0 14px 36px -10px rgb(0 0 0 / 0.5);
  font: 13.5px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased; text-align: left; letter-spacing: normal;
}
@media (prefers-color-scheme: light) {
  .panel {
    --bg: #ffffff; --fg: #17221d; --muted: #5b6962; --border: #e0e5df; --accent: #ebf2ed;
    --primary: #1e5c45; --primary-fg: #f3faf6; --signal: #e2a01a; --signal-soft: #fcf1d8;
    --signal-fg: #4a3400; --destructive: #b8432f; --ring: #2f7a5c;
    box-shadow: 0 14px 36px -12px rgb(23 34 29 / 0.3);
  }
}
header { display: flex; align-items: center; gap: 8px; padding: 10px 8px 10px 14px; }
.brand { font-weight: 600; font-size: 13px; flex: 1; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
.dot.ready, .dot.done { background: var(--primary); }
.dot.waiting { background: var(--signal); }
.dot.error { background: var(--destructive); }
.icon {
  all: unset; width: 26px; height: 26px; border-radius: 6px; display: grid;
  place-items: center; color: var(--muted); cursor: pointer; font-size: 15px; line-height: 1;
}
.icon:hover { background: var(--accent); color: var(--fg); }
.icon:focus-visible, .action:focus-visible, a:focus-visible {
  outline: 2px solid var(--ring); outline-offset: 2px;
}
.body { padding: 0 14px 14px; display: grid; gap: 10px; }
.what { border-top: 1px solid var(--border); padding-top: 10px; }
.title { font-weight: 600; font-size: 14px; overflow-wrap: anywhere; }
.company { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
p { margin: 0; overflow-wrap: anywhere; }
p.muted, .footnote { color: var(--muted); font-size: 12.5px; }
p.strong { font-weight: 600; }
p.error { color: var(--destructive); }
p.signal {
  background: var(--signal-soft); color: var(--signal-fg); border-radius: 8px;
  padding: 8px 10px; font-weight: 600;
}
.list { display: grid; gap: 4px; }
.list h3 { margin: 0; font-size: 12.5px; font-weight: 600; color: var(--muted); }
ul { margin: 0; padding-left: 18px; display: grid; gap: 2px; font-size: 13px; }
a { color: var(--primary); text-underline-offset: 3px; }
.action {
  all: unset; box-sizing: border-box; display: block; width: 100%; text-align: center;
  background: var(--primary); color: var(--primary-fg); font-weight: 600; font-size: 13.5px;
  padding: 9px 12px; border-radius: 8px; cursor: pointer;
}
.action:hover { filter: brightness(1.06); }
.action.quiet {
  background: transparent; color: var(--fg); font-weight: 500;
  box-shadow: inset 0 0 0 1px var(--border);
}
.action.quiet:hover { background: var(--accent); filter: none; }
.action[disabled] { opacity: 0.6; cursor: default; }
.pill {
  all: unset; display: flex; align-items: center; gap: 8px; padding: 8px 12px;
  background: var(--bg); color: var(--fg); border: 1px solid var(--border);
  border-radius: 999px; font: 600 13px/1 system-ui, sans-serif; cursor: pointer;
  box-shadow: 0 8px 20px -8px rgb(0 0 0 / 0.45);
}
`;

  let host = null;
  let root = null;
  let view = null;
  let small = false;
  let dismissed = false;

  function h(tag, props = {}, ...children) {
    const el = document.createElement(tag);
    for (const [name, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (name === "class") el.className = value;
      else if (name === "text") el.textContent = value;
      else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
      else el.setAttribute(name, value === true ? "" : value);
    }
    el.append(...children.filter(Boolean));
    return el;
  }

  function mount() {
    if (host?.isConnected) return;
    host = document.createElement("div");
    host.id = HOST_ID;
    // Inline and important, so a page rule for every div cannot move or hide the panel.
    host.setAttribute(
      "style",
      "all: initial !important; position: fixed !important; top: 16px !important; right: 16px !important; z-index: 2147483647 !important; display: block !important;",
    );
    root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = CSS_TEXT;
    root.append(style, h("div", { class: "mount" }));
    document.documentElement.append(host);
  }

  /** Runs `fn` only for a click a person made. */
  const real = (fn) => (event) => {
    if (event.isTrusted) fn(event);
  };

  function draw() {
    if (!view || dismissed) return;
    mount();
    const slot = root.querySelector(".mount");
    const dot = h("span", { class: `dot ${view.tone ?? "quiet"}`, "aria-hidden": "true" });
    if (small) {
      slot.replaceChildren(
        h(
          "div",
          { class: "panel", style: "width: auto; border: 0; background: none; box-shadow: none;" },
          h(
            "button",
            {
              class: "pill",
              type: "button",
              "aria-label": "Show Foray",
              onclick: real(() => {
                small = false;
                draw();
              }),
            },
            dot,
            h("span", { text: "Foray" }),
          ),
        ),
      );
      return;
    }
    const body = h("div", { class: "body" });
    if (view.title || view.company) {
      body.append(
        h(
          "div",
          { class: "what" },
          view.title && h("div", { class: "title", text: view.title }),
          view.company && h("div", { class: "company", text: view.company }),
        ),
      );
    }
    for (const line of view.lines ?? []) {
      body.append(h("p", { class: line.kind ?? "", text: line.text }));
    }
    for (const list of view.lists ?? []) {
      if (!list.items.length) continue;
      const shown = list.items.slice(0, 6);
      const more = list.items.length - shown.length;
      body.append(
        h(
          "div",
          { class: "list" },
          h("h3", { text: list.heading }),
          h(
            "ul",
            {},
            ...shown.map((item) => h("li", { text: item })),
            more > 0 && h("li", { text: `and ${more} more` }),
          ),
        ),
      );
    }
    if (view.link) {
      body.append(
        h(
          "p",
          {},
          h("a", {
            href: view.link.href,
            target: "_blank",
            rel: "noopener noreferrer",
            text: view.link.text,
          }),
        ),
      );
    }
    if (view.action) {
      body.append(
        h("button", {
          class: view.action.quiet ? "action quiet" : "action",
          type: "button",
          disabled: view.busy,
          text: view.busy ? "Filling…" : view.action.label,
          onclick: real(() => {
            if (!view.busy) view.action.run();
          }),
        }),
      );
    }
    if (view.footnote) body.append(h("p", { class: "footnote", text: view.footnote }));
    slot.replaceChildren(
      h(
        "section",
        { class: "panel", "aria-label": "Foray" },
        h(
          "header",
          {},
          dot,
          h("span", { class: "brand", text: "Foray" }),
          h("button", {
            class: "icon",
            type: "button",
            "aria-label": "Make Foray smaller",
            text: "–",
            onclick: real(() => {
              small = true;
              draw();
            }),
          }),
          h("button", {
            class: "icon",
            type: "button",
            "aria-label": "Hide Foray on this page",
            text: "×",
            onclick: real(() => {
              dismissed = true;
              remove();
            }),
          }),
        ),
        h("div", { role: "status", "aria-live": "polite" }, body),
      ),
    );
  }

  /** Draws a view. `always` brings back a panel the user had hidden (they asked again). */
  function show(next, { always = false } = {}) {
    view = next;
    if (always) {
      dismissed = false;
      small = false;
    }
    draw();
  }

  function remove() {
    host?.remove();
    host = null;
    root = null;
  }

  /** Takes the panel off screen for a moment (a picture of the page should not show it). */
  function conceal(hidden) {
    if (host) host.style.setProperty("visibility", hidden ? "hidden" : "visible", "important");
  }

  /** The panel's words, for the tests: they read it from the extension's own world. */
  function words() {
    return root?.querySelector(".mount")?.textContent ?? "";
  }

  /** Where the main button is on screen, so a test can make a real click there. */
  function actionBox() {
    const button = root?.querySelector(".action");
    if (!button) return null;
    const { x, y, width, height } = button.getBoundingClientRect();
    return { x, y, width, height, disabled: button.disabled };
  }

  Foray.panel = { show, remove, conceal, words, actionBox, current: () => view };
})();
