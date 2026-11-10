/*
 * What happens on a page, in order:
 *
 * 1. Ask the service worker whether Foray has an application for this page. The worker
 *    holds the token and does every request; this script never sees it, and it can ask only
 *    about the page it is in and act only on the application found for it.
 * 2. If it has, show the panel: which application, how many answers are ready, what is
 *    missing. A form Foray could not read ahead is read from the page first and planned by
 *    Foray. Nothing on the page is touched before the user presses "Fill the form".
 * 3. On that press, fill, mark what was filled and what was not, tell Foray what went in,
 *    and stop: the user reads the form and presses submit. (Only when Foray says an
 *    application may be sent on its own, and nothing on the page asks for the user, does the
 *    extension press submit.)
 * 4. When the user has pressed submit and the board's page then confirms the application,
 *    tell Foray once, with the page's words and a picture of it.
 */
(() => {
  const Foray = (globalThis.Foray ??= {});
  if (Foray.main) return;
  const { text: T, dom: D, controls: C, reader: R, boards: B, handoff: H, panel: P } = Foray;
  const F = Foray.fill;

  const adapter = B.pick();
  const top = window === window.top;
  const state = {
    url: "",
    forayUrl: "",
    app: null, // the application for this page, as Foray gave it
    pageFields: [], // the form as last read from the page
    planned: false, // an unread form's questions have been sent to be planned
    hasForm: false,
    busy: false,
    results: null, // what the last fill did
    pending: null, // {id, submitSeen}: a fill in this tab that waits for the page to confirm
    confirming: false,
    confirmTries: 0,
    reported: false,
    stopped: false,
  };

  // --- talking to the service worker --------------------------------------------------------

  async function send(message) {
    try {
      return (await chrome.runtime.sendMessage(message)) ?? { ok: false };
    } catch {
      // The extension was reloaded or removed under this page: this script is an orphan.
      stop();
      return { ok: false, gone: true };
    }
  }

  function stop() {
    state.stopped = true;
    clearInterval(timer);
    P.remove();
  }

  // Looking, planning, filling and reporting each wait for the one before: two of them at
  // once would read a form the other is changing.
  let chain = Promise.resolve();
  let waiting = 0;

  function inTurn(task) {
    waiting += 1;
    const run = chain.then(task).finally(() => {
      waiting -= 1;
    });
    chain = run.catch(() => {});
    return run;
  }

  // --- views --------------------------------------------------------------------------------

  const BOARD_FRAME = 'iframe[src*="greenhouse.io/embed/job_app"]';
  // Where this page is, without what follows ? or #: a form's own scripts change those.
  const here = () => location.origin + location.pathname;

  const heading = () => ({ title: state.app?.title ?? "", company: state.app?.company ?? "" });
  const NOT_SENT = "Nothing is sent. You read the form and press submit.";

  function say(text, tone = "quiet", options = {}) {
    P.show({ ...heading(), tone, lines: [{ text, kind: tone === "error" ? "error" : "" }] }, options);
  }

  function readyView() {
    const app = state.app;
    const answers = app.fields.filter((field) => field.value !== null);
    const wantsResume = app.fields.some((field) => field.source === "file");
    const lines = [];
    const ready = answers.length - (wantsResume && !app.resume ? 1 : 0);
    lines.push({
      text:
        ready > 0
          ? `${T.plural(ready, "answer")} ready${wantsResume && app.resume ? ", with your resume" : ""}.`
          : "Foray has no answers for this form yet.",
      kind: "strong",
    });
    if (wantsResume && !app.resume) {
      lines.push({
        text: "There is no resume PDF yet. Make one on Foray's Profile page, or attach yours here yourself.",
        kind: "muted",
      });
    }
    if (app.maySubmit === true) {
      lines.push({
        text: "You let Foray send this one itself. It still stops for a CAPTCHA, a sign-in, or anything it could not fill.",
        kind: "muted",
      });
    }
    const missing = app.missing ?? [];
    return {
      ...heading(),
      tone: "ready",
      lines,
      lists: [
        {
          heading: `${T.plural(missing.length, "question")} still ${missing.length === 1 ? "needs" : "need"} your answer`,
          items: missing,
        },
      ],
      link: missing.length
        ? { text: "Answer them in Foray", href: `${state.forayUrl}/apply` }
        : null,
      action: { label: "Fill the form", run: startFill },
      busy: state.busy,
      footnote: app.maySubmit === true ? "" : NOT_SENT,
    };
  }

  /** "Question: why not", without a colon after a question mark. */
  const because = (item) =>
    /[?.!:]$/.test(item.label) ? `${item.label} ${item.reason}` : `${item.label}: ${item.reason}`;

  function filledView() {
    const { outcomes, empty, consent, handoff, pressed, picture } = state.results;
    const filled = outcomes.filter((item) => item.status === "filled");
    const unsure = outcomes.filter((item) => item.status === "check");
    const unfilled = outcomes.filter((item) => item.status === "unfilled");
    const lines = [
      { text: `Filled ${filled.length} of ${T.plural(outcomes.length, "answer")}.`, kind: "strong" },
    ];
    lines.push({
      text: pressed
        ? "Foray pressed submit, as you allowed for this application."
        : "Check everything, then press submit yourself.",
      kind: "signal",
    });
    // A CAPTCHA, a sign-in or an account to create: said in full, because it is the user's.
    if (handoff) lines.push({ text: handoff.message });
    if (consent) {
      lines.push({
        text: `Foray never ticks agreement boxes: ${T.plural(consent, "box", "boxes")} left for you to read.`,
        kind: "muted",
      });
    }
    if (!pressed && !picture) {
      lines.push({
        text: "Foray will note the confirmation page's words. For a picture of it too, click Foray in the toolbar on this tab before you submit.",
        kind: "muted",
      });
    }
    return {
      ...heading(),
      tone: "waiting",
      lines,
      lists: [
        { heading: "Not filled", items: unfilled.map(because) },
        { heading: "Check these", items: unsure.map(because) },
        { heading: "Still empty and required", items: empty },
      ],
      // The next move is the user's, so this button stays quiet.
      action: { label: "Fill again", run: startFill, quiet: true },
      busy: state.busy,
    };
  }

  // --- reading and planning -----------------------------------------------------------------

  /**
   * Reads the form and, when Foray could not read it ahead, has Foray plan what was found.
   * `open` also opens each drop-down once to read its options, which is done only after the
   * user asked for the fill.
   */
  async function plan({ open }) {
    const fields = adapter.read();
    if (open && !state.app.formRead) {
      for (const field of fields) {
        const unknown = field.kind === "combobox" && !field.options.length;
        if (unknown && !F.isPlace({ label: field.label }, field)) {
          field.options = await C.comboboxOptions(field.el, field.container);
        }
      }
    }
    state.pageFields = fields;
    if (state.app.formRead) return true;
    const readable = R.forPlan(fields);
    if (!readable.length) return true;
    const answer = await send({ type: "plan", id: state.app.id, fields: readable });
    if (answer.gone) return false;
    if (!answer.ok) {
      say(answer.error?.message ?? "Foray could not plan this form.", "error");
      return false;
    }
    state.app = answer.application;
    state.planned = true;
    return true;
  }

  /** Waits until a form the page is still drawing has stopped growing. */
  async function settled() {
    let count = adapter.read().length;
    for (let round = 0; round < 6; round += 1) {
      await D.wait(300);
      const now = adapter.read().length;
      if (now === count) return;
      count = now;
    }
  }

  /** Draws the panel for the application as it stands. */
  async function refresh(options) {
    const app = state.app;
    if (!app || state.stopped) return;
    state.hasForm = !!adapter.root();
    if (app.blocked) return say(app.blocked, "quiet", options);
    if (!state.hasForm) {
      // The form is the board's own frame inside this page: the panel is in there.
      if (document.querySelector(BOARD_FRAME)) return P.remove();
      const handoff = H.detect();
      return say(
        handoff?.message ??
          "Foray has this application ready. Open its form on this page and it can be filled.",
        handoff ? "waiting" : "quiet",
        options,
      );
    }
    if (!app.formRead && !state.planned) {
      await settled();
      if (!(await plan({ open: false }))) return;
    }
    P.show(readyView(), options);
  }

  /** Asks Foray about this page and shows what it says. `forced`: the user asked. */
  async function look({ forced = false } = {}) {
    state.url = here();
    const options = { always: forced };
    const answer = await send({ type: "find" });
    if (answer.gone || state.stopped) return;
    state.app = null;
    state.results = null;
    state.planned = false;
    if (!answer.configured) {
      if (forced && top) {
        say(
          "Foray is not connected yet. Open the extension's settings and add your Foray address and a token.",
          "quiet",
          options,
        );
      } else P.remove();
      return;
    }
    state.forayUrl = answer.forayUrl;
    state.pending = answer.pending ?? null;
    if (state.pending) {
      watchSubmit();
      // The press of submit was noted on the page it happened on, in case the worker was
      // asleep and the page left before it heard.
      const mark = readMark();
      const fresh = mark && mark.at >= state.pending.at && Date.now() - mark.at < MARK_FOR_MS;
      if (!state.pending.submitSeen && fresh) {
        const noted = await send({
          type: "submit-seen",
          id: state.pending.id,
          auto: mark.auto === true,
        });
        if (noted.ok) state.pending.submitSeen = true;
      }
      if (await confirmIfShown()) return;
    }
    if (answer.error) {
      if (forced && top) say(answer.error.message, "error", options);
      else P.remove();
      return;
    }
    state.app = answer.application ?? null;
    if (!state.app) {
      if (forced && top) {
        say(
          "Foray has no application waiting for this page. Prepare one in Foray, then open its form from the Apply page.",
          "quiet",
          options,
        );
      } else if (!state.pending) P.remove();
      return;
    }
    await refresh(options);
  }

  // --- the fill -----------------------------------------------------------------------------

  let resumeFile = null;

  async function resume() {
    if (resumeFile) return { ok: true, file: resumeFile };
    if (!state.app.resume) {
      return { ok: false, message: "There is no resume PDF yet. Make one on Foray's Profile page." };
    }
    const answer = await send({ type: "resume", id: state.app.id });
    if (!answer.ok) {
      return { ok: false, message: answer.error?.message ?? "The resume could not be fetched." };
    }
    const bytes = Uint8Array.from(atob(answer.base64), (char) => char.charCodeAt(0));
    resumeFile = new File([bytes], answer.filename, { type: answer.mime });
    return { ok: true, file: resumeFile };
  }

  /** The required questions on the page that still hold nothing. */
  function stillEmpty() {
    return adapter
      .read()
      .filter((field) => field.required && !R.valueOf(field))
      .map((field) => field.label);
  }

  /** The panel's button: says so at once, then fills when the page is not being read. */
  function startFill() {
    if (state.busy || state.stopped) return;
    state.busy = true;
    P.show({ ...(P.current() ?? heading()), busy: true });
    inTurn(fill);
  }

  async function fill() {
    if (!state.app || state.stopped) {
      state.busy = false;
      return;
    }
    state.busy = true;
    P.show({ ...(P.current() ?? heading()), busy: true });
    try {
      // The user may have answered a question in Foray since this page loaded.
      if (state.app.formRead) {
        const fresh = await send({ type: "get", id: state.app.id });
        if (fresh.gone) return;
        if (fresh.ok) state.app = fresh.application;
      }
      if (!(await plan({ open: true }))) return;
      const app = state.app;
      if (app.blocked || !adapter.root()) {
        state.busy = false;
        return void (await refresh());
      }
      const outcomes = await F.run(app, state.pageFields, adapter, resume);
      const filled = outcomes
        .filter((item) => item.status === "filled")
        .map((item) => ({ key: item.key, label: item.label, value: item.shown }));
      let picture = false;
      if (filled.length) {
        const noted = await send({
          type: "filled",
          id: app.id,
          filled,
          title: app.title,
          company: app.company,
        });
        if (noted.gone) return;
        picture = !!noted.picture;
        state.pending = { id: app.id, submitSeen: false, at: Date.now() };
        state.reported = false;
        state.confirmTries = 0;
        clearMark();
        watchSubmit();
      }
      const consent = adapter.read().filter((field) => field.consent).length;
      state.results = {
        outcomes,
        empty: stillEmpty(),
        consent,
        handoff: H.detect(),
        pressed: false,
        picture,
      };
      state.busy = false;
      if (maySubmit(app)) await pressSubmit(app);
      P.show(filledView());
    } finally {
      state.busy = false;
    }
  }

  // --- submit -------------------------------------------------------------------------------

  /**
   * Whether the extension may press submit itself. Foray must have said so for this
   * application, every planned answer must be in the form, nothing required may be empty,
   * and the page must not be asking for a person: a CAPTCHA, a sign-in, an account.
   */
  function maySubmit(app) {
    const { outcomes, empty } = state.results;
    return (
      app.maySubmit === true &&
      !app.blocked &&
      (app.missing ?? []).length === 0 &&
      outcomes.length > 0 &&
      outcomes.every((item) => item.status === "filled") &&
      empty.length === 0 &&
      !H.detect()
    );
  }

  async function pressSubmit(app) {
    const button = adapter.submitButton();
    if (!button || button.disabled || button.getAttribute("aria-disabled") === "true") return;
    const noted = await send({
      type: "submit-seen",
      id: app.id,
      auto: true,
      filled: F.snapshot(app, adapter),
    });
    // Asked again at the last moment: a challenge can appear while the form is being filled.
    if (!noted.ok || H.detect()) return;
    state.pending = { ...state.pending, id: app.id, submitSeen: true };
    state.results.pressed = true;
    setMark(true);
    pressing = true;
    try {
      button.click();
    } finally {
      pressing = false;
    }
  }

  // That submit was pressed is also left on the page itself, where the next page of the same
  // site finds it: a message sent as a page unloads is lost when the worker has to be woken
  // for it. It says only when, and whether the press was the extension's.
  const MARK = "foray:submit";
  const MARK_FOR_MS = 15 * 60 * 1000;

  function setMark(auto) {
    try {
      sessionStorage.setItem(MARK, JSON.stringify({ at: Date.now(), auto }));
    } catch {
      // No storage on this page: the message alone will have to do.
    }
  }

  function readMark() {
    try {
      const mark = JSON.parse(sessionStorage.getItem(MARK) ?? "null");
      return typeof mark?.at === "number" ? mark : null;
    } catch {
      return null;
    }
  }

  function clearMark() {
    try {
      sessionStorage.removeItem(MARK);
    } catch {
      // Nothing to clear.
    }
  }

  let watching = false;
  let pressing = false; // the extension's own press, which is already noted as its own
  let editTimer = 0;

  /** Tells the worker what the form holds now, so the record of what was sent is current. */
  function tellHeld() {
    const pending = state.pending;
    if (!pending || state.reported || !adapter.root()) return;
    send({ type: "held", id: pending.id, filled: F.snapshot(state.app, adapter) });
  }

  /**
   * Notes when the user presses the form's submit button, and what they change in the form
   * before they do. It changes nothing on the page.
   */
  function watchSubmit() {
    if (watching) return;
    watching = true;
    const seen = () => {
      const pending = state.pending;
      if (!pending || state.reported || pressing) return;
      pending.submitSeen = true;
      setMark(false);
      send({ type: "submit-seen", id: pending.id, filled: F.snapshot(state.app, adapter) });
    };
    const edited = () => {
      clearTimeout(editTimer);
      editTimer = setTimeout(tellHeld, 600);
    };
    document.addEventListener("input", edited, true);
    document.addEventListener("change", edited, true);
    document.addEventListener(
      "click",
      (event) => {
        const el =
          event.target instanceof Element
            ? event.target.closest('button, input[type="submit"], [role="button"]')
            : null;
        if (!el || !state.pending) return;
        const button = adapter.submitButton();
        if (el === button || T.isSubmitText(el.value || el.textContent)) seen();
      },
      true,
    );
    document.addEventListener("submit", seen, true);
  }

  // --- confirmation -------------------------------------------------------------------------

  const frame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));

  /** Reports the confirmation once the page shows it. True when it was recorded. */
  async function confirmIfShown() {
    const pending = state.pending;
    if (!pending?.submitSeen || state.reported || state.confirming || state.confirmTries >= 3) {
      return false;
    }
    if (!adapter.confirmation()) return false;
    state.confirming = true;
    try {
      // Let the page finish drawing, and make sure it still says so.
      await D.wait(400);
      const shown = adapter.confirmation();
      if (!shown) return false;
      shown.element?.scrollIntoView?.({ block: "center" });
      P.conceal(true); // the picture is of the employer's page, not of this panel
      await frame();
      await frame();
      await D.wait(100);
      const words = (document.querySelector("main") ?? document.body).innerText ?? "";
      const answer = await send({
        type: "confirmed",
        id: pending.id,
        url: location.href,
        text: words.slice(0, 20000),
      });
      P.conceal(false);
      if (answer.gone) return false;
      if (!answer.ok) {
        state.confirmTries += 1;
        if (!answer.ignored && state.confirmTries >= 3) {
          say(
            `${answer.error?.message ?? "Foray could not be told."} In Foray, press "I sent it" on this application.`,
            "error",
          );
        }
        return false;
      }
      state.reported = true;
      state.pending = null;
      clearMark();
      state.app = { ...(state.app ?? {}), title: answer.title, company: answer.company };
      P.show(
        {
          ...heading(),
          tone: "done",
          lines: [
            { text: "Sent. Foray recorded it.", kind: "strong" },
            {
              text: answer.picture
                ? "It kept the confirmation page's words and a picture of it."
                : "It kept the confirmation page's words. There is no picture: Chrome allows one only on a tab where you clicked Foray in the toolbar.",
              kind: "muted",
            },
          ],
        },
        { always: true },
      );
      state.app = null;
      return true;
    } finally {
      P.conceal(false);
      state.confirming = false;
    }
  }

  // --- the page changing under us -----------------------------------------------------------

  let ticks = 0;

  async function tick() {
    if (state.stopped) return;
    if (here() !== state.url) return void (await look());
    if (state.pending && (await confirmIfShown())) return;
    // A word to the worker now and then keeps it awake while a filled form waits to be
    // sent, so it is there to hear the press of submit.
    ticks += 1;
    if (state.pending && ticks % 20 === 0) tellHeld();
    // The form may arrive after the page: Ashby draws it, and its tab can be switched to.
    if (state.app && !state.results && !!adapter.root() !== state.hasForm) await refresh();
  }

  const timer = setInterval(() => {
    if (!waiting) inTurn(tick);
  }, 1000);

  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id || state.stopped) return undefined;
    if (message?.type === "ping") {
      if (top) respond({ ok: true });
      return undefined;
    }
    if (message?.type === "fill-now") {
      // The toolbar button: find the application, then fill at once. The answer goes back
      // straight away so the popup can close and give the page its focus back.
      if (top) respond({ ok: true });
      inTurn(async () => {
        await look({ forced: true });
        if (state.app && !state.app.blocked && adapter.root()) await fill();
      });
    }
    return undefined;
  });

  Foray.main = { state, adapter };
  inTurn(look);
})();
