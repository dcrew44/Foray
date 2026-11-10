/*
 * The page, as the fill engine touches it: what is visible, what an element says, how a value
 * is set so that a framework notices, and how a filled field is marked for the user.
 *
 * Nothing here hides what it does. Values are set at once and events are plain synthetic
 * events (`isTrusted` is false, as it is for any script): no typing delays, no pointer paths.
 */
(() => {
  const Foray = (globalThis.Foray ??= {});
  if (Foray.dom) return;
  const T = Foray.text;

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /** Polls `test` until it answers something truthy, or gives up with null. */
  async function until(test, timeout = 1500, step = 50) {
    const end = Date.now() + timeout;
    for (;;) {
      const value = test();
      if (value) return value;
      if (Date.now() >= end) return null;
      await wait(step);
    }
  }

  /** In the layout at all (no `display: none` on it or above it). */
  function rendered(el) {
    return !!el && el.getClientRects().length > 0;
  }

  /** Something a person can see: laid out, not hidden, not transparent, not a 1px helper. */
  function visible(el) {
    if (!rendered(el)) return false;
    const box = el.getBoundingClientRect();
    if (box.width < 2 || box.height < 2) return false;
    if (getComputedStyle(el).visibility !== "visible") return false;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      if (Number(getComputedStyle(node).opacity) === 0) return false;
    }
    return true;
  }

  const NOT_WORDS =
    'script, style, svg, [aria-hidden="true"], .required, .visually-hidden, .sr-only, input, select, textarea, button';

  /** What an element says to a reader, on one line, without controls or required marks. */
  function text(el) {
    if (!el) return "";
    const clone = el.cloneNode(true);
    for (const junk of clone.querySelectorAll(NOT_WORDS)) junk.remove();
    return T.cleanLabel(clone.textContent);
  }

  /** The words of the elements a list of ids names (`aria-labelledby`). */
  function textOfIds(ids) {
    return String(ids ?? "")
      .split(/\s+/)
      .filter(Boolean)
      .map((id) => text(document.getElementById(id)))
      .filter(Boolean)
      .join(" ");
  }

  function fire(el, type) {
    el.dispatchEvent(new Event(type, { bubbles: true, cancelable: true }));
  }

  /**
   * Focus that a framework hears. While the window itself is not focused (the toolbar popup
   * is open, say) `focus()` moves the caret but sends no event, and a widget that opens on
   * focus stays shut.
   */
  function focus(el) {
    el.focus({ preventScroll: true });
    if (!document.hasFocus()) {
      el.dispatchEvent(new FocusEvent("focus"));
      el.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    }
  }

  /** The events one click is made of, in order. Widgets differ in which they listen for. */
  function press(el) {
    const at = { bubbles: true, cancelable: true, composed: true, view: window, button: 0 };
    const pointer = { ...at, pointerId: 1, pointerType: "mouse", isPrimary: true };
    el.dispatchEvent(new PointerEvent("pointerdown", { ...pointer, buttons: 1 }));
    el.dispatchEvent(new MouseEvent("mousedown", { ...at, buttons: 1 }));
    el.dispatchEvent(new PointerEvent("pointerup", pointer));
    el.dispatchEvent(new MouseEvent("mouseup", at));
    if (typeof el.click === "function") el.click();
    else el.dispatchEvent(new MouseEvent("click", at));
  }

  const KEY_CODES = { ArrowDown: 40, Escape: 27, Enter: 13 };

  function key(el, name) {
    const init = {
      key: name,
      code: name,
      keyCode: KEY_CODES[name],
      which: KEY_CODES[name],
      bubbles: true,
      cancelable: true,
      composed: true,
    };
    el.dispatchEvent(new KeyboardEvent("keydown", init));
    el.dispatchEvent(new KeyboardEvent("keyup", init));
  }

  /**
   * Sets a control's value through the element's own setter. React keeps its own record of
   * an input's value and ignores an `input` event when the two agree, so the value has to
   * change underneath that record for the event that follows to count as a change.
   */
  function setValue(el, value) {
    const proto =
      el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : el instanceof HTMLSelectElement
          ? HTMLSelectElement.prototype
          : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
  }

  /** Types `value` into a control at once and tells the page it changed. */
  function type(el, value) {
    setValue(el, value);
    el.dispatchEvent(
      new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }),
    );
  }

  // --- marking what was filled -------------------------------------------------------------

  const MARK = "data-foray-fill";
  const STYLE_ID = "foray-fill-marks";
  const STYLE = `
[${MARK}="filled"] { outline: 2px solid #2f7a5c !important; outline-offset: 2px !important; }
[${MARK}="check"] { outline: 2px dashed #e2a01a !important; outline-offset: 2px !important; }
`;

  /** The nearest thing a person can see for a control that is itself hidden. */
  function seen(el) {
    let node = el;
    for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
      if (visible(node) && node.getBoundingClientRect().height >= 12) return node;
    }
    return el;
  }

  /** Outlines a control: `filled` in green, `check` dashed in marigold. In plain sight. */
  function mark(el, state) {
    if (!el) return;
    if (!document.getElementById(STYLE_ID)) {
      const style = document.createElement("style");
      style.id = STYLE_ID;
      style.textContent = STYLE;
      (document.head ?? document.documentElement).append(style);
    }
    seen(el).setAttribute(MARK, state);
  }

  function clearMarks() {
    for (const el of document.querySelectorAll(`[${MARK}]`)) el.removeAttribute(MARK);
  }

  Foray.dom = {
    wait,
    until,
    rendered,
    visible,
    text,
    textOfIds,
    fire,
    focus,
    press,
    key,
    setValue,
    type,
    mark,
    clearMarks,
    seen,
  };
})();
