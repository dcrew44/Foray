/*
 * Reads a form from the page: every question a person could answer, with its label, its kind
 * of control, its options and whether it is required. Reading changes nothing on the page.
 *
 * A board's adapter may say where one question's markup begins and ends (`hooks.block`) and
 * what it is called (`hooks.label`, `hooks.key`). Without those the label is found the way a
 * screen reader finds it: `aria-labelledby`, a `<label>`, a fieldset's legend, `aria-label`,
 * and last the words just before the control.
 *
 * A page field is
 *   {key, label, type, required, options, kind, el, els, container, consent}
 * where `type` is the server's vocabulary (text, textarea, select, multiselect, file) and
 * `kind` is how the control is worked: text, textarea, select, radio, checkbox, buttons,
 * combobox or file.
 */
(() => {
  const Foray = (globalThis.Foray ??= {});
  if (Foray.reader) return;
  const { text: T, dom: D, controls: C } = Foray;

  const NEVER = new Set([
    "hidden", "submit", "button", "reset", "image", "password", "search", "range", "color",
  ]);
  const CAPTCHA = /captcha|turnstile/i;
  const UPLOAD_WORDS = /^(attach|upload|browse|choose|select)( a| your)?( file| resume| cv)?$/i;
  const PRIORITY = ["file", "buttons", "combobox", "select", "textarea", "radio", "checkbox", "text"];
  const TYPE = {
    text: "text",
    textarea: "textarea",
    select: "select",
    radio: "select",
    buttons: "select",
    combobox: "select",
    checkbox: "multiselect",
    file: "file",
  };

  function kindOf(el) {
    if (el.tagName === "TEXTAREA") return "textarea";
    if (el.tagName === "SELECT") return "select";
    if (el.tagName !== "INPUT") return null;
    const type = (el.getAttribute("type") || "text").toLowerCase();
    if (type === "file") return "file";
    if (type === "radio" || type === "checkbox") return type;
    if (NEVER.has(type)) return null;
    if (el.getAttribute("role") === "combobox" || el.getAttribute("aria-haspopup") === "listbox") {
      return "combobox";
    }
    return "text";
  }

  /** Whether a person could answer this control. A field nobody can see is not filled. */
  function usable(el, kind) {
    if (el.disabled || el.getAttribute("aria-hidden") === "true") return false;
    if (CAPTCHA.test(`${el.name} ${el.id}`)) return false;
    // Uploads, radios and tick boxes are routinely hidden behind a drawn control.
    if (kind === "file") return D.rendered(el.parentElement);
    if (kind === "radio" || kind === "checkbox") {
      return D.rendered(el) || D.rendered(el.labels?.[0]) || D.rendered(el.parentElement);
    }
    if (kind === "select") return D.rendered(el) || D.rendered(el.parentElement);
    if (kind === "combobox") return D.visible(el);
    return D.visible(el) && !el.readOnly;
  }

  /** The buttons in `scope` that act as one choice: toggles, or ARIA radios. */
  function choiceButtons(scope) {
    const buttons = [
      ...scope.querySelectorAll('button[aria-pressed], [role="radio"]:not(input)'),
    ].filter(D.visible);
    return buttons.length >= 2 ? buttons : [];
  }

  // --- labels --------------------------------------------------------------------------------

  /** The words just before `members` inside the nearest ancestor that has any. */
  function leadingText(members, root) {
    const own = new Set(members);
    let node = members[0];
    for (let depth = 0; depth < 6 && node && node !== root; depth += 1) {
      const parent = node.parentElement;
      if (!parent) break;
      let words = [];
      for (const child of parent.childNodes) {
        if (child === node) break;
        if (child.nodeType === 3) {
          words.push(child.textContent);
        } else if (child.nodeType === 1) {
          const others = [...child.querySelectorAll("input, textarea, select")].filter(
            (el) => !own.has(el) && kindOf(el),
          );
          // Another question's control: its words are not ours.
          if (others.length || (kindOf(child) && !own.has(child))) words = [];
          else words.push(D.text(child));
        }
      }
      const raw = words.join(" ");
      const said = T.cleanLabel(raw);
      if (said) return { text: said.slice(0, 300), raw, el: null };
      node = parent;
    }
    return { text: "", raw: "", el: null };
  }

  function groupLabel(el) {
    const group = el.closest('fieldset, [role="group"], [role="radiogroup"]');
    if (!group) return { text: "", raw: "", el: null };
    const legend = group.querySelector(":scope > legend");
    const named = String(group.getAttribute("aria-labelledby") ?? "")
      .split(/\s+/)
      .filter(Boolean)
      .map((id) => document.getElementById(id))
      .find(Boolean);
    const source = named ?? legend;
    const raw = source?.textContent ?? group.getAttribute("aria-label") ?? "";
    const said = D.text(source) || group.getAttribute("aria-label") || "";
    return { text: T.cleanLabel(said), raw, el: source ?? null };
  }

  function ownLabel(el) {
    const label = el.labels?.[0] ?? null;
    const said =
      D.textOfIds(el.getAttribute("aria-labelledby")) ||
      D.text(label) ||
      el.getAttribute("aria-label") ||
      "";
    return { text: T.cleanLabel(said), raw: label?.textContent ?? said, el: label };
  }

  function questionLabel(el, kind, members, root) {
    const grouped = kind === "radio" || kind === "checkbox" || kind === "buttons";
    if (grouped && members.length > 1) {
      const group = groupLabel(el);
      return group.text ? group : leadingText(members, root);
    }
    const own = ownLabel(el);
    // "Attach" names the button, not the question.
    if (own.text && !(kind === "file" && UPLOAD_WORDS.test(own.text))) {
      // A lone tick box is labelled by its option; the question, if any, comes before it.
      if (kind !== "checkbox") return own;
    }
    const group = groupLabel(el);
    if (group.text) return group;
    const leading = leadingText(members, root);
    if (leading.text) return leading;
    if (own.text) return own;
    const hint = el.getAttribute("placeholder") || el.getAttribute("title") || "";
    const said = T.cleanLabel(hint || (el.name || "").replace(/[_[\]-]+/g, " "));
    return { text: said, raw: said, el: null };
  }

  // --- groups --------------------------------------------------------------------------------

  /** The radios or tick boxes that are one question with `el`. */
  function groupOf(el, kind, root) {
    const same = (other) => kindOf(other) === kind;
    if (kind === "radio" && el.name) {
      return [...root.querySelectorAll(`input[type="radio"]`)].filter(
        (other) => other.name === el.name && other.form === el.form,
      );
    }
    const group = el.closest('fieldset, [role="group"], [role="radiogroup"]');
    if (group && root.contains(group)) {
      const inside = [...group.querySelectorAll("input")].filter(same);
      if (inside.length > 1) return inside;
    }
    if (el.name) {
      const named = [...root.querySelectorAll("input")].filter(
        (other) => same(other) && other.name === el.name,
      );
      if (named.length > 1) return named;
    }
    return [el];
  }

  function required(members, label, block, hooks) {
    if (block && hooks.required?.(block)) return true;
    if (members.some((el) => el.required || el.getAttribute("aria-required") === "true")) {
      return true;
    }
    const group = members[0].closest('[role="group"], [role="radiogroup"]');
    if (group?.getAttribute("aria-required") === "true") return true;
    return (
      T.saysRequired(label.raw) || !!label.el?.querySelector('.required, [class*="required"]')
    );
  }

  function optionsOf(kind, members) {
    if (kind === "select") return C.selectOptions(members[0]).map((o) => T.cleanLabel(o.text));
    if (kind === "radio" || kind === "checkbox") return members.map(C.optionLabel);
    if (kind === "buttons") return members.map(C.buttonLabel);
    return [];
  }

  // --- reading -------------------------------------------------------------------------------

  /** Every question in `root`, in the page's order. */
  function read(root, hooks = {}) {
    const fields = [];
    const taken = new Set();
    const blocks = new Set();
    const keys = new Map();

    function add(kind, members, block) {
      const el = members[0];
      for (const member of members) taken.add(member);
      const hooked = block ? T.cleanLabel(hooks.label?.(block, el) ?? "") : "";
      const found = hooked
        ? { text: hooked, raw: hooked, el: null }
        : questionLabel(el, kind, members, root);
      const options = optionsOf(kind, members);
      const label = found.text || options[0] || "";
      if (!label) return;
      let key =
        (block && hooks.key?.(block, el)) ||
        (kind === "radio" || (kind === "checkbox" && members.length > 1)
          ? members.every((member) => member.name === el.name) && el.name
          : el.id || el.name) ||
        `label:${T.slug(label)}`;
      const count = (keys.get(key) ?? 0) + 1;
      keys.set(key, count);
      if (count > 1) key = `${key}#${count}`;
      const multiple = kind === "select" && el.multiple;
      fields.push({
        key: String(key).slice(0, 200),
        label,
        type: multiple ? "multiselect" : TYPE[kind],
        required: required(members, found, block, hooks),
        options,
        kind,
        el,
        els: members,
        container:
          block ??
          el.closest('fieldset, [role="group"], [role="radiogroup"], [class*="select__container"]') ??
          el.labels?.[0]?.parentElement ??
          el.parentElement,
        consent: kind === "checkbox" && T.isConsent({ key: String(key), label, options }),
      });
    }

    function readBlock(block) {
      if (blocks.has(block)) return;
      blocks.add(block);
      const inside = [...block.querySelectorAll("input, textarea, select")]
        .map((el) => ({ el, kind: kindOf(el) }))
        .filter(({ el, kind }) => kind && !hooks.skip?.(el) && usable(el, kind));
      const buttons = choiceButtons(block);
      for (const { el } of inside) taken.add(el);
      const kind = PRIORITY.find(
        (name) => (name === "buttons" ? buttons.length : inside.some((item) => item.kind === name)),
      );
      if (!kind) return;
      const members =
        kind === "buttons"
          ? buttons
          : inside.filter((item) => item.kind === kind).map((item) => item.el);
      const group = kind === "radio" || kind === "checkbox" || kind === "buttons";
      add(kind, group ? members : [members[0]], block);
    }

    // Toggle buttons first, so the hidden input that mirrors them is not read as a field.
    if (!hooks.block) {
      const groups = new Map();
      for (const button of choiceButtons(root)) {
        const holder = button.closest('[role="radiogroup"]') ?? button.parentElement;
        groups.set(holder, [...(groups.get(holder) ?? []), button]);
      }
      for (const [holder, buttons] of groups) {
        if (buttons.length < 2) continue;
        for (const input of holder.querySelectorAll("input")) taken.add(input);
        add("buttons", buttons, null);
      }
    }

    for (const el of root.querySelectorAll("input, textarea, select")) {
      if (taken.has(el)) continue;
      const kind = kindOf(el);
      if (!kind || hooks.skip?.(el) || !usable(el, kind)) continue;
      const block = hooks.block?.(el) ?? null;
      if (block) {
        readBlock(block);
        continue;
      }
      const members =
        kind === "radio" || kind === "checkbox"
          ? groupOf(el, kind, root).filter((member) => usable(member, kind))
          : [el];
      add(kind, members.length ? members : [el], null);
    }

    // Keep the page's order: the toggle groups were read ahead of everything else.
    const order = (a, b) =>
      a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
    return fields.sort(order);
  }

  /** What a field holds now, as a person would read it; "" when it is empty. */
  function valueOf(field) {
    const { kind, el, els, container } = field;
    if (kind === "text" || kind === "textarea") return el.value.trim();
    if (kind === "select") {
      return [...el.selectedOptions]
        .filter((option) => option.value !== "")
        .map((option) => option.text.trim())
        .join(", ");
    }
    if (kind === "radio" || kind === "checkbox") {
      return els.filter((input) => input.checked).map(C.optionLabel).join(", ");
    }
    if (kind === "buttons") return els.filter(C.pressed).map(C.buttonLabel).join(", ");
    if (kind === "combobox") return C.comboboxValue(el, container);
    if (kind === "file") return el.files?.[0]?.name ?? "";
    return "";
  }

  /** The fields as the server plans them: no elements, and no agreements. */
  function forPlan(fields) {
    return fields
      .filter((field) => !field.consent)
      .slice(0, 200)
      .map(({ key, label, type, required: needed, options }) => ({
        key,
        label: label.slice(0, 500),
        type,
        required: needed,
        options: options.slice(0, 200).map((option) => option.slice(0, 300)),
      }));
  }

  Foray.reader = { read, valueOf, forPlan, kindOf, usable };
})();
