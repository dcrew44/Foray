/*
 * The moving parts of the replica forms, hand-written for the tests. Nothing here is copied
 * from a job board: each piece is the smallest stand-in that behaves, to a script filling the
 * form, the way the real widget does.
 *
 * - `controlled(input, key)`: an input owned by a framework, as React owns one. The framework
 *   keeps its own record of the value; an `input` event that finds the record and the element
 *   agreeing is ignored, and the element is then drawn again from state. So a script that
 *   assigns `.value` and fires `input` changes nothing, exactly as on a React page.
 * - `combobox(...)`: a react-select look-alike (same class names, roles and ARIA wiring):
 *   opens on a mouse-down on its control, filters as you type, chooses on a click, closes on
 *   Escape or blur, and can search a slow list.
 * - `listCombobox(...)`: an autocomplete whose list is drawn at the end of <body>, away from
 *   the field, with no id tying the two together.
 * - `replica`: what the page would send. Tests read `replica.state` (not the DOM) to see what
 *   the "application" holds, `replica.submits` to see whether anything pressed submit, and
 *   `replica.heard` for everything the page's own scripts could observe.
 */
const replica = {
  state: {},
  files: {},
  submits: 0,
  submitted: null,
  heard: [],
};
window.replica = replica;

// Everything a page's own script can overhear, for the test that the token never gets here.
window.addEventListener("message", (event) => {
  replica.heard.push(typeof event.data === "string" ? event.data : JSON.stringify(event.data));
});

const nativeValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
const nativeArea = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");

function controlled(input, key) {
  const native = input instanceof HTMLTextAreaElement ? nativeArea : nativeValue;
  let tracked = "";
  replica.state[key] = "";
  // The framework's record: writes through the element's own `.value` update it.
  Object.defineProperty(input, "value", {
    configurable: true,
    get() {
      return native.get.call(this);
    },
    set(next) {
      tracked = String(next);
      native.set.call(this, next);
    },
  });
  input.addEventListener("input", () => {
    const actual = native.get.call(input);
    if (actual !== tracked) {
      tracked = actual;
      replica.state[key] = actual; // onChange
    }
    native.set.call(input, replica.state[key]); // draw again from state
  });
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === false || value === undefined || value === null) continue;
    if (name === "text") node.textContent = value;
    else node.setAttribute(name, value === true ? "" : value);
  }
  node.append(...children);
  return node;
}

/**
 * A react-select look-alike built into `mount`. `options` is a list of labels, or a function
 * of the typed text that resolves to one after a moment (a remote search).
 */
function combobox(mount, { id, label, required = false, options, placeholder = "Select..." }) {
  const remote = typeof options === "function";
  let shown = remote ? [] : options;
  let open = false;
  let focused = false;
  replica.state[id] = "";

  const input = el("input", {
    id,
    class: "select__input",
    type: "text",
    role: "combobox",
    autocomplete: "off",
    "aria-autocomplete": "list",
    "aria-expanded": "false",
    "aria-haspopup": "true",
    "aria-labelledby": `${id}-label`,
    "aria-required": String(required),
  });
  const value = el("div", { class: "select__placeholder", id: `react-select-${id}-placeholder`, text: placeholder });
  const control = el(
    "div",
    { class: "select__control" },
    el("div", { class: "select__value-container" }, value, el("div", { class: "select__input-container" }, input)),
    el(
      "div",
      { class: "select__indicators" },
      el("button", { type: "button", class: "icon-button", "aria-label": "Toggle flyout", tabindex: "-1" }),
    ),
  );
  const menu = el("div", { class: "select__menu", hidden: true });
  const list = el("div", { class: "select__menu-list", role: "listbox", id: `react-select-${id}-listbox` });
  menu.append(list);
  const shell = el("div", { class: "select-shell" }, control, menu);
  const container = el(
    "div",
    { class: "select__container" },
    el("label", { id: `${id}-label`, for: id, class: "label select__label", text: label }),
    shell,
  );
  if (required) {
    container.querySelector("label").append(el("span", { "aria-hidden": "true", text: "*" }));
    container.append(el("input", { class: "requiredInput", required: true, tabindex: "-1", "aria-hidden": "true" }));
  }
  mount.append(el("div", { class: "select" }, container));

  function draw() {
    menu.hidden = !open;
    input.setAttribute("aria-expanded", String(open));
    if (open) input.setAttribute("aria-controls", list.id);
    else input.removeAttribute("aria-controls");
    const typed = input.value.trim().toLowerCase();
    const visible = remote ? shown : shown.filter((option) => option.toLowerCase().includes(typed));
    list.replaceChildren(
      ...(visible.length
        ? visible.map((option) => {
            const item = el("div", { class: "select__option", role: "option", tabindex: "-1", text: option });
            item.addEventListener("click", () => choose(option));
            return item;
          })
        : [el("div", { class: "select__menu-notice", text: remote && !typed ? "Start typing" : "No options" })]),
    );
  }

  function choose(option) {
    replica.state[id] = option;
    // As react-select does: the hidden input that makes the browser insist goes once there
    // is a value.
    container.querySelector(".requiredInput")?.remove();
    value.className = "select__single-value";
    value.textContent = option;
    input.value = "";
    open = false;
    draw();
  }

  let search = 0;
  input.addEventListener("input", () => {
    open = true;
    if (remote) {
      const mine = (search += 1);
      const typed = input.value;
      setTimeout(async () => {
        const found = await options(typed);
        if (mine === search) {
          shown = found;
          draw();
        }
      }, 120);
    }
    draw();
  });
  input.addEventListener("focus", () => {
    focused = true;
  });
  input.addEventListener("blur", () => {
    focused = false;
    open = false;
    input.value = "";
    draw();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") open = false;
    if (event.key === "ArrowDown") open = true;
    draw();
  });
  // As react-select does: a press on the menu must not take the focus from the input, or
  // the blur would close the menu before the click lands.
  menu.addEventListener("mousedown", (event) => event.preventDefault());
  control.addEventListener("mousedown", (event) => {
    if (!focused) input.focus();
    open = !open || event.target === input;
    if (event.target !== input) event.preventDefault();
    draw();
  });
  return container;
}

/**
 * An autocomplete whose options are drawn in a list at the end of <body>. The input has no
 * id and nothing names the list from it: a script has to notice the list that opened.
 */
function listCombobox(mount, { key, options, search = false }) {
  replica.state[key] = "";
  const input = el("input", {
    class: "ashby-application-form-input-autocomplete",
    placeholder: "Start typing...",
    role: "combobox",
    "aria-autocomplete": "list",
    "aria-expanded": "false",
    "aria-haspopup": "listbox",
  });
  const toggle = el("button", { class: "_toggleButton_d7ago_32", type: "button", "aria-label": "Toggle" });
  mount.append(el("div", { class: "_inputContainer_d7ago_28" }, input, toggle));
  let list = null;

  function close() {
    list?.remove();
    list = null;
    input.setAttribute("aria-expanded", "false");
  }

  function show() {
    const typed = input.value.trim().toLowerCase();
    const found = search && !typed ? [] : options.filter((option) => option.toLowerCase().includes(typed));
    list?.remove();
    list = el("div", { role: "listbox", class: "_results_d7ago_41" });
    const box = input.getBoundingClientRect();
    list.style.cssText = `position:absolute;left:${box.left + scrollX}px;top:${box.bottom + scrollY}px;width:${box.width}px`;
    for (const option of found) {
      const item = el("div", { role: "option", text: option });
      // Chosen on mouse-down, before the input's blur can close the list.
      item.addEventListener("mousedown", (event) => {
        event.preventDefault();
        replica.state[key] = option;
        input.value = option;
        close();
      });
      list.append(item);
    }
    document.body.append(list);
    input.setAttribute("aria-expanded", "true");
  }

  input.addEventListener("click", show);
  input.addEventListener("input", show);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });
  input.addEventListener("blur", () => setTimeout(close, 50));
  toggle.addEventListener("click", () => (list ? close() : show()));
  return input;
}

/** Records a file control's file: its name, size, type and a digest of its bytes. */
function watchFile(input, key) {
  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file) return;
    const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    replica.files[key] = {
      name: file.name,
      size: file.size,
      type: file.type,
      sha256: [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join(""),
    };
  });
}

/** Counts a submit and keeps what the page would have sent. */
function submit(extra = {}) {
  replica.submits += 1;
  replica.submitted = { ...replica.state, ...extra };
  // Kept across the navigation to the confirming page, for the tests to read there.
  sessionStorage.setItem("replica", JSON.stringify({ submits: replica.submits, submitted: replica.submitted }));
}

export { combobox, controlled, el, listCombobox, replica, submit, watchFile };
