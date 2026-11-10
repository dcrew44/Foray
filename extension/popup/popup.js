/*
 * The toolbar popup: whether the extension is connected, and one button that fills the page
 * in front. The fill itself and everything it reports happen in the page's own panel; the
 * popup closes as soon as the page has taken the request, which also gives the page its
 * focus back.
 *
 * Opening the popup is the one moment Chrome lets the extension act on a tab it has no
 * standing access to (`activeTab`), and the only way it is allowed to take a picture of the
 * confirmation page later. The service worker notes that when it is asked for the state.
 */
const connection = document.getElementById("connection");
const waiting = document.getElementById("waiting");
const openForay = document.getElementById("open-foray");
const fill = document.getElementById("fill");
const connect = document.getElementById("connect");
const result = document.getElementById("result");
const settings = document.getElementById("settings");

const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

settings.addEventListener("click", () => chrome.runtime.openOptionsPage());
connect.addEventListener("click", () => chrome.runtime.openOptionsPage());

fill.addEventListener("click", async () => {
  fill.disabled = true;
  result.textContent = "";
  const answer = await chrome.runtime.sendMessage({ type: "popup-fill", tabId: tab?.id });
  if (answer?.ok) {
    window.close();
    return;
  }
  result.textContent = answer?.message ?? "Foray could not start on this page.";
  fill.disabled = false;
});

const state = await chrome.runtime.sendMessage({ type: "popup-state", tabId: tab?.id });
if (!state?.configured) {
  connection.textContent = "Not connected to Foray yet.";
  fill.hidden = true;
  connect.hidden = false;
} else if (!state.me?.ok) {
  connection.textContent = state.me?.error?.message ?? "Foray could not be reached.";
  connection.className = "error";
} else {
  connection.textContent = `Connected as ${state.me.name}.`;
  openForay.textContent =
    state.me.waiting === 1
      ? "1 application is waiting in Foray"
      : `${state.me.waiting} applications are waiting in Foray`;
  openForay.href = `${state.forayUrl}/apply`;
  waiting.hidden = false;
  fill.disabled = false;
}
