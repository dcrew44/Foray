/*
 * What Foray knows about each job board's form: where it is on the page, where one question
 * begins and ends, how a planned field is found again, which button sends the application,
 * and what the page looks like once it has gone in.
 *
 * - Greenhouse: `#application-form`; every control's id is the field's name in the board's
 *   API, which is the key Foray planned under.
 * - Lever: `/apply`, a plain HTML form of `li.application-question` rows.
 * - Ashby: `/application`, React with no `<form>`; each question is a field entry whose
 *   title label names it.
 * - Anything else: the form with the most questions, read by its labels.
 *
 * Confirmation is deliberately hard to meet: the form must be gone from the page and the
 * page must be the board's own confirming address or say so in a few fixed phrases. A missed
 * confirmation costs the user one press of "I sent it" in Foray; a wrong one would record an
 * application that never went in.
 */
(() => {
  const Foray = (globalThis.Foray ??= {});
  if (Foray.boards) return;
  const { text: T, dom: D, reader: R } = Foray;

  /** The smallest visible thing on the page that says an application went in. */
  function confirming() {
    const candidates = document.querySelectorAll(
      'h1, h2, h3, h4, p, [role="status"], [role="alert"], [class*="success"], [class*="confirm"], [class*="thank"]',
    );
    for (const el of candidates) {
      const said = el.textContent ?? "";
      if (said.length < 600 && D.visible(el) && T.saysConfirmed(said)) return el;
    }
    return null;
  }

  function locateByLabel(field, pageFields, used) {
    const open = pageFields.filter(
      (page) => !used.has(page) && (field.type === "file") === (page.kind === "file"),
    );
    const index = T.bestLabel(
      field.label,
      open.map((page) => page.label),
    );
    return index === -1 ? null : open[index];
  }

  const SUBMITS = 'button, input[type="submit"], [role="button"]';

  function submitByWords(root) {
    const typed = root.querySelector('button[type="submit"], input[type="submit"]');
    if (typed && D.visible(typed)) return typed;
    return (
      [...root.querySelectorAll(SUBMITS)].find(
        (el) => D.visible(el) && T.isSubmitText(el.value || el.textContent),
      ) ?? null
    );
  }

  const generic = {
    name: "generic",
    hooks: {},

    /** The address to ask Foray about. */
    lookupUrl() {
      return location.href;
    },

    /**
     * The form: the `<form>` with the most questions, when it has at least three, else the
     * page's main content when that does (React pages often have no `<form>`). A form that
     * asks for a password is a sign-in, never an application.
     */
    root() {
      const questions = (scope) =>
        [...scope.querySelectorAll("input, textarea, select")].filter((el) => {
          const kind = R.kindOf(el);
          return kind && R.usable(el, kind);
        }).length;
      const signIn = (scope) => !!scope.querySelector('input[type="password"]');
      let best = null;
      let most = 2;
      for (const form of document.forms) {
        if (form.getAttribute("role") === "search" || signIn(form)) continue;
        const count = questions(form);
        if (count > most) {
          best = form;
          most = count;
        }
      }
      if (best) return best;
      const page = document.querySelector("main") ?? document.body;
      return page && !signIn(page) && questions(page) > 2 ? page : null;
    },

    read() {
      const root = this.root();
      return root ? R.read(root, this.hooks) : [];
    },

    /** The page's control for a planned field: by its key, else by what it is called. */
    locate(field, pageFields, used) {
      const byKey = pageFields.find((page) => page.key === field.key && !used.has(page));
      return byKey ?? locateByLabel(field, pageFields, used);
    },

    submitButton() {
      const root = this.root();
      return root ? submitByWords(root) : null;
    },

    /** `{element}` once the page confirms the application went in, else null. */
    confirmation() {
      if (this.root()) return null;
      const element = confirming();
      return element ? { element } : null;
    },
  };

  const greenhouse = {
    ...generic,
    name: "greenhouse",

    // The form on an employer's own site is this board's page in a frame, addressed by
    // `for` and `token`. Foray knows the posting by its board address.
    lookupUrl() {
      const url = new URL(location.href);
      const board = url.searchParams.get("for");
      const job = url.searchParams.get("token");
      if (url.pathname.startsWith("/embed/job_app") && board && /^\d+$/.test(job ?? "")) {
        return `https://job-boards.greenhouse.io/${encodeURIComponent(board)}/jobs/${job}`;
      }
      return location.href;
    },

    root() {
      return document.querySelector("#application-form, #application_form");
    },

    locate(field, pageFields, used) {
      const root = this.root();
      // The API's names are the page's ids, with one exception: the location search.
      const ids = [
        field.key === "location" ? "candidate-location" : field.key,
        field.key.replace(/\[\]$/, ""),
      ];
      for (const id of ids) {
        const el = root?.querySelector(`[id="${CSS.escape(id)}"], [name="${CSS.escape(id)}"]`);
        const page = el && pageFields.find((item) => item.els.includes(el) && !used.has(item));
        if (page) return page;
      }
      return locateByLabel(field, pageFields, used);
    },

    submitButton() {
      const root = this.root();
      return root ? submitByWords(root) : null;
    },

    confirmation() {
      if (this.root()) return null;
      const element = confirming();
      if (/\/confirmation\/?$/.test(location.pathname) || element) return { element };
      return null;
    },
  };

  const lever = {
    ...generic,
    name: "lever",
    hooks: {
      block: (el) => el.closest("li.application-question"),
      label: (block) =>
        D.text(block.querySelector(".application-label .text")) ||
        D.text(block.querySelector(".application-label")),
      // A Lever control is known by its name; few have ids.
      key: (block, el) => el.name || el.id || "",
      required: (block) => !!block.querySelector(".application-label .required"),
      // "Apply with LinkedIn" is a sign-in with another site: the user's to press.
      skip: (el) => !!el.closest(".awli-application-row"),
    },

    root() {
      return document.querySelector("form#application-form");
    },

    submitButton() {
      const button = document.querySelector("#btn-submit, button.template-btn-submit");
      return button && D.visible(button) ? button : null;
    },

    confirmation() {
      if (this.root()) return null;
      const element = confirming();
      if (/\/thanks\/?$/.test(location.pathname) || element) return { element };
      return null;
    },
  };

  const ASHBY_ENTRY = '.ashby-application-form-field-entry, [class*="_fieldEntry_"]';

  const ashby = {
    ...generic,
    name: "ashby",
    hooks: {
      block: (el) => el.closest(ASHBY_ENTRY),
      label: (block) =>
        D.text(block.querySelector(".ashby-application-form-question-title, label")) ||
        D.text(block.querySelector(".ashby-application-form-question-description")),
      key: (block) =>
        block.getAttribute("data-field-path") ||
        block.querySelector("label[for]")?.getAttribute("for") ||
        "",
      required: (block) => !!block.querySelector('label[class*="_required_"]'),
      // "Autofill from resume" reads a file and rewrites the form. The resume goes in the
      // form's own field.
      skip: (el) =>
        !!el.closest('.ashby-application-form-autofill-uploader, [class*="_autofillPane_"]'),
    },

    root() {
      const entry = document.querySelector(ASHBY_ENTRY);
      if (!entry) return null;
      return entry.closest('[role="tabpanel"]') ?? entry.parentElement;
    },

    submitButton() {
      const button = document.querySelector(
        '.ashby-application-form-submit-button, button[class*="_submitButton_"]',
      );
      return button && D.visible(button) ? button : null;
    },

    confirmation() {
      if (this.root()) return null;
      const element =
        [...document.querySelectorAll('[class*="ashby-application-form-success"]')].find(
          D.visible,
        ) ?? confirming();
      return element ? { element } : null;
    },
  };

  /** The adapter for the page this script is in. */
  function pick() {
    const host = location.hostname;
    if (/(^|\.)greenhouse\.io$/.test(host)) return greenhouse;
    if (/(^|\.)lever\.co$/.test(host)) return lever;
    if (/(^|\.)ashbyhq\.com$/.test(host)) return ashby;
    return generic;
  }

  Foray.boards = { pick, generic, greenhouse, lever, ashby };
})();
