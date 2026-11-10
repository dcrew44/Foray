/*
 * One strategy per kind of control. Each puts one answer into one control and says what the
 * page then shows, or why it could not: `{ok: true, shown}` or `{ok: false, reason}`.
 * A strategy that cannot be sure the page took the answer says so (`unsure`), and the field is
 * highlighted for the user instead of counted as filled.
 */
(() => {
  const Foray = (globalThis.Foray ??= {});
  if (Foray.controls) return;
  const { text: T, dom: D } = Foray;

  const ok = (shown, extra = {}) => ({ ok: true, shown, ...extra });
  const no = (reason) => ({ ok: false, reason });
  const NO_OPTION = (value) => `No option on the page says “${value}”.`;

  // --- text ----------------------------------------------------------------------------------

  async function fillText(el, value) {
    D.focus(el);
    D.type(el, value);
    D.fire(el, "change");
    el.blur();
    await D.wait(0);
    if (!el.value) return no("The page did not keep what was typed.");
    return ok(el.value);
  }

  // --- <select> ------------------------------------------------------------------------------

  function selectOptions(el) {
    return [...el.options].filter((option) => !option.disabled && option.value !== "");
  }

  function fillSelect(el, value) {
    const options = selectOptions(el);
    let index = T.matchOption(
      value,
      options.map((option) => option.text),
    );
    if (index === -1) index = options.findIndex((option) => T.norm(option.value) === T.norm(value));
    if (index === -1) return no(NO_OPTION(value));
    const option = options[index];
    if (el.multiple) option.selected = true;
    else D.setValue(el, option.value);
    D.fire(el, "input");
    D.fire(el, "change");
    return option.selected ? ok(option.text.trim()) : no("The page did not take the choice.");
  }

  // --- radios and tick boxes, by their labels ------------------------------------------------

  /** What one radio or tick box is called. */
  function optionLabel(input) {
    const label = input.labels?.[0];
    return (
      D.text(label) ||
      input.getAttribute("aria-label") ||
      D.textOfIds(input.getAttribute("aria-labelledby")) ||
      D.text(input.nextElementSibling) ||
      input.value ||
      ""
    );
  }

  async function fillChoice(inputs, value) {
    const labels = inputs.map(optionLabel);
    let index = T.matchOption(value, labels);
    if (index === -1) index = inputs.findIndex((input) => T.norm(input.value) === T.norm(value));
    if (index === -1) return no(NO_OPTION(value));
    const input = inputs[index];
    // A click is what a framework listens for on these; it also sends `input` and `change`.
    if (!input.checked) input.click();
    await D.wait(0);
    return input.checked ? ok(labels[index]) : no("The page did not take the choice.");
  }

  // --- buttons that act as a choice (Yes / No) -----------------------------------------------

  const ON = /(^|[\s_-])(active|selected|checked|pressed)($|[\s_-])/i;

  function pressed(button) {
    return (
      ["aria-pressed", "aria-checked", "aria-selected"].some(
        (name) => button.getAttribute(name) === "true",
      ) ||
      ["on", "checked", "active"].includes(button.getAttribute("data-state") ?? "") ||
      ON.test(String(button.className))
    );
  }

  const buttonLabel = (button) =>
    T.cleanLabel(button.textContent) || button.getAttribute("aria-label") || "";

  async function fillButtons(buttons, value) {
    const labels = buttons.map(buttonLabel);
    const index = T.matchOption(value, labels);
    if (index === -1) return no(NO_OPTION(value));
    const button = buttons[index];
    if (!pressed(button)) D.press(button);
    const took = await D.until(() => pressed(button), 600);
    return ok(labels[index], took ? {} : { unsure: true });
  }

  // --- ARIA comboboxes (react-select and its relatives) --------------------------------------

  const expanded = (input) => input.getAttribute("aria-expanded") === "true";

  function listboxes() {
    return [...document.querySelectorAll('[role="listbox"]')].filter(D.visible);
  }

  /** The options an open combobox offers: its own listbox, else one that has just opened. */
  function optionsOf(input, container, before) {
    const named = (input.getAttribute("aria-controls") || input.getAttribute("aria-owns") || "")
      .split(/\s+/)
      .filter(Boolean)
      .map((id) => document.getElementById(id))
      .find(Boolean);
    const inside = container
      ? [...container.querySelectorAll('[role="listbox"]')].find(D.visible)
      : null;
    const scope = named ?? inside ?? listboxes().filter((list) => !before.has(list)).at(-1);
    let found = scope ? [...scope.querySelectorAll('[role="option"]')] : [];
    // Older react-select draws its options without roles.
    if (!found.length && container) {
      found = [...container.querySelectorAll('[class*="select__option"]')];
    }
    return found.filter(
      (option) => D.visible(option) && option.getAttribute("aria-disabled") !== "true",
    );
  }

  const optionText = (option) => D.text(option) || T.cleanLabel(option.textContent);

  async function open(input, container) {
    const before = new Set(listboxes());
    const isOpen = () => expanded(input) || optionsOf(input, container, before).length > 0;
    if (isOpen()) return before;
    D.focus(input);
    if (await D.until(isOpen, 150)) return before;
    // react-select opens on a mouse-down on its control, others on a click of the input.
    D.press(input.closest('[class*="control"]') ?? input);
    if (await D.until(isOpen, 400)) return before;
    D.key(input, "ArrowDown");
    if (await D.until(isOpen, 300)) return before;
    const toggle = container?.querySelector(
      'button[aria-haspopup], button[aria-label*="oggle"], button[class*="oggle"]',
    );
    if (toggle) {
      D.press(toggle);
      await D.until(isOpen, 400);
    }
    return before;
  }

  async function close(input, typed) {
    if (typed) D.type(input, "");
    if (expanded(input)) D.key(input, "Escape");
    input.blur();
    await D.wait(0);
  }

  /** What a combobox shows as chosen. */
  function comboboxValue(input, container) {
    const chosen = container?.querySelector(
      '[class*="single-value"], [class*="singleValue"], [class*="multi-value"], [class*="multiValue"]',
    );
    return T.cleanLabel(chosen?.textContent) || input.value || "";
  }

  /**
   * Opens the list, finds the option that says `value`, and chooses it. A list that shows
   * nothing until something is typed (a long list, a location search) gets the value typed
   * into it once. When no option says the value the list is closed again and nothing stays
   * typed: the field is left as it was found.
   */
  async function fillCombobox(input, container, value, { place = false } = {}) {
    const before = await open(input, container);
    let options = optionsOf(input, container, before);
    const find = () => {
      const labels = options.map(optionText);
      const at = place ? T.matchLocation(value, labels) : -1;
      return at !== -1 ? at : T.matchOption(value, labels);
    };
    let index = find();
    let typed = false;
    if (index === -1 && !input.readOnly) {
      typed = true;
      D.type(input, place ? value.split(",")[0].trim() : value);
      await D.until(
        () => {
          options = optionsOf(input, container, before);
          index = find();
          return index !== -1;
        },
        place ? 4000 : 1200,
        100,
      );
    }
    if (index === -1) {
      await close(input, typed);
      return no(
        place
          ? `No place the page suggests matches “${value}”. Choose yours from its list.`
          : NO_OPTION(value),
      );
    }
    const option = options[index];
    const label = optionText(option);
    option.scrollIntoView?.({ block: "nearest" });
    D.press(option);
    const shows = () => T.norm(comboboxValue(input, container)).includes(T.norm(label));
    const took = await D.until(shows, 800);
    if (expanded(input)) D.key(input, "Escape");
    input.blur();
    return ok(label, took ? {} : { unsure: true });
  }

  /** The options of a combobox, read by opening it and closing it again. */
  async function comboboxOptions(input, container) {
    if (comboboxValue(input, container)) return [];
    const before = await open(input, container);
    const found = await D.until(() => {
      const options = optionsOf(input, container, before);
      return options.length ? options : null;
    }, 500);
    const labels = (found ?? []).map(optionText).filter(Boolean).slice(0, 200);
    await close(input, false);
    return labels;
  }

  // --- files ---------------------------------------------------------------------------------

  function takesPdf(input) {
    const accept = (input.getAttribute("accept") || "").toLowerCase();
    if (!accept.trim()) return true;
    return accept
      .split(",")
      .map((item) => item.trim())
      .some((item) => [".pdf", "application/pdf", "application/*", "*/*"].includes(item));
  }

  function fillFile(input, file) {
    if (!takesPdf(input)) return no("This upload does not take a PDF.");
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
    D.fire(input, "input");
    D.fire(input, "change");
    return input.files?.[0]?.name === file.name
      ? ok(file.name)
      : no("The page did not take the file.");
  }

  Foray.controls = {
    fillText,
    fillSelect,
    selectOptions,
    fillChoice,
    optionLabel,
    fillButtons,
    buttonLabel,
    pressed,
    fillCombobox,
    comboboxOptions,
    comboboxValue,
    fillFile,
  };
})();
