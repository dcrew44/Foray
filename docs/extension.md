# The browser extension

Applying happens in your own browser, as you. Foray prepares an application (the form's
questions, an answer for each); the extension in `extension/` puts those answers into the
employer's form and stops. You read the form and press submit. When the employer's page
confirms, the extension tells Foray, with proof.

It is a Chrome extension (Manifest V3) made of plain JavaScript files that Chrome loads as
they are: no build step, no dependencies, no code fetched from anywhere.

## What it does

1. On an application page of a job board Foray reads, it asks your Foray whether an
   application is prepared for that page.
2. If one is, it shows a small panel: which application, how many answers are ready, which
   questions still need one from you. Nothing on the page is touched yet.
3. You press **Fill the form**. It puts each answer into its field, attaches your resume
   PDF, outlines what it filled in green and what it could not in dashed marigold, and lists
   both in the panel, with the required questions that are still empty.
4. You check the form and press submit.
5. When the board's page says the application went in, the extension reports it to Foray
   once: the page's address and words, what the form held when you sent it, and a picture of
   the page. The match moves to Applied.

A form Foray could read ahead (Greenhouse publishes its questions) is filled from that plan.
Any other form is read from the page when you open it, planned by Foray from your saved
answers, and filled from the result. On a page that is not one of the boards, click Foray in
the toolbar and then **Fill this page with Foray**.

## What it never does

- **It does not press submit**, with one exception you have to turn on: in Foray's Settings,
  under Browser extension, you can let it submit applications that are answered entirely
  from what you saved. Foray decides which those are, one application at a time. Even then
  the extension stops with the form filled when the page shows a CAPTCHA, a sign-in or an
  account to create, when any answer could not be put in, or when a required question is
  still empty.
- **It does not solve or work around a CAPTCHA.** The invisible ones every board loads are
  left to do their job; one that asks for something is yours.
- **It does not sign in or create an account**, and it never types into a password field.
- **It does not make up an answer.** A field Foray has no answer for is left exactly as it
  was found. It never ticks an agreement box (terms, privacy, marketing), even if a plan
  says to.
- **It does not disguise itself.** No fake typing, no delays, no pointer paths: values are
  set at once, and filled fields are outlined on the page.
- **It talks to nothing but your own Foray.** The token that signs it in stays in the
  extension's service worker; the scripts that run inside employers' pages never see it.

## Install it

1. Open `chrome://extensions`, turn on **Developer mode**, press **Load unpacked** and
   choose the repository's `extension/` folder. Its settings page opens.
2. In Foray, open **Settings**, find **Browser extension**, name a token and press **Make a
   token**. Copy it: it is shown once.
3. In the extension's settings, type the address you open Foray at (for example
   `http://localhost:3000`), paste the token and press **Save and test connection**. Chrome
   asks whether the extension may reach that address; allow it. The page then says who you
   are connected as.

To change the token later, paste a new one and save. **Disconnect** forgets the address and
the token; remove the token in Foray's Settings as well, which signs out any copy of it.

After changing the extension's files, press the reload arrow on its card in
`chrome://extensions`, then reload any form page that was open.

## Using it

Open a prepared application from Foray's Apply page. The panel appears at the top right of
the form. **Fill the form**, read everything, answer what is listed as still empty, tick any
agreement yourself, and submit.

**A picture of the confirmation.** Chrome lets an extension take a picture of a tab only if
you clicked the extension's toolbar button on that tab. Start the fill from the toolbar
(**Fill this page with Foray**), or click the toolbar button once before you submit, and the
confirmation is recorded with a picture. Started only from the panel, it is recorded with
the page's address and words, and the panel says so.

**When the confirmation is not noticed.** The extension reports only when it filled the form
in that tab, saw submit pressed, and the page is then the board's own confirming page or
says so in a few fixed phrases. That is strict on purpose. When it misses one, press **I sent
it** on the application in Foray.

**Limits.** Foray fills at most so many applications to one employer in 60 days and in one
day (Settings, Browser extension). Past a limit the panel says so and fills nothing; you can
still apply by hand.

## What it can reach, and why

| In the manifest | Why |
|---|---|
| `storage` | The Foray address and the token, kept in this browser profile. Content scripts are shut out of it (`chrome.storage.local.setAccessLevel`). |
| `activeTab` | The page you click the toolbar button on: to fill a form on a site that is not one of the boards, and to take the picture of the confirmation page (`chrome.tabs.captureVisibleTab` is allowed only with this, or with access to every site). |
| `scripting` | To put the content scripts into that page after the click. |
| Content scripts on the job boards | To show the panel there without a click. The hosts are the boards Foray reads: Greenhouse, Lever, Ashby, Gem, Workable, Recruitee, SmartRecruiters, iCIMS and Workday. Greenhouse's embedded form (`/embed/job_app`) is also matched inside other sites' frames. |
| `optional_host_permissions` for any `http(s)` address | So you can allow one address: your own Foray's, which is not known until you type it. Nothing is granted until you press Save and accept Chrome's prompt, and only that host is asked for. |

It does not ask for `tabs`, `webRequest`, `cookies`, `<all_urls>` or `debugger`, exposes none
of its files to pages (`web_accessible_resources`) and accepts no messages from pages
(`externally_connectable`). Its requests to Foray carry the token and no cookie; Foray
refuses a request to these routes that carries a session cookie.

## How it is built

```
extension/
  manifest.json
  background.js        the service worker: the only place the token is read and Foray is called
  lib/api.js           Foray's API, for the worker
  lib/origin.js        the address typed on the options page, checked
  lib/urls.js          which address to ask Foray about for a page or frame
  options/  popup/     the settings page and the toolbar popup
  ui/foray.css         Foray's colours for those two pages
  content/             what runs in an application page, in the extension's own world:
    text.js              how labels, options and confirming words are compared (no DOM)
    dom.js               visibility, events, setting a value so a framework notices, outlines
    controls.js          one strategy per control: text, select, radios and tick boxes,
                         Yes / No buttons, ARIA comboboxes, file inputs
    reader.js            reads a form from the page: questions, labels, options, required
    boards.js            Greenhouse, Lever, Ashby and the generic adapter
    handoff.js           a CAPTCHA that asks for something, a sign-in, an account to create
    fill.js              the fill, and the rules it keeps
    panel.js             the panel, in a closed shadow root
    main.js              the order of things on a page
  test/replicas/       hand-written stand-ins for the boards' forms
  test/unit/           Vitest tests of the pure helpers
```

Content scripts are classic scripts sharing one object (`Foray`) in the extension's isolated
world; the worker, the options page and the popup are ES modules. A content script asks the
worker by message and is told only about the page Chrome says it is in and the application
Foray found for it. What a tab is in the middle of (a filled form waiting to be sent) is kept
in `chrome.storage.session`, because Chrome stops an idle worker after half a minute.

A new board needs an adapter in `content/boards.js` (where the form is, where one question
begins and ends, the submit button, what a confirmation looks like), its host in the
manifest's `matches`, and a replica with a test.

## Tests

```bash
pnpm test:web                                   # the pure helpers (extension/test/unit)
cd backend
uv run playwright install chromium              # once: Chrome itself will not load an unpacked extension from the command line
uv run pytest tests/extension -m "not slow"     # the manifest and the code, without a browser
uv run pytest tests/extension -m slow           # a real Chromium with the extension loaded
FORAY_EXTENSION_HEADED=1 uv run pytest tests/extension -m slow -k lever   # watch one
```

The browser tests live in `backend/tests/extension/` and are marked `slow`, with the other
tests that need Chromium, so CI's `slow` job runs them. They load the extension into
Playwright's Chromium and drive it against:

- **Replicas** of a Greenhouse, a Lever and an Ashby form, an employer's own form, and a
  sign-in page (`extension/test/replicas/`): hand-written HTML with the boards' ids, names,
  roles and label markup, and small stand-ins for a framework-owned input and a react-select
  combobox. They are served by the browser itself at the boards' real addresses, so the
  manifest's own `matches` decide where the extension runs, and no name but this machine's
  resolves: a test cannot reach a real site.
- **A fake Foray** (`support.py`) that speaks the extension's API and records what it is
  sent, and, in one test, Django's own API with a real token and application.

Every test ends by asking Chrome what it holds against the extension (install warnings,
manifest errors, errors raised while running) and fails if there is anything.

The tests load a copy of the folder with one line added to the manifest: the fake server's
address is granted up front, because the prompt Chrome shows for an optional permission
cannot be pressed by a script. One test loads the folder exactly as it is.

**No test touches a real application form, and nobody should test this by filling one they
do not mean to send.** The replicas are built from reading each board's page, not from using
it, so the first real use of each board is where their differences from the real thing show:
see the checklist below.

## The first time on a real form

The replicas match the structure of each board's form on the day they were written. Check
these on the first real application you mean to send on each board, before pressing submit:

- **Greenhouse.** Every drop-down shows the option Foray chose (they are react-select
  comboboxes). The location search chose the right city. The phone number's country, which
  Foray does not answer, is set. The resume's name appears next to Attach. A question with
  tick boxes has the right ones ticked.
- **Lever.** The resume is attached and Lever's own "Success!" appears beside it (Lever reads
  the file when it is attached). The location field kept what was typed; Lever offers its own
  suggestions there and may want one chosen. Radio and tick-box questions show the right
  choice.
- **Ashby.** Your name and email stayed in their fields after the fill (the page is React).
  The Yes / No buttons show the chosen side. The location and any other search-as-you-type
  list show a chosen value. The resume went into the Resume field, not into "Autofill from
  resume".
- **All three.** After you submit, the panel says "Sent. Foray recorded it." and the match
  in Foray has moved to Applied. If it has not, press **I sent it** on the application there.
