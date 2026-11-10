/*
 * The fill: every answer Foray planned goes into the control the board's adapter finds for
 * it, through that control's strategy. The rules it keeps:
 *
 * - Only the answers given. A field whose value is null is left exactly as it was found,
 *   and nothing is made up for a control Foray has no answer for.
 * - An agreement (terms, privacy, marketing) is never ticked, whatever the plan says.
 * - A file control takes the resume and nothing else; the resume goes nowhere but a file
 *   control.
 * - A field that could not be filled, or that the page may not have taken, is outlined for
 *   the user and not counted.
 *
 * `run` answers one outcome per planned answer:
 *   {key, label, status: "filled" | "check" | "unfilled" | "consent", shown, reason, page}
 */
(() => {
  const Foray = (globalThis.Foray ??= {});
  if (Foray.fill) return;
  const { dom: D, controls: C, reader: R } = Foray;

  const NOT_FOUND = "Foray could not find this question on the page.";
  const PLACE = /\b(location|city|where (are|do) you (based|located|live))\b/i;

  const isPlace = (field, page) =>
    field.kind === "location" || PLACE.test(field.label) || PLACE.test(page.label);

  async function put(field, page, resume) {
    const value = String(field.value);
    if ((field.source === "file") !== (page.kind === "file")) {
      return { ok: false, reason: "This is not the kind of field Foray planned for." };
    }
    switch (page.kind) {
      case "file": {
        const got = await resume();
        return got.ok ? C.fillFile(page.el, got.file) : { ok: false, reason: got.message };
      }
      case "text":
      case "textarea":
        return C.fillText(page.el, value);
      case "select":
        return C.fillSelect(page.el, value);
      case "radio":
      case "checkbox":
        return C.fillChoice(page.els, value);
      case "buttons":
        return C.fillButtons(page.els, value);
      case "combobox":
        return C.fillCombobox(page.el, page.container, value, { place: isPlace(field, page) });
      default:
        return { ok: false, reason: NOT_FOUND };
    }
  }

  /**
   * Fills the page from an application. `resume()` fetches the file the first time a file
   * control needs it.
   */
  async function run(application, pageFields, adapter, resume) {
    D.clearMarks();
    const used = new Set();
    const outcomes = [];
    for (const field of application.fields) {
      if (field.value === null || field.value === undefined) continue;
      const base = { key: field.key, label: field.label };
      const page = adapter.locate(field, pageFields, used);
      if (!page) {
        outcomes.push({ ...base, status: "unfilled", reason: NOT_FOUND });
        continue;
      }
      used.add(page);
      if (page.consent) {
        outcomes.push({ ...base, status: "consent", page });
        continue;
      }
      let result;
      try {
        result = await put(field, page, resume);
      } catch {
        result = { ok: false, reason: "The page would not take the answer." };
      }
      const status = !result.ok ? "unfilled" : result.unsure ? "check" : "filled";
      const group = page.kind === "radio" || page.kind === "checkbox" || page.kind === "buttons";
      D.mark(group ? page.container : page.el, status === "filled" ? "filled" : "check");
      outcomes.push({
        ...base,
        status,
        shown: result.shown ?? "",
        reason:
          result.reason ??
          (result.unsure ? "Foray chose an answer but could not see the page take it." : ""),
        page,
      });
    }
    return outcomes;
  }

  /**
   * What the form holds now, for the record of what was sent: every question with an
   * answer in it, under the key Foray planned it by when it has one.
   */
  function snapshot(application, adapter) {
    const pageFields = adapter.read();
    const used = new Set();
    const held = [];
    for (const field of application?.fields ?? []) {
      const page = adapter.locate(field, pageFields, used);
      if (!page) continue;
      used.add(page);
      const value = R.valueOf(page);
      if (value) held.push({ key: field.key, label: field.label, value });
    }
    for (const page of pageFields) {
      if (used.has(page)) continue;
      const value = R.valueOf(page);
      if (value) held.push({ key: page.key, label: page.label, value });
    }
    return held.slice(0, 300).map((item) => ({
      key: item.key.slice(0, 200),
      label: item.label.slice(0, 500),
      value: item.value.slice(0, 4000),
    }));
  }

  Foray.fill = { run, snapshot, isPlace };
})();
